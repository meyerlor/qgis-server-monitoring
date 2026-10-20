"""
Shared SQLite layer for the Lizmap tile-cache management.

The cache database is the *only* interface between the dashboard (monitor.py)
and the cache worker (cache_worker.py): the dashboard inserts jobs, schedules
and requested actions; the worker executes them and writes progress, logs, the
seeding ledger and cache sizes back. The dashboard never runs a command itself.

Both processes run as different users (dashboard: monitoring/root, worker: the
Lizmap PHP user, e.g. www-data), so the DB directory is shared via a group and
every process opens it with a group-writable umask.

stdlib only — the worker has no third-party dependencies.

Copyright (c) 2025 meyerlor - MIT License (see LICENSE)
"""

import copy
import ipaddress
import json
import math
import os
import re
import sqlite3
from datetime import datetime
from urllib.parse import urlparse

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per configured Lizmap instance (filled by the worker from `env`)
CREATE TABLE IF NOT EXISTS instances (
    name         TEXT PRIMARY KEY,
    lizmap_path  TEXT,
    env_json     TEXT,
    refreshed_at TEXT,
    error        TEXT
);

-- Projects found in the instance's repositories, with their capabilities
CREATE TABLE IF NOT EXISTS projects (
    instance     TEXT NOT NULL,
    repository   TEXT NOT NULL,
    project      TEXT NOT NULL,
    repo_label   TEXT,
    title        TEXT,
    qgs_path     TEXT,
    qgs_mtime    INTEGER,
    cfg_mtime    INTEGER,
    caps_json    TEXT,
    refreshed_at TEXT,
    error        TEXT,
    PRIMARY KEY (instance, repository, project)
);

-- Cache size per layer and tile matrix set (file count == tile count)
CREATE TABLE IF NOT EXISTS layer_sizes (
    instance   TEXT NOT NULL,
    repository TEXT NOT NULL,
    project    TEXT NOT NULL,
    layer      TEXT NOT NULL,
    tms        TEXT NOT NULL,
    bytes      INTEGER,
    files      INTEGER,
    scanned_at TEXT,
    PRIMARY KEY (instance, repository, project, layer, tms)
);

-- Seeding ledger: Lizmap's cache keys are md5 hashes, so which zoom levels are
-- cached cannot be read back from the cache itself. We record what we seeded.
CREATE TABLE IF NOT EXISTS seeded_levels (
    instance     TEXT NOT NULL,
    repository   TEXT NOT NULL,
    project      TEXT NOT NULL,
    layer        TEXT NOT NULL,
    tms          TEXT NOT NULL,
    level        INTEGER NOT NULL,
    seeded_at    TEXT,
    image_format TEXT,
    tiles        INTEGER,
    errors       INTEGER,
    forced       INTEGER,
    bbox         TEXT,
    job_id       INTEGER,
    PRIMARY KEY (instance, repository, project, layer, tms, level)
);

CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT NOT NULL,               -- seed | clean | refresh
    instance         TEXT,
    repository       TEXT,
    project          TEXT,
    layer            TEXT,
    tms              TEXT,
    zmin             INTEGER,
    zmax             INTEGER,
    bbox             TEXT,
    force            INTEGER DEFAULT 0,
    pool             TEXT,                        -- pool name, NULL = worker default
    window_only      INTEGER DEFAULT 0,
    priority         INTEGER DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'queued',
                     -- queued | running | paused | done | failed | cancelled
    pause_reason     TEXT,                        -- user | load | window
    requested_action TEXT,                        -- pause | resume | cancel
    schedule_id      INTEGER,
    created_by       TEXT,
    created_at       TEXT,
    started_at       TEXT,
    finished_at      TEXT,
    current_level    INTEGER,
    tiles_total      INTEGER,
    tiles_done       INTEGER DEFAULT 0,
    errors           INTEGER DEFAULT 0,
    message          TEXT,
    pid              INTEGER
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS job_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    ts     TEXT,
    level  TEXT,                                  -- info | warn | error
    line   TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_log_job ON job_log(job_id, id);

