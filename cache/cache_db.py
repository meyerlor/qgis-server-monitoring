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

import json
import math
import os
import sqlite3
from datetime import datetime

SCHEMA_VERSION = 1

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
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                 (str(SCHEMA_VERSION),))
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