CREATE TABLE IF NOT EXISTS schedules (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    instance       TEXT NOT NULL,
    repository     TEXT NOT NULL,
    project        TEXT NOT NULL,
    layer          TEXT NOT NULL,
    tms            TEXT NOT NULL,
    zmin           INTEGER NOT NULL,
    zmax           INTEGER NOT NULL,
    bbox           TEXT,
    interval_hours REAL NOT NULL,
    pool           TEXT,
    window_only    INTEGER DEFAULT 1,
    enabled        INTEGER DEFAULT 1,
    last_run       TEXT,
    next_run       TEXT,
    created_by     TEXT,
    updated_at     TEXT,
    UNIQUE (instance, repository, project, layer, tms)
);
"""

ACTIVE_STATUSES = ('queued', 'running', 'paused')
TIME_FMT = '%Y-%m-%d %H:%M:%S'


def now_str():
    return datetime.now().strftime(TIME_FMT)


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.strptime(value[:19], TIME_FMT)
    except ValueError:
        return None


def connect(path):
    """Open the shared cache DB. Group-writable umask so the other process
    (different user, same group) can write the DB and its WAL/SHM files."""
    os.umask(0o007)
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, mode=0o2770, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=30000')
    _share_with_group(path)
    return conn


def _share_with_group(path):
    """SQLite creates its files with mode 0644 regardless of the umask, so the
    other process (same group, different user) could not write. Make the DB and
    its WAL/SHM files group-writable — only files we own can be chmod'ed."""
    uid = os.geteuid()
    for p in (path, path + '-wal', path + '-shm'):
        try:
            st = os.stat(p)
            if st.st_uid == uid and (st.st_mode & 0o060) != 0o060:
                os.chmod(p, (st.st_mode | 0o060) & 0o7777)
        except OSError:
            pass


def init_schema(conn):
    conn.executescript(SCHEMA)
    # v2: free-form job parameters (pool test URL, discovery base, ...)
    cols = {r['name'] for r in conn.execute('PRAGMA table_info(jobs)')}
    if 'params' not in cols:
        conn.execute('ALTER TABLE jobs ADD COLUMN params TEXT')
    conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(SCHEMA_VERSION),))
    conn.commit()


def meta_get(conn, key, default=None):
    row = conn.execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
    if not row or row['value'] is None:
        return default
    try:
        return json.loads(row['value'])
    except ValueError:
        return default


def meta_set(conn, key, value):
    conn.execute('INSERT INTO meta(key, value) VALUES (?, ?) '
                 'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                 (key, json.dumps(value)))
    conn.commit()


def add_log(conn, job_id, line, level='info'):
    conn.execute('INSERT INTO job_log(job_id, ts, level, line) VALUES (?, ?, ?, ?)',
                 (job_id, now_str(), level, line[:2000]))


# ---------------------------------------------------------------------------
# Tile math shared by dashboard (estimates) and worker (job planning).
# Mirrors Lizmap\CliHelpers\WMTSCache::seed() so counts match Lizmap's output.
# ---------------------------------------------------------------------------
TILE_SIZE = 256.0


def parse_bbox(value):
    """'x1,y1,x2,y2' -> [x1, y1, x2, y2] floats, or None. Raises ValueError."""
    if value in (None, ''):
        return None
    parts = [p.strip() for p in str(value).split(',')]
    if len(parts) != 4:
        raise ValueError('bbox needs 4 comma separated numbers')
    nums = [float(p) for p in parts]
    if any(math.isnan(n) or math.isinf(n) for n in nums):
        raise ValueError('bbox contains invalid numbers')
    if nums[2] <= nums[0] or nums[3] <= nums[1]:
        raise ValueError('bbox must be xmin,ymin,xmax,ymax with xmax>xmin and ymax>ymin')
    return nums


def level_tiles(tms_level, limit, bbox=None):
    """Number of tiles Lizmap will generate for one level (optionally in a bbox)."""
    if not bbox:
        return limit['tiles']
    res = tms_level['resolution']
    min_col = math.floor((bbox[0] - tms_level['left']) / (TILE_SIZE * res))
    max_col = math.floor((bbox[2] - tms_level['left']) / (TILE_SIZE * res))
    min_row = math.floor((tms_level['top'] - bbox[3]) / (TILE_SIZE * res))
    max_row = math.floor((tms_level['top'] - bbox[1]) / (TILE_SIZE * res))
    min_row = max(min_row, limit['min_row'])
    min_col = max(min_col, limit['min_col'])
    max_row = min(max_row, limit['max_row'])
    max_col = min(max_col, limit['max_col'])
    if max_row < min_row or max_col < min_col:
        return 0
    return (max_row - min_row + 1) * (max_col - min_col + 1)


def find_tile_layer(caps, layer_name):
    for tl in (caps or {}).get('tile_layers') or []:
        if tl.get('name') == layer_name:
            return tl
    return None


def plan_levels(caps, layer_name, tms_ref, zmin, zmax, bbox=None):
    """[(level_id, tiles)] for the levels of a seed request that have tiles.
    Raises ValueError if layer/tms do not exist."""
    tl = find_tile_layer(caps, layer_name)
    if not tl:
        raise ValueError('layer is not seedable (not in the tile capabilities): %s' % layer_name)
    link = next((l for l in tl['tile_matrix_sets'] if l['ref'] == tms_ref), None)
    if not link:
        raise ValueError('tile matrix set %s not available for layer %s' % (tms_ref, layer_name))
    tms = next((t for t in caps.get('tile_matrix_sets') or [] if t['ref'] == tms_ref), None)
    if not tms:
        raise ValueError('tile matrix set %s unknown' % tms_ref)
    levels = {lv['id']: lv for lv in tms['levels']}
    plan = []
    for limit in link['limits']:
        z = limit['id']
        if zmin <= z <= zmax and z in levels:
            n = level_tiles(levels[z], limit, bbox)
            if n > 0:
                plan.append((z, n))
    return plan


def cache_dir_name(tms_ref):
    """Lizmap's file-cache sub directory for a tile matrix set ('EPSG:3857' -> 'EPSG_3857')."""
    return tms_ref.replace(':', '_')


# ---------------------------------------------------------------------------
# Settings
#
# Two levels, on purpose:
#  * boundaries — in a root-owned file read only by the worker
#    (/etc/lizmap-cache-worker.json): where Lizmap instances may live and
#    whether pools may be non-local. Dashboard users cannot widen them, so they
#    can neither make the worker run PHP from an arbitrary directory nor send
#    requests to arbitrary hosts.
#  * settings — everything operational (instances, pools, time windows, load
#    limits, intervals); edited in the dashboard, stored in meta 'settings'.
#    The worker validates them against its boundaries before using them.
# ---------------------------------------------------------------------------
BOUNDARY_DEFAULTS = {
    'instances_base': ['/var/www'],     # instances must be <base>/<name>/lizmap
    'allow_remote_pools': False,         # pools only on localhost
}

SETTINGS_DEFAULTS = {
    'instances': [],                     # [{"name", "lizmap_path"}]
    'pools': [],                         # [{"name", "url", "monitor_pool"}]
    'default_pool': None,                # pool name; None = each instance's own wmsServerURL
    'windows': [],                       # ["20:00-06:00"]; empty = always allowed
    'load_pause': {
        'enabled': True,
        'cpu_percent': 85,
        'user_p95_ms': 5000,
        'watch_pools': None,             # None = all dashboard pools except seeding pools
        'resume_below_factor': 0.8,
        'resume_after_s': 120,
        'check_interval_s': 5,
    },
    'inventory_refresh_hours': 24,
    'size_scan_interval_min': 360,
    'job_history_days': 30,
    'log_lines_per_job': 3000,
}

NAME_RE = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')


def parse_window(text):
    m = re.match(r'^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$', str(text))
    if not m:
        raise ValueError('Zeitfenster „%s“ ungültig (Format HH:MM-HH:MM)' % text)
    a = int(m.group(1)) * 60 + int(m.group(2))
    b = int(m.group(3)) * 60 + int(m.group(4))
    if a >= 24 * 60 or b > 24 * 60 or int(m.group(2)) > 59 or int(m.group(4)) > 59 or a == b:
        raise ValueError('Zeitfenster „%s“ ungültig' % text)
    return a, b


def is_loopback_url(url):
    host = (urlparse(url).hostname or '').lower()
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def path_is_under(path, bases):
    real = os.path.realpath(path)
    for base in bases:
        b = os.path.realpath(base)
        if real == b or real.startswith(b.rstrip('/') + '/'):
            return True
    return False


def check_instance_path(path, boundaries, check_files=True):
    """Raises ValueError unless path is <base>/<name>/lizmap below an allowed base."""
    if not path or not os.path.isabs(path):
        raise ValueError('Lizmap-Pfad muss absolut sein: %s' % path)
    if not path_is_under(path, boundaries['instances_base']):
        raise ValueError('Lizmap-Pfad liegt außerhalb der erlaubten Ordner (%s): %s'
                         % (', '.join(boundaries['instances_base']), path))
    if check_files and not os.path.isfile(os.path.join(path, 'application.init.php')):
        raise ValueError('keine Lizmap-Installation (application.init.php fehlt): %s' % path)


def check_pool_url(url, boundaries):
    if not isinstance(url, str) or not re.match(r'^https?://[^\s/]+', url):
        raise ValueError('Pool-URL muss mit http:// oder https:// beginnen: %s' % url)
    if not boundaries.get('allow_remote_pools') and not is_loopback_url(url):
        raise ValueError('Pool-URL muss auf localhost zeigen (entfernte Pools sind in '
                         '/etc/lizmap-cache-worker.json nicht freigegeben): %s' % url)


def _num(value, name, lo, hi, cast=float):
    try:
        v = cast(value)
    except (TypeError, ValueError):
        raise ValueError('%s muss eine Zahl sein' % name)
    if not (lo <= v <= hi):
        raise ValueError('%s muss zwischen %s und %s liegen' % (name, lo, hi))
    return v


def validate_settings(raw, boundaries, check_files=True):
    """Validate dashboard settings against the worker's boundaries.
    Returns (clean_settings, errors). Invalid entries are dropped, not guessed."""
    boundaries = dict(BOUNDARY_DEFAULTS, **(boundaries or {}))
    raw = raw if isinstance(raw, dict) else {}
    s = copy.deepcopy(SETTINGS_DEFAULTS)
    errors = []

    seen = set()
    for inst in raw.get('instances') or []:
        try:
            name = str(inst.get('name') or '').strip()
            path = str(inst.get('lizmap_path') or '').strip().rstrip('/')
            if not NAME_RE.match(name):
                raise ValueError('Instanzname ungültig: „%s“' % name)
            if name in seen:
                raise ValueError('Instanzname doppelt: %s' % name)
            check_instance_path(path, boundaries, check_files)
            seen.add(name)
            s['instances'].append({'name': name, 'lizmap_path': path})
        except (ValueError, AttributeError) as e:
            errors.append(str(e))

    pool_names = set()
    for p in raw.get('pools') or []:
        try:
            name = str(p.get('name') or '').strip()
            url = str(p.get('url') or '').strip()
            if not name or len(name) > 60:
                raise ValueError('Poolname fehlt oder ist zu lang')
            if name in pool_names:
                raise ValueError('Poolname doppelt: %s' % name)
            check_pool_url(url, boundaries)
            mp = p.get('monitor_pool') or None
            if mp is not None and not NAME_RE.match(str(mp)):
                raise ValueError('Dashboard-Pool ungültig: %s' % mp)
            pool_names.add(name)
            s['pools'].append({'name': name, 'url': url, 'monitor_pool': mp})
        except (ValueError, AttributeError) as e:
            errors.append(str(e))

    dp = raw.get('default_pool') or None
    if dp and dp not in pool_names:
        errors.append('Standard-Pool „%s“ existiert nicht' % dp)
        dp = None
    s['default_pool'] = dp

    for w in raw.get('windows') or []:
        try:
            parse_window(w)
            s['windows'].append(str(w).strip())
        except ValueError as e:
            errors.append(str(e))

    lp = raw.get('load_pause') or {}
    s['load_pause']['enabled'] = bool(lp.get('enabled', True))
    # each limit on its own: one bad value must not reset the others to defaults
    for key, name, lo, hi, cast in (('cpu_percent', 'CPU-Grenze', 10, 100, float),
                                    ('user_p95_ms', 'p95-Grenze', 0, 600000, float),
                                    ('resume_after_s', 'Fortsetzen nach', 0, 3600, int),
                                    ('check_interval_s', 'Prüfintervall', 1, 300, int)):
        if lp.get(key) is not None:
            try:
                s['load_pause'][key] = _num(lp[key], name, lo, hi, cast)
            except ValueError as e:
                errors.append(str(e))
    wp = lp.get('watch_pools')
    s['load_pause']['watch_pools'] = [str(x) for x in wp] if isinstance(wp, list) and wp else None

    for key, name, lo, hi in (('inventory_refresh_hours', 'Inventar-Intervall', 0, 24 * 30),
                              ('size_scan_interval_min', 'Größenmessung', 10, 60 * 24 * 7),
                              ('job_history_days', 'Job-Verlauf', 1, 365)):
        if key in raw:
            try:
                s[key] = _num(raw[key], name, lo, hi, int)
            except ValueError as e:
                errors.append(str(e))
    return s, errors
