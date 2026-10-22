#!/usr/bin/env python3
"""
Lizmap tile cache worker for the QGIS Server Monitoring Dashboard.

Runs as its own systemd service AS THE LIZMAP PHP USER (e.g. www-data), so the
tiles it writes have the same owner as the ones Lizmap writes itself. It
executes the jobs the dashboard puts into the shared cache DB:

  refresh  read projects + tile capabilities of every configured Lizmap instance
  seed     generate tiles for one layer / tile matrix set / level range
  clean    delete the cache of one layer

Only one job runs at a time. Seeding is split into one step per zoom level,
which gives an exact per-level ledger and lets an interrupted job resume at the
level where it stopped. A level is rendered by several helper processes in
parallel (each takes every n-th tile), as many as the pool is set up for. A
running seed job is paused (SIGSTOP) while the server is busy or outside the
configured time window, and continued (SIGCONT) afterwards. Tile requests can
be routed to a dedicated QGIS Server pool.

Instances, pools, time windows and load limits are edited in the dashboard
(stored in the cache DB). The optional, root-owned config file only sets the
boundaries dashboard users cannot widen (where instances may live, whether
pools may be non-local) plus paths. Besides the queue, two light tasks run in
a side thread so they never wait behind a long seeding job:

  discover   find Lizmap installs below the allowed base directories
  pool_test  check that a QGIS Server answers at a pool URL

stdlib only. Usage:
    cache_worker.py [--config /etc/lizmap-cache-worker.json]
    cache_worker.py --config ... check        # validate setup + instances, then exit

Copyright (c) 2025 meyerlor - MIT License (see LICENSE)
"""

import argparse
import copy
import getpass
import json
import os
import queue
import re
import selectors
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cache_db as cdb  # noqa: E402

VERSION = '1.1'
DEFAULT_CONFIG_PATH = '/etc/lizmap-cache-worker.json'

# Root-owned file: paths and boundaries only. Everything else is a dashboard setting.
FILE_DEFAULTS = {
    'db_path': '/var/lib/lizmap-cache/cache.db',
    'php': '/usr/bin/php',
    'helper_path': os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lizmap_cache_helper.php'),
    'step_timeout_s': 600,       # for refresh steps (capabilities); seeding has no timeout
    'instances_base': list(cdb.BOUNDARY_DEFAULTS['instances_base']),
    'allow_remote_pools': cdb.BOUNDARY_DEFAULTS['allow_remote_pools'],
}
SIDE_KINDS = ('discover', 'pool_test')   # run in the side thread, never queued behind seeding

CAPS_VERSION = 2                # = CAPS_VERSION of lizmap_cache_helper.php; older stored caps are re-read
MAX_LOGGED_TILE_ERRORS = 50     # per level; further errors are only counted
MANUAL_RESUME_GRACE_S = 1800    # a manual "continue" overrides the load pause this long
EARLY_ABORT_ERRORS = 200        # abort a level that fails (almost) every tile

PRIO_CLEAN = 20
PRIO_MANUAL = 10
PRIO_REFRESH = 5
PRIO_SCHEDULE = 0

RE_PROGRESS = re.compile(r'^Progression: (\d+)%, (\d+) tiles generated on (\d+) tiles')
RE_TILE_ERROR = re.compile(r'^Error for tile: ')
RE_NOT_CACHED = re.compile(r'^Error, tile not cached: ')
RE_HELPER_ERROR = re.compile(r'^Error: (.*)')
RE_IDENTITY = re.compile(r'^Seeding (as technical user|anonymously)')
RE_NOISE = re.compile(r"^(The TileMatrixSet '|\d+ tiles to generate for )")   # once per process; the worker logs the level itself


def log(msg):
    print('[%s] %s' % (cdb.now_str(), msg), flush=True)


def caps_version(caps_json):
    """Helper version that produced stored capabilities (0 = before versioning)."""
    try:
        return int(json.loads(caps_json or '{}').get('caps_version') or 0)
    except (ValueError, TypeError, AttributeError):
        return 0


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
class FileConfig:
    """The optional root-owned config file: paths + boundaries.
    Older files may still carry instances/pools/...; those seed the dashboard
    settings once (when the DB has none yet)."""

    def __init__(self, path):
        self.path = path
        self.exists = os.path.isfile(path)
        raw = {}
        if self.exists:
            with open(path, encoding='utf-8') as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError('config file must contain a JSON object')
        data = copy.deepcopy(FILE_DEFAULTS)
        for k in FILE_DEFAULTS:
            if k in raw:
                data[k] = raw[k]
        if isinstance(data['instances_base'], str):
            data['instances_base'] = [data['instances_base']]
        if not data['instances_base'] or not all(isinstance(b, str) and os.path.isabs(b) for b in data['instances_base']):
            raise ValueError('instances_base must be a list of absolute directories')
        data['allow_remote_pools'] = bool(data['allow_remote_pools'])
        self.__dict__.update(data)
        self.legacy_settings = {k: raw[k] for k in cdb.SETTINGS_DEFAULTS if k in raw}

    @property
    def boundaries(self):
        return {'instances_base': self.instances_base, 'allow_remote_pools': self.allow_remote_pools}


class Config:
    """Effective configuration = file (paths, boundaries) + validated dashboard settings."""

    def __init__(self, fcfg, settings):
        self.file = fcfg
        self.path = fcfg.path
        for k in FILE_DEFAULTS:
            setattr(self, k, getattr(fcfg, k))
        self.__dict__.update(copy.deepcopy(settings))
        self.windows_parsed = [cdb.parse_window(w) for w in self.windows]

    def instance(self, name):
        return next((i for i in self.instances if i['name'] == name), None)

    def pool(self, name):
        return next((p for p in self.pools if p['name'] == name), None)

    def pool_url(self, name):
        """Resolve a job's pool to a URL. Returns (url or None, error or None)."""
        name = name or self.default_pool
        if not name:
            return None, None
        p = self.pool(name)
        if not p:
            return None, 'unbekannter Seeding-Pool: %s' % name
        return p['url'], None

    def parallel(self, name):
        """Seeding processes per level for a job's pool (read at every level, so changes apply live)."""
        p = self.pool(name or self.default_pool) if (name or self.default_pool) else None
        return int(p.get('parallel') or 1) if p else int(self.instance_parallel or 1)

    def summary(self):
        return {
            'pools': [{'name': p['name'], 'url': p['url'], 'monitor_pool': p.get('monitor_pool'),
                       'parallel': p.get('parallel')} for p in self.pools],
            'default_pool': self.default_pool,
            'instance_parallel': self.instance_parallel,
            'windows': self.windows,
            'load_pause': self.load_pause,
            'instances': [i['name'] for i in self.instances],
            'size_scan_interval_min': self.size_scan_interval_min,
            'inventory_refresh_hours': self.inventory_refresh_hours,
            'boundaries': self.file.boundaries,
            'config_path': self.path,
            'config_file_exists': self.file.exists,
        }


def in_windows(windows, now=None):
    if not windows:
        return True
    now = now or datetime.now()
    minute = now.hour * 60 + now.minute
    for a, b in windows:
        if a <= b:
            if a <= minute < b:
                return True
        elif minute >= a or minute < b:   # crosses midnight
            return True
    return False


# ---------------------------------------------------------------------------
# Load measurement
# ---------------------------------------------------------------------------
class CpuSampler:
    """Machine-wide CPU busy % between two calls, from /proc/stat."""

    def __init__(self):
        self.prev = self._read()

    @staticmethod
    def _read():
        try:
            with open('/proc/stat') as f:
                parts = f.readline().split()[1:]
            vals = [int(x) for x in parts]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            return sum(vals), idle
        except (OSError, ValueError, IndexError):
            return None

    def sample(self):
        cur = self._read()
        if not cur or not self.prev:
            self.prev = cur
            return None
        total = cur[0] - self.prev[0]
        idle = cur[1] - self.prev[1]
        self.prev = cur
        if total <= 0:
            return None
        return round(100.0 * (total - idle) / total, 1)


# ---------------------------------------------------------------------------
# Child process (one helper call) with non-blocking line reading
# ---------------------------------------------------------------------------
class Child:
    def __init__(self, args):
        self.args = args
        self.proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        os.set_blocking(self.proc.stdout.fileno(), False)
        self.buf = b''
        self.started = time.time()
        self.last_output = time.time()
        self.stopped = False

    def fileno(self):
        return self.proc.stdout.fileno()

    def read_lines(self):
        lines = []
        while True:
            try:
                chunk = os.read(self.fileno(), 65536)
            except BlockingIOError:
                break
            except OSError:
                break
            if not chunk:
                break
            self.buf += chunk
            self.last_output = time.time()
        while b'\n' in self.buf:
            line, self.buf = self.buf.split(b'\n', 1)
            lines.append(line.decode('utf-8', 'replace').rstrip('\r'))
        return lines

    def flush_rest(self):
        lines = self.read_lines()
        if self.buf:
            lines.append(self.buf.decode('utf-8', 'replace'))
            self.buf = b''
        return lines

    def poll(self):
        return self.proc.poll()

    def signal(self, sig):
        try:
            os.killpg(self.proc.pid, sig)
        except ProcessLookupError:
            pass

    def pause(self):
        if not self.stopped:
            self.signal(signal.SIGSTOP)
            self.stopped = True

    def resume(self):
        if self.stopped:
            self.signal(signal.SIGCONT)
            self.stopped = False


class ChildGroup:
    """The helper processes of one step (several for a parallel seeding level),
    driven together: pause/resume/terminate all, done when all have exited."""

    def __init__(self, arg_lists):
        self.children = []
        try:
            for args in arg_lists:
                self.children.append(Child(args))
        except OSError:
            self.terminate(grace=2)
            raise
        self.started = time.time()
        self.stopped = False

    @property
    def pids(self):
        return [c.proc.pid for c in self.children]

    @property
    def streams(self):
        return [c.proc.stdout for c in self.children]

    def read_lines(self):
        """[(child index, line)]"""
        return [(i, line) for i, c in enumerate(self.children) for line in c.read_lines()]

    def flush_rest(self):
        return [(i, line) for i, c in enumerate(self.children) for line in c.flush_rest()]

    def poll(self):
        """None while any process runs, then the first non-zero exit code (or 0)."""
        codes = [c.poll() for c in self.children]
        if any(rc is None for rc in codes):
            return None
        return next((rc for rc in codes if rc != 0), 0)

    def pause(self):
        if not self.stopped:
            for c in self.children:
                c.pause()
            self.stopped = True

    def resume(self):
        if self.stopped:
            for c in self.children:
                c.resume()
            self.stopped = False

    def terminate(self, grace=10):
        for c in self.children:
            c.resume()
            c.signal(signal.SIGTERM)
        deadline = time.time() + grace
        while time.time() < deadline and any(c.proc.poll() is None for c in self.children):
            time.sleep(0.2)
        for c in self.children:
            if c.proc.poll() is None:
                c.signal(signal.SIGKILL)
                c.proc.wait(5)


# ---------------------------------------------------------------------------
# Cache size scanning (own thread + own DB connection; I/O heavy on big caches)
# ---------------------------------------------------------------------------
class SizeScanner(threading.Thread):
    def __init__(self, db_path):
        super().__init__(name='size-scanner', daemon=True)
        self.db_path = db_path
        self.q = queue.Queue()
        self.pending = set()
        self.lock = threading.Lock()

    def enqueue(self, target):
        key = target[:5]
        with self.lock:
            if key in self.pending:
                return
            self.pending.add(key)
        self.q.put(target)

    @staticmethod
    def measure(path):
        total = files = 0
        stack = [path]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                stack.append(e.path)
                            elif e.is_file(follow_symlinks=False):
                                total += e.stat(follow_symlinks=False).st_size
                                files += 1
                        except OSError:
                            pass
            except OSError:
                pass
        return total, files

    def run(self):
        conn = cdb.connect(self.db_path)
        while True:
            target = self.q.get()
            instance, repo, project, layer, tms, storage, root, cache_dir = target
            try:
                if storage == 'file':
                    path = os.path.join(root, repo, project, cache_dir, cdb.cache_dir_name(tms))
                    size, files = self.measure(path) if os.path.isdir(path) else (0, 0)
                elif storage == 'sqlite':
                    path = os.path.join(root, repo, project, '%s_%s.db' % (cache_dir, cdb.cache_dir_name(tms)))
                    size, files = (os.path.getsize(path), None) if os.path.isfile(path) else (0, 0)
                else:
                    size, files = None, None      # redis: not measurable from disk
                conn.execute(
                    'INSERT INTO layer_sizes(instance, repository, project, layer, tms, bytes, files, scanned_at) '
                    'VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(instance, repository, project, layer, tms) DO UPDATE SET '
                    'bytes=excluded.bytes, files=excluded.files, scanned_at=excluded.scanned_at',
                    (instance, repo, project, layer, tms, size, files, cdb.now_str()))
                conn.commit()
            except Exception as e:   # never let the scanner die
                log('size scan failed for %s/%s/%s %s: %s' % (repo, project, layer, tms, e))
            finally:
                with self.lock:
                    self.pending.discard(target[:5])


# ---------------------------------------------------------------------------
# Side jobs: instance discovery and pool tests (own thread + own DB connection)
# ---------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A pool test must not be redirected elsewhere (would bypass the localhost boundary)."""

    def redirect_request(self, *args, **kwargs):
        return None


class SideRunner(threading.Thread):
    def __init__(self, worker):
        super().__init__(name='side-runner', daemon=True)
        self.fcfg = worker.fcfg
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)

    def run(self):
        conn = cdb.connect(self.fcfg.db_path)
        while True:
            try:
                row = conn.execute("SELECT * FROM jobs WHERE status = 'queued' AND kind IN (?, ?) ORDER BY id LIMIT 1",
                                   SIDE_KINDS).fetchone()
                if not row:
                    time.sleep(1)
                    continue
                self.execute(conn, dict(row))
            except Exception:
                log('side runner error:\n' + traceback.format_exc())
                time.sleep(2)

    @staticmethod
    def _log(conn, jid, line, level='info'):
        cdb.add_log(conn, jid, line, level)
        conn.commit()

    def execute(self, conn, job):
        jid = job['id']
        cur = conn.execute("UPDATE jobs SET status='running', started_at=? WHERE id=? AND status='queued'",
                           (cdb.now_str(), jid))
        conn.commit()
        if cur.rowcount == 0:      # cancelled in the meantime
            return
        try:
            params = json.loads(job.get('params') or '{}')
        except ValueError:
            params = {}
        try:
            if job['kind'] == 'discover':
                status, msg = self.discover(conn, jid)
            else:
                status, msg = self.pool_test(conn, jid, params.get('url'))
        except Exception as e:
            status, msg = 'failed', str(e)
        conn.execute('UPDATE jobs SET status=?, finished_at=?, message=? WHERE id=?',
                     (status, cdb.now_str(), msg, jid))
        conn.commit()
        self._log(conn, jid, '%s: %s' % ('Fertig' if status == 'done' else 'Fehlgeschlagen', msg),
                  'info' if status == 'done' else 'error')

    def helper_env(self, lizmap_path):
        try:
            out = subprocess.run([self.fcfg.php, self.fcfg.helper_path, '--lizmap=%s' % lizmap_path, 'env'],
                                 capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as e:
            return None, str(e)
        for line in reversed(out.stdout.strip().splitlines()):
            if line.startswith('{'):
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                return (None, data['error']) if data.get('error') else (data, None)
        return None, 'keine Antwort vom Helper (Exit-Code %s) %s' % (out.returncode, (out.stderr or '')[-300:])

    def discover(self, conn, jid):
        found = []
        for base in self.fcfg.instances_base:
            if not os.path.isdir(base):
                self._log(conn, jid, 'Ordner nicht gefunden: %s' % base, 'warn')
                continue
            for entry in sorted(os.listdir(base)):
                path = os.path.join(base, entry, 'lizmap')
                if os.path.isfile(os.path.join(path, 'application.init.php')):
                    found.append((entry, path))
        self._log(conn, jid, '%d Lizmap-Installation(en) unter %s gefunden' % (len(found), ', '.join(self.fcfg.instances_base)))
        candidates = []
        for entry, path in found:
            name = re.sub(r'[^A-Za-z0-9_.-]', '_', entry)[:64]
            env, err = self.helper_env(path)
            candidates.append({'name': name, 'lizmap_path': path, 'env': env, 'error': err})
            if err:
                self._log(conn, jid, '%s: %s' % (name, err), 'error')
            else:
                self._log(conn, jid, '%s: Lizmap %s, QGIS %s, Cache %s' % (
                    name, env.get('lizmap_version'), env.get('wms_server_url'), env.get('cache_storage_type')))
        cdb.meta_set(conn, 'discovery', {'ts': cdb.now_str(), 'ts_epoch': time.time(),
                                         'bases': self.fcfg.instances_base, 'candidates': candidates})
        ok = sum(1 for c in candidates if not c['error'])
        return 'done', '%d Instanz(en) gefunden%s' % (ok, ', %d mit Fehler' % (len(candidates) - ok) if ok < len(candidates) else '')

    def pool_test(self, conn, jid, url):
        cdb.check_pool_url(url, self.fcfg.boundaries)
        # Best test: let Lizmap ask the pool for a real project (same MAP path as seeding).
        row = conn.execute('SELECT p.repository, p.project, i.lizmap_path, i.name FROM projects p '
                           'JOIN instances i ON i.name = p.instance '
                           'WHERE p.caps_json IS NOT NULL AND p.error IS NULL '
                           'ORDER BY p.instance, p.repository, p.project LIMIT 1').fetchone()
        if row:
            return self.pool_test_project(conn, jid, url, row)
        return self.pool_test_plain(conn, jid, url)

    def pool_test_project(self, conn, jid, url, row):
        target = '%s: %s/%s' % (row['name'], row['repository'], row['project'])
        self._log(conn, jid, 'Teste %s mit Projekt %s' % (url, target))
        try:
            out = subprocess.run([self.fcfg.php, self.fcfg.helper_path, '--lizmap=%s' % row['lizmap_path'],
                                  '--wms-url=%s' % url, 'wmsping', row['repository'], row['project']],
                                 capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
            data = json.loads(out.stdout.strip().splitlines()[-1])
        except (OSError, subprocess.TimeoutExpired, ValueError, IndexError) as e:
            return 'failed', 'Test fehlgeschlagen: %s' % e
        if data.get('error'):
            return 'failed', data['error']
        groups = data.get('seed_groups') or []
        as_groups = (', als Gruppen %s' % ', '.join(groups)) if groups else ''
        if data.get('ok'):
            return 'done', 'QGIS Server liefert %s (HTTP %d, %d ms%s)' % (target, data['code'], data['ms'], as_groups)
        if data.get('request_error'):
            return 'failed', 'nicht erreichbar: %s' % data['request_error']
        return 'failed', 'kein gültiges GetCapabilities für %s (MAP=%s%s): %s' % (
            target, data.get('map'), as_groups, data.get('message') or data.get('snippet') or '')

    def pool_test_plain(self, conn, jid, url):
        """Without an inventory there is no project to ask for: reachability only."""
        test = url + ('&' if '?' in url else '?') + 'SERVICE=WMS&REQUEST=GetCapabilities&VERSION=1.3.0'
        self._log(conn, jid, 'Teste %s' % test)
        t0 = time.time()
        req = urllib.request.Request(test, headers={'User-Agent': 'lizmap-cache-worker/%s' % VERSION})
        try:
            with self.opener.open(req, timeout=10) as r:
                code, body, server = r.status, r.read(65536), r.headers.get('Server', '')
        except urllib.error.HTTPError as e:
            code, server = e.code, (e.headers.get('Server', '') if e.headers else '')
            body = e.read(65536) if e.fp else b''
        except (urllib.error.URLError, OSError, ValueError) as e:
            return 'failed', 'nicht erreichbar: %s' % getattr(e, 'reason', e)
        ms = int((time.time() - t0) * 1000)
        text = body.decode('utf-8', 'replace')
        markers = ('ServiceException' in text or 'WMS_Capabilities' in text
                   or 'qgis' in text.lower() or 'qgis' in server.lower() or 'qjazz' in server.lower())
        snippet = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', text)).strip()[:120]
        if 300 <= code < 400:
            return 'failed', 'leitet weiter (HTTP %d) – bitte die Ziel-URL direkt eintragen' % code
        if markers:
            return 'done', 'QGIS Server antwortet (HTTP %d, %d ms)' % (code, ms)
        if code < 500:
            # e.g. qjazz answers 400 "Missing project" without MAP
            return 'done', ('erreichbar (HTTP %d, %d ms: %s) – vollständige Prüfung mit einem Projekt, '
                            'sobald Instanzen eingelesen sind' % (code, ms, snippet or '–'))
        return 'failed', 'Serverfehler (HTTP %d, %d ms): %s' % (code, ms, snippet)


# ---------------------------------------------------------------------------
# Running job state
# ---------------------------------------------------------------------------
class RunningJob:
    def __init__(self, row):
        self.id = row['id']
        self.row = dict(row)
        self.kind = row['kind']
        self.steps = deque()
        self.step = None          # dict of the step being executed
        self.child = None
        self.output = []          # stdout of a JSON step
        self.tiles_done_before = 0
        self.errors = 0
        self.last_db_update = 0
        self.last_message = None
        self.load_ok_since = None
        self.load_override_until = 0   # manual "continue": load pause suspended until then
        self.wms_url = None        # pool override for the helper, None = instance URL
        self.force_caps = False
        self.caps_total = 0
        self.image_format = None


def load_settings(conn, fcfg):
    """Dashboard settings from the DB, validated against the file's boundaries.
    On first start an older config file's instances/pools/... seed them."""
    raw = cdb.meta_get(conn, 'settings')
    if raw is None:
        raw = copy.deepcopy(fcfg.legacy_settings)
        if raw:
            cdb.meta_set(conn, 'settings', raw)
            cdb.meta_set(conn, 'settings_rev', 1)
    return cdb.validate_settings(raw or {}, fcfg.boundaries)


class Worker:
    def __init__(self, fcfg):
        self.fcfg = fcfg
        self.conn = cdb.connect(fcfg.db_path)
        cdb.init_schema(self.conn)
        settings, errors = load_settings(self.conn, fcfg)
        self.cfg = Config(fcfg, settings)
        self.settings_errors = errors
        self.settings_rev = cdb.meta_get(self.conn, 'settings_rev', 0)
        self.cpu = CpuSampler()
        self.cur = None
        self.started_at = cdb.now_str()
        self.stop_requested = False
        self.last = {'heartbeat': 0, 'schedule': 0, 'load': 0, 'maintenance': 0, 'size_full': 0, 'settings': 0}
        self.cpu_value = None
        self.p95_value = None
        self.scanner = SizeScanner(fcfg.db_path)
        self.scanner.start()
        self.side = SideRunner(self)
        self.side.start()
        self.sel = selectors.DefaultSelector()

    # -- infrastructure ----------------------------------------------------
    def db(self, sql, args=()):
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur

    def job_log(self, job_id, line, level='info'):
        cdb.add_log(self.conn, job_id, line, level)
        self.conn.commit()

    def update_job(self, job_id, **fields):
        if not fields:
            return
        cols = ', '.join('%s = ?' % k for k in fields)
        self.db('UPDATE jobs SET %s WHERE id = ?' % cols, list(fields.values()) + [job_id])
        if 'status' in fields:
            self.heartbeat(force=True)   # state changes reach the dashboard immediately

    def heartbeat(self, force=False):
        if not force and time.time() - self.last['heartbeat'] < 5:
            return
        self.last['heartbeat'] = time.time()
        state = 'idle'
        pause_reason = None
        if self.cur:
            state = 'paused' if self.cur.child and self.cur.child.stopped else 'running'
            pause_reason = self.cur.row.get('pause_reason') if state == 'paused' else None
        cdb.meta_set(self.conn, 'worker', {
            'pid': os.getpid(),
            'user': getpass.getuser(),
            'version': VERSION,
            'started_at': self.started_at,
            'last_seen': cdb.now_str(),
            'last_seen_epoch': time.time(),   # liveness check is timezone independent
            'state': state,
            'job_id': self.cur.id if self.cur else None,
            'pause_reason': pause_reason,
            'cpu_percent': self.cpu_value,
            'user_p95_ms': self.p95_value,
            'in_window': in_windows(self.cfg.windows_parsed),
        })

    # -- startup -----------------------------------------------------------
    def recover(self):
        """Jobs that were running when the worker stopped are resumed: seeding
        skips the levels this job already recorded in the ledger."""
        rows = self.db("SELECT id, kind FROM jobs WHERE status = 'running'").fetchall()
        for r in rows:
            if r['kind'] in SIDE_KINDS:
                self.update_job(r['id'], status='failed', finished_at=cdb.now_str(), message='Worker neu gestartet')
                continue
            self.update_job(r['id'], status='queued', pid=None)
            self.job_log(r['id'], 'Worker neu gestartet – Job wird fortgesetzt', 'warn')
        self.apply_settings(initial=True)

    def apply_settings(self, initial=False):
        """Publish the effective config and sync the instances table with it."""
        cdb.meta_set(self.conn, 'worker_config', self.cfg.summary())
        cdb.meta_set(self.conn, 'settings_errors', self.settings_errors)
        before = {r['name'] for r in self.db('SELECT name FROM instances').fetchall()}
        for inst in self.cfg.instances:
            self.db('INSERT INTO instances(name, lizmap_path) VALUES (?, ?) '
                    'ON CONFLICT(name) DO UPDATE SET lizmap_path = excluded.lizmap_path',
                    (inst['name'], inst['lizmap_path']))
        names = [i['name'] for i in self.cfg.instances]
        removed = before - set(names)
        for name in removed:
            # projects of instances no longer managed disappear from the tab
            self.db('DELETE FROM projects WHERE instance = ?', (name,))
            self.db('DELETE FROM instances WHERE name = ?', (name,))
        never = [r['name'] for r in self.db('SELECT name FROM instances WHERE refreshed_at IS NULL').fetchall()]
        for name in never:
            if not self.db("SELECT 1 FROM jobs WHERE kind='refresh' AND status IN (?,?,?) AND (instance = ? OR instance IS NULL)",
                           cdb.ACTIVE_STATUSES + (name,)).fetchone():
                self.enqueue({'kind': 'refresh', 'instance': name, 'priority': PRIO_REFRESH,
                              'created_by': 'Worker (Start)' if initial else 'Worker (neue Instanz)'})
        if self.settings_errors:
            log('settings problems: ' + '; '.join(self.settings_errors))

    def check_settings(self):
        """Pick up settings saved in the dashboard (settings_rev is bumped on save)."""
        rev = cdb.meta_get(self.conn, 'settings_rev', 0)
        if rev == self.settings_rev:
            return
        self.settings_rev = rev
        settings, errors = load_settings(self.conn, self.fcfg)
        self.cfg = Config(self.fcfg, settings)
        self.settings_errors = errors
        self.apply_settings()
        log('settings revision %s applied: %d instance(s), %d pool(s), default pool %s, windows %s'
            % (rev, len(self.cfg.instances), len(self.cfg.pools), self.cfg.default_pool or '(instance URL)',
               self.cfg.windows or 'always'))
        if self.cur and self.cur.kind == 'seed' and self.cur.row.get('pool') and not self.cfg.pool(self.cur.row['pool']):
            self.job_log(self.cur.id, 'Hinweis: der Pool dieses Jobs wurde aus den Einstellungen entfernt '
                         '(der laufende Job nutzt ihn weiter)', 'warn')

    def enqueue(self, job):
        cols = ['kind', 'instance', 'repository', 'project', 'layer', 'tms', 'zmin', 'zmax', 'bbox',
                'force', 'pool', 'window_only', 'priority', 'schedule_id', 'created_by']
        vals = [job.get(c) for c in cols]
        cur = self.db('INSERT INTO jobs(%s, status, created_at) VALUES (%s, \'queued\', ?)'
                      % (', '.join(cols), ', '.join('?' * len(cols))), vals + [cdb.now_str()])
        return cur.lastrowid

    def active_job(self, kind, schedule_id=None):
        sql = 'SELECT id FROM jobs WHERE kind = ? AND status IN (?, ?, ?)'
        args = [kind] + list(cdb.ACTIVE_STATUSES)
        if schedule_id is not None:
            sql += ' AND schedule_id = ?'
            args.append(schedule_id)
        return self.db(sql, args).fetchone()

    # -- main loop ---------------------------------------------------------
    def run(self):
        signal.signal(signal.SIGTERM, self._on_term)
        signal.signal(signal.SIGINT, self._on_term)
        self.recover()
        log('worker %s started as %s, %d instance(s), pools: %s, default pool: %s, windows: %s'
            % (VERSION, getpass.getuser(), len(self.cfg.instances),
               [p['name'] for p in self.cfg.pools] or '-', self.cfg.default_pool or '(instance URL)',
               self.cfg.windows or 'always'))
        self.heartbeat(force=True)
        while not self.stop_requested:
            try:
                self.loop_once()
            except Exception:
                log('loop error:\n' + traceback.format_exc())
                time.sleep(2)
        self.shutdown()

    def _on_term(self, *_):
        self.stop_requested = True

    def loop_once(self):
        now = time.time()
        if self.cur and self.cur.child:
            events = self.sel.select(timeout=1.0)
            if events:
                self.pump()
        else:
            time.sleep(1.0)

        if now - self.last['settings'] >= 3:
            self.last['settings'] = now
            self.check_settings()
        self.handle_requested_actions()
        if self.cur:
            self.pump()
            self.check_child_exit()
        if now - self.last['load'] >= self.cfg.load_pause.get('check_interval_s', 5):
            self.last['load'] = now
            self.measure_load()
            self.apply_pause_rules()
        if now - self.last['schedule'] >= 60:
            self.last['schedule'] = now
            self.run_scheduler()
            self.maybe_auto_refresh()
        if now - self.last['maintenance'] >= 3600:
            self.last['maintenance'] = now
            self.maintenance()
        if now - self.last['size_full'] >= self.cfg.size_scan_interval_min * 60:
            self.last['size_full'] = now
            self.enqueue_all_size_scans()
        if not self.cur:
            self.start_next_job()
        self.heartbeat()

    def shutdown(self):
        log('stopping')
        if self.cur and self.cur.child:
            self.unregister(self.cur.child)
            self.cur.child.terminate()
            # resume after restart (completed levels are skipped)
            self.update_job(self.cur.id, status='queued', pid=None)
            self.job_log(self.cur.id, 'Worker gestoppt – Job wird nach dem Neustart fortgesetzt', 'warn')
        meta = cdb.meta_get(self.conn, 'worker', {}) or {}
        meta.update({'state': 'stopped', 'last_seen': cdb.now_str(), 'last_seen_epoch': time.time(), 'job_id': None})
        cdb.meta_set(self.conn, 'worker', meta)

    # -- requested actions from the dashboard ------------------------------
    def handle_requested_actions(self):
        rows = self.db("SELECT id, status, requested_action FROM jobs WHERE requested_action IS NOT NULL").fetchall()
        for r in rows:
            action, jid = r['requested_action'], r['id']
            self.update_job(jid, requested_action=None)
            running = self.cur and self.cur.id == jid
            if action == 'cancel':
                if running:
                    self.job_log(jid, 'Abbruch angefordert', 'warn')
                    self.abort_current('cancelled', None)
                elif r['status'] in ('queued', 'paused'):
                    self.update_job(jid, status='cancelled', finished_at=cdb.now_str(), message=None)
                    self.job_log(jid, 'Abgebrochen (noch nicht gestartet)', 'warn')
            elif action == 'pause':
                if running and self.cur.kind == 'seed' and self.cur.child:
                    self.cur.child.pause()
                    self.cur.row['pause_reason'] = 'user'
                    self.update_job(jid, status='paused', pause_reason='user')
                    self.job_log(jid, 'Pausiert (manuell)')
                elif r['status'] == 'queued':
                    self.update_job(jid, status='paused', pause_reason='user')
            elif action == 'resume':
                if running and self.cur.child and self.cur.child.stopped:
                    was_load = self.cur.row.get('pause_reason') == 'load'
                    self.cur.row['pause_reason'] = None
                    self.cur.load_ok_since = None
                    # otherwise the next load check (a few seconds later, same
                    # 5-minute p95) would pause the job again right away
                    self.cur.load_override_until = time.time() + MANUAL_RESUME_GRACE_S
                    self.cur.child.resume()
                    self.update_job(jid, status='running', pause_reason=None)
                    self.job_log(jid, 'Fortgesetzt (manuell)%s' % (
                        ' – Lastpause für %d min ausgesetzt' % (MANUAL_RESUME_GRACE_S // 60) if was_load else ''))
                elif r['status'] == 'paused':
                    self.update_job(jid, status='queued', pause_reason=None)

    # -- load / window pausing ---------------------------------------------
    def measure_load(self):
        self.cpu_value = self.cpu.sample()
        sig = cdb.meta_get(self.conn, 'load_signal')
        self.p95_value = None
        if sig and sig.get('ts_epoch'):
            # epoch, not local time: dashboard and worker may run with different TZ settings
            if 0 <= time.time() - float(sig['ts_epoch']) <= 120:
                pools = sig.get('pools') or {}
                watch = self.cfg.load_pause.get('watch_pools')
                if not watch:
                    seeding = {p.get('monitor_pool') for p in self.cfg.pools if p.get('monitor_pool')}
                    watch = [k for k in pools if k not in seeding]
                vals = [pools[k] for k in watch if pools.get(k) is not None]
                self.p95_value = max(vals) if vals else None

    def apply_pause_rules(self):
        cur = self.cur
        if not cur or cur.kind != 'seed' or not cur.child:
            return
        reason = cur.row.get('pause_reason')
        if reason == 'user':
            return
        lp = self.cfg.load_pause
        # time window
        if cur.row.get('window_only') and not in_windows(self.cfg.windows_parsed):
            if not cur.child.stopped:
                cur.child.pause()
                cur.row['pause_reason'] = 'window'
                self.update_job(cur.id, status='paused', pause_reason='window')
                self.job_log(cur.id, 'Pausiert: außerhalb des Zeitfensters (%s)' % ', '.join(self.cfg.windows))
            return
        if reason == 'window' and cur.child.stopped:
            cur.child.resume()
            cur.row['pause_reason'] = None
            self.update_job(cur.id, status='running', pause_reason=None)
            self.job_log(cur.id, 'Fortgesetzt: Zeitfenster geöffnet')
            return
        if not lp.get('enabled', True):
            return
        if time.time() < cur.load_override_until and reason != 'load':
            return
        cpu, p95 = self.cpu_value, self.p95_value
        too_busy = ((cpu is not None and cpu > lp['cpu_percent'])
                    or (p95 is not None and lp.get('user_p95_ms') and p95 > lp['user_p95_ms']))
        if not cur.child.stopped:
            if too_busy:
                cur.child.pause()
                cur.row['pause_reason'] = 'load'
                cur.load_ok_since = None
                self.update_job(cur.id, status='paused', pause_reason='load')
                self.job_log(cur.id, 'Pausiert wegen Serverlast (CPU %s %%, p95 Nutzer %s ms)'
                             % (cpu if cpu is not None else '?', int(p95) if p95 is not None else '?'), 'warn')
        elif reason == 'load':
            f = lp.get('resume_below_factor', 0.8)
            calm = ((cpu is None or cpu < lp['cpu_percent'] * f)
                    and (p95 is None or not lp.get('user_p95_ms') or p95 < lp['user_p95_ms'] * f))
            if calm:
                cur.load_ok_since = cur.load_ok_since or time.time()
                if time.time() - cur.load_ok_since >= lp.get('resume_after_s', 120):
                    cur.child.resume()
                    cur.row['pause_reason'] = None
                    self.update_job(cur.id, status='running', pause_reason=None)
                    self.job_log(cur.id, 'Fortgesetzt: Serverlast wieder normal')
            else:
                cur.load_ok_since = None

    # -- scheduling ----------------------------------------------------------
    def run_scheduler(self):
        now = datetime.now()
        rows = self.db('SELECT * FROM schedules WHERE enabled = 1').fetchall()
        for s in rows:
            due = cdb.parse_time(s['next_run'])
            if due and due > now:
                continue
            if self.active_job('seed', s['id']):
                continue
            jid = self.enqueue({
                'kind': 'seed', 'instance': s['instance'], 'repository': s['repository'],
                'project': s['project'], 'layer': s['layer'], 'tms': s['tms'],
                'zmin': s['zmin'], 'zmax': s['zmax'], 'bbox': s['bbox'], 'force': 1,
                'pool': s['pool'], 'window_only': s['window_only'], 'priority': PRIO_SCHEDULE,
                'schedule_id': s['id'], 'created_by': 'Zeitplan',
            })
            nxt = now + timedelta(hours=float(s['interval_hours']))
            self.db('UPDATE schedules SET last_run = ?, next_run = ? WHERE id = ?',
                    (now.strftime(cdb.TIME_FMT), nxt.strftime(cdb.TIME_FMT), s['id']))
            self.job_log(jid, 'Vom Zeitplan eingereiht (alle %s h)' % s['interval_hours'])

    def maybe_auto_refresh(self):
        hours = self.cfg.inventory_refresh_hours
        if not hours or not self.cfg.instances or self.active_job('refresh'):
            return
        row = self.db('SELECT MIN(refreshed_at) m FROM instances').fetchone()
        last = cdb.parse_time(row['m']) if row else None
        if last is None or datetime.now() - last >= timedelta(hours=hours):
            self.enqueue({'kind': 'refresh', 'priority': PRIO_REFRESH - 4, 'created_by': 'Worker (automatisch)'})

    def maintenance(self):
        cutoff = (datetime.now() - timedelta(days=self.cfg.job_history_days)).strftime(cdb.TIME_FMT)
        old = [r['id'] for r in self.db(
            "SELECT id FROM jobs WHERE status IN ('done','failed','cancelled') AND finished_at < ?",
            (cutoff,)).fetchall()]
        for jid in old:
            self.db('DELETE FROM job_log WHERE job_id = ?', (jid,))
            self.db('DELETE FROM jobs WHERE id = ?', (jid,))
        keep = int(self.cfg.log_lines_per_job)
        for r in self.db('SELECT job_id, COUNT(*) c FROM job_log GROUP BY job_id HAVING c > ?', (keep,)).fetchall():
            self.db('DELETE FROM job_log WHERE job_id = ? AND id NOT IN '
                    '(SELECT id FROM job_log WHERE job_id = ? ORDER BY id DESC LIMIT ?)',
                    (r['job_id'], r['job_id'], keep))

    # -- size scans ------------------------------------------------------------
    def instance_env(self, name):
        row = self.db('SELECT env_json FROM instances WHERE name = ?', (name,)).fetchone()
        try:
            return json.loads(row['env_json']) if row and row['env_json'] else None
        except ValueError:
            return None

    def enqueue_size_scans(self, instance, repo, project, layer=None, tms=None):
        env = self.instance_env(instance)
        if not env:
            return
        row = self.db('SELECT caps_json FROM projects WHERE instance=? AND repository=? AND project=?',
                      (instance, repo, project)).fetchone()
        if not row or not row['caps_json']:
            return
        caps = json.loads(row['caps_json'])
        for tl in caps.get('tile_layers') or []:
            if layer and tl['name'] != layer:
                continue
            for link in tl['tile_matrix_sets']:
                if tms and link['ref'] != tms:
                    continue
                self.scanner.enqueue((instance, repo, project, tl['name'], link['ref'],
                                      env.get('cache_storage_type', 'file'),
                                      env.get('cache_root_directory', '/tmp'), tl['cache_dir']))

    def enqueue_all_size_scans(self):
        for r in self.db('SELECT instance, repository, project FROM projects WHERE caps_json IS NOT NULL').fetchall():
            self.enqueue_size_scans(r['instance'], r['repository'], r['project'])

    # -- job execution -----------------------------------------------------------
    def start_next_job(self):
        window_ok = in_windows(self.cfg.windows_parsed)
        rows = self.db("SELECT * FROM jobs WHERE status = 'queued' AND kind NOT IN (?, ?) "
                       "ORDER BY priority DESC, id ASC", SIDE_KINDS).fetchall()
        for row in rows:
            if row['kind'] == 'seed' and row['window_only'] and not window_ok:
                continue      # wait for the window; other jobs may still run
            self.begin(row)
            return

    def begin(self, row):
        cur = RunningJob(row)
        self.cur = cur
        try:
            if cur.kind == 'refresh':
                self.plan_refresh(cur)
            elif cur.kind == 'seed':
                self.plan_seed(cur)
            elif cur.kind == 'clean':
                self.plan_clean(cur)
            else:
                raise ValueError('unbekannter Job-Typ: %s' % cur.kind)
        except ValueError as e:
            self.finish('failed', str(e))
            return
        self.update_job(cur.id, status='running', started_at=row['started_at'] or cdb.now_str(),
                        pause_reason=None, message=None)
        self.next_step()

    def helper_args(self, instance_name, wms_url, *args):
        inst = self.cfg.instance(instance_name)
        if not inst:
            raise ValueError('Instanz nicht konfiguriert: %s' % instance_name)
        cmd = [inst.get('php') or self.cfg.php, self.cfg.helper_path, '--lizmap=%s' % inst['lizmap_path']]
        if wms_url:
            cmd.append('--wms-url=%s' % wms_url)
        return cmd + [str(a) for a in args]

    # refresh ------------------------------------------------------------------
    def plan_refresh(self, cur):
        insts = [cur.row['instance']] if cur.row.get('instance') else [i['name'] for i in self.cfg.instances]
        if not insts:
            raise ValueError('keine Lizmap-Instanz konfiguriert')
        # Always the instance's own QGIS Server: reading the inventory must not
        # depend on a seeding pool being set up right (the pool test checks that).
        cur.wms_url = None
        cur.force_caps = bool(cur.row.get('force'))
        for name in insts:
            cur.steps.append({'type': 'env', 'instance': name})
            cur.steps.append({'type': 'projects', 'instance': name})
        cur.caps_total = 0
        self.job_log(cur.id, 'Inventar wird eingelesen: %s' % ', '.join(insts))

    # seed ---------------------------------------------------------------------
    def plan_seed(self, cur):
        r = cur.row
        if not self.cfg.instance(r['instance']):
            raise ValueError('Instanz nicht konfiguriert: %s' % r['instance'])
        prow = self.db('SELECT caps_json FROM projects WHERE instance=? AND repository=? AND project=?',
                       (r['instance'], r['repository'], r['project'])).fetchone()
        if not prow or not prow['caps_json']:
            raise ValueError('Projekt nicht im Inventar – bitte Projekte neu einlesen')
        caps = json.loads(prow['caps_json'])
        bbox = cdb.parse_bbox(r['bbox'])
        plan = cdb.plan_levels(caps, r['layer'], r['tms'], int(r['zmin']), int(r['zmax']), bbox)
        if not plan:
            raise ValueError('keine Kacheln im gewählten Bereich')
        url, err = self.cfg.pool_url(r['pool'])
        if err:
            raise ValueError(err)
        cur.wms_url = url
        cur.image_format = (cdb.find_tile_layer(caps, r['layer']) or {}).get('image_format')
        # resume: skip levels this job already completed (worker restart)
        done = {x['level']: x['tiles'] for x in self.db(
            'SELECT level, tiles FROM seeded_levels WHERE job_id = ? AND instance=? AND repository=? '
            'AND project=? AND layer=? AND tms=?',
            (cur.id, r['instance'], r['repository'], r['project'], r['layer'], r['tms'])).fetchall()}
        total = sum(n for _, n in plan)
        cur.tiles_done_before = sum(n for z, n in plan if z in done)
        cur.errors = int(r['errors'] or 0) if done else 0
        for z, n in plan:
            if z not in done:
                cur.steps.append({'type': 'seed', 'level': z, 'tiles': n})
        self.update_job(cur.id, tiles_total=total, tiles_done=cur.tiles_done_before, errors=cur.errors)
        pool = r['pool'] or self.cfg.default_pool
        self.job_log(cur.id, 'Seeding %s / %s / %s, %s, Stufen %s–%s, %d Kacheln%s%s, Pool: %s%s, %d parallele Prozesse' % (
            r['repository'], r['project'], r['layer'], r['tms'], r['zmin'], r['zmax'], total,
            ', BBox ' + r['bbox'] if r['bbox'] else '', ', neu rechnen (force)' if r['force'] else '',
            pool or 'Instanz-Standard', (' (%s)' % url) if url else '', self.cfg.parallel(r['pool'])))
        if done:
            self.job_log(cur.id, 'Fortsetzung: Stufen %s bereits erledigt' % ', '.join(str(z) for z in sorted(done)))

    # clean --------------------------------------------------------------------
    def plan_clean(self, cur):
        r = cur.row
        if not self.cfg.instance(r['instance']):
            raise ValueError('Instanz nicht konfiguriert: %s' % r['instance'])
        cur.wms_url = None     # deleting files needs no rendering pool
        cur.steps.append({'type': 'clean'})
        self.job_log(cur.id, 'Cache löschen: %s / %s / %s (alle Kachelgitter)' % (r['repository'], r['project'], r['layer']))

    # step machinery -----------------------------------------------------------
    def next_step(self):
        cur = self.cur
        if not cur.steps:
            self.finish_success()
            return
        step = cur.steps.popleft()
        cur.step = step
        cur.output = []
        r = cur.row
        try:
            if step['type'] == 'env':
                args = self.helper_args(step['instance'], cur.wms_url, 'env')
            elif step['type'] == 'projects':
                args = self.helper_args(step['instance'], cur.wms_url, 'projects')
            elif step['type'] == 'caps':
                args = self.helper_args(step['instance'], cur.wms_url, 'capabilities', step['repository'], step['project'])
            elif step['type'] == 'seed':
                extra = []
                if r['bbox']:
                    extra.append('--bbox=%s' % r['bbox'])
                if r['force']:
                    extra.append('--force')
                n = max(1, min(self.cfg.parallel(r['pool']), step['tiles']))
                args = [self.helper_args(r['instance'], cur.wms_url, 'seed', r['repository'], r['project'],
                                         r['layer'], r['tms'], step['level'], step['level'], *extra,
                                         *(['--shard=%d/%d' % (k, n)] if n > 1 else []))
                        for k in range(n)]
                step['done_by'] = [0] * n
                step['done'] = 0
                step['errors'] = 0
                step['not_cached'] = 0
                step['next_percent'] = 5
                step['t0'] = time.time()
                self.update_job(cur.id, current_level=step['level'])
                self.job_log(cur.id, '── Stufe %d: %d Kacheln%s' % (
                    step['level'], step['tiles'], ', %d Prozesse parallel' % n if n > 1 else ''))
            elif step['type'] == 'clean':
                args = self.helper_args(r['instance'], cur.wms_url, 'clean', r['repository'], r['project'], r['layer'])
            else:
                raise ValueError('unknown step %s' % step['type'])
            cur.child = ChildGroup(args if step['type'] == 'seed' else [args])
        except (OSError, ValueError) as e:
            self.finish('failed', 'Start fehlgeschlagen: %s' % e)
            return
        for stream in cur.child.streams:
            self.sel.register(stream, selectors.EVENT_READ)
        self.update_job(cur.id, pid=cur.child.pids[0])

    def unregister(self, group):
        for stream in group.streams:
            try:
                self.sel.unregister(stream)
            except (KeyError, ValueError):
                pass

    def pump(self):
        cur = self.cur
        if not cur or not cur.child:
            return
        for idx, line in cur.child.read_lines():
            self.handle_line(line, idx)
            if self.cur is not cur:      # job aborted while handling output
                return
        self.flush_progress()

    def handle_line(self, line, idx=0):
        cur = self.cur
        st = cur.step['type']
        if st in ('env', 'projects', 'caps'):
            cur.output.append(line)
            return
        if not line.strip() or RE_NOISE.match(line):
            return
        if RE_IDENTITY.match(line):
            # printed for every level; once per job is enough
            if not getattr(cur, 'identity_logged', False):
                cur.identity_logged = True
                self.job_log(cur.id, line)
            return
        if st == 'seed':
            m = RE_PROGRESS.match(line)
            if m:
                step = cur.step
                step['done_by'][idx] = int(m.group(2))
                step['done'] = sum(step['done_by'])
                # one progress line for all processes, every 5 % of the level
                percent = int(step['done'] * 100 / step['tiles']) if step['tiles'] else 100
                if percent >= step['next_percent']:
                    step['next_percent'] = percent - percent % 5 + 5
                    self.job_log(cur.id, 'Fortschritt: %d %%, %d von %d Kacheln' % (percent, step['done'], step['tiles']))
                return
            if RE_TILE_ERROR.match(line):
                cur.step['errors'] += 1
                cur.errors += 1
                n = cur.step['errors']
                if n <= MAX_LOGGED_TILE_ERRORS:
                    self.job_log(cur.id, line, 'error')
                elif n == MAX_LOGGED_TILE_ERRORS + 1:
                    self.job_log(cur.id, '… weitere Kachelfehler dieser Stufe werden nur noch gezählt', 'error')
                # a dead pool fails every tile: stop early instead of grinding through the level
                if n >= EARLY_ABORT_ERRORS and n >= 0.9 * max(cur.step['done'], n):
                    cur.last_message = ('%d Kachelfehler in Stufe %d ohne erfolgreiche Kacheln – '
                                        'ist der QGIS-Pool erreichbar?' % (n, cur.step['level']))
                    self.job_log(cur.id, 'Abbruch: ' + cur.last_message, 'error')
                    self.abort_current('failed', cur.last_message)
                return
            if RE_NOT_CACHED.match(line):
                cur.step['not_cached'] += 1
                if cur.step['not_cached'] <= MAX_LOGGED_TILE_ERRORS:
                    self.job_log(cur.id, line, 'warn')
                return
        m = RE_HELPER_ERROR.match(line)
        if m:
            cur.last_message = m.group(1)
            self.job_log(cur.id, line, 'error')
            return
        self.job_log(cur.id, line)

    def flush_progress(self, force=False):
        cur = self.cur
        if not cur or cur.kind != 'seed' or not cur.step or cur.step['type'] != 'seed':
            return
        if not force and time.time() - cur.last_db_update < 1:
            return
        cur.last_db_update = time.time()
        self.update_job(cur.id, tiles_done=cur.tiles_done_before + cur.step['done'], errors=cur.errors)

    def check_child_exit(self):
        cur = self.cur
        if not cur or not cur.child:
            return
        rc = cur.child.poll()
        if rc is None:
            if (cur.step['type'] in ('env', 'projects', 'caps')
                    and time.time() - cur.child.started > self.cfg.step_timeout_s):
                cur.child.terminate()
                rc = -1
                cur.last_message = 'Zeitüberschreitung nach %d s' % self.cfg.step_timeout_s
            else:
                return
        for idx, line in cur.child.flush_rest():
            self.handle_line(line, idx)
            if self.cur is not cur:      # aborted while handling the remaining output
                return
        self.unregister(cur.child)
        cur.child = None
        step = cur.step
        try:
            ok = self.complete_step(step, rc)
        except Exception as e:
            log('step handling failed:\n' + traceback.format_exc())
            ok = False
            cur.last_message = 'interner Fehler: %s' % e
        if self.cur is not cur:      # finished/aborted inside complete_step
            return
        if ok:
            self.next_step()
        else:
            self.finish('failed', cur.last_message or 'Schritt fehlgeschlagen (Exit-Code %s)' % rc)

    def parse_json_output(self, cur):
        text = '\n'.join(cur.output).strip()
        # take the last line that parses as JSON (Lizmap may print notices before)
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith('{'):
                try:
                    return json.loads(line)
                except ValueError:
                    continue
        return None

    def complete_step(self, step, rc):
        cur = self.cur
        st = step['type']
        if st in ('env', 'projects', 'caps'):
            data = self.parse_json_output(cur)
            if data is None:
                cur.last_message = 'keine gültige Antwort vom Helper (Exit-Code %s): %s' % (
                    rc, ' | '.join(cur.output[-3:])[:500])
            elif data.get('error') and st != 'caps':
                cur.last_message = data['error']
                data = None
            return self.complete_refresh_step(step, data, rc)
        if st == 'seed':
            return self.complete_seed_step(step, rc)
        if st == 'clean':
            r = cur.row
            if rc != 0:
                cur.last_message = cur.last_message or 'Löschen fehlgeschlagen'
                return False
            self.db('DELETE FROM seeded_levels WHERE instance=? AND repository=? AND project=? AND layer=?',
                    (r['instance'], r['repository'], r['project'], r['layer']))
            self.enqueue_size_scans(r['instance'], r['repository'], r['project'], r['layer'])
            return True
        return False

    def complete_refresh_step(self, step, data, rc):
        cur = self.cur
        inst = step['instance']
        if step['type'] == 'env':
            if data is None:
                self.db('UPDATE instances SET error = ? WHERE name = ?', (cur.last_message, inst))
                self.job_log(cur.id, '%s: %s' % (inst, cur.last_message), 'error')
                # skip this instance's project step, continue with others
                cur.steps = deque(s for s in cur.steps if s.get('instance') != inst)
                cur.instance_failed = True
                return True
            self.db('UPDATE instances SET env_json = ?, error = NULL WHERE name = ?', (json.dumps(data), inst))
            self.job_log(cur.id, '%s: Lizmap %s, Cache %s (%s)%s' % (
                inst, data.get('lizmap_version'), data.get('cache_storage_type'), data.get('cache_root_directory'),
                ', WebP-Cache-Fix fehlt' if not data.get('webp_cache_fix') else ''))
            return True
        if step['type'] == 'projects':
            if data is None:
                self.db('UPDATE instances SET error = ? WHERE name = ?', (cur.last_message, inst))
                self.job_log(cur.id, '%s: %s' % (inst, cur.last_message), 'error')
                cur.instance_failed = True
                return True
            seen = set()
            n_caps = 0
            for repo in data.get('repositories') or []:
                for p in repo.get('projects') or []:
                    key = (repo['key'], p['key'])
                    seen.add(key)
                    old = self.db('SELECT qgs_mtime, cfg_mtime, caps_json, error FROM projects '
                                  'WHERE instance=? AND repository=? AND project=?',
                                  (inst, repo['key'], p['key'])).fetchone()
                    self.db('INSERT INTO projects(instance, repository, project, repo_label, qgs_path, qgs_mtime, cfg_mtime) '
                            'VALUES (?,?,?,?,?,?,?) ON CONFLICT(instance, repository, project) DO UPDATE SET '
                            'repo_label=excluded.repo_label, qgs_path=excluded.qgs_path',
                            (inst, repo['key'], p['key'], repo.get('label'), p['qgs_path'], p['qgs_mtime'], p['cfg_mtime']))
                    changed = (not old or old['caps_json'] is None or old['error']
                               or old['qgs_mtime'] != p['qgs_mtime'] or old['cfg_mtime'] != p['cfg_mtime']
                               or caps_version(old['caps_json']) < CAPS_VERSION)
                    if changed or cur.force_caps:
                        cur.steps.append({'type': 'caps', 'instance': inst, 'repository': repo['key'],
                                          'project': p['key'], 'qgs_mtime': p['qgs_mtime'], 'cfg_mtime': p['cfg_mtime']})
                        n_caps += 1
            # drop projects that disappeared
            for r in self.db('SELECT repository, project FROM projects WHERE instance = ?', (inst,)).fetchall():
                if (r['repository'], r['project']) not in seen:
                    self.db('DELETE FROM projects WHERE instance=? AND repository=? AND project=?',
                            (inst, r['repository'], r['project']))
                    self.job_log(cur.id, '%s: Projekt entfernt: %s/%s' % (inst, r['repository'], r['project']), 'warn')
            cur.caps_total += n_caps
            self.update_job(cur.id, tiles_total=cur.caps_total)
            self.job_log(cur.id, '%s: %d Projekte, %d werden neu analysiert' % (inst, len(seen), n_caps))
            self.db('UPDATE instances SET refreshed_at = ?, error = NULL WHERE name = ?', (cdb.now_str(), inst))
            return True
        # caps
        key = (inst, step['repository'], step['project'])
        if data is None:
            self.db('UPDATE projects SET error = ?, refreshed_at = ? WHERE instance=? AND repository=? AND project=?',
                    (cur.last_message, cdb.now_str()) + key)
            self.job_log(cur.id, '%s/%s: %s' % (step['repository'], step['project'], cur.last_message), 'error')
        else:
            err = data.get('error')
            self.db('UPDATE projects SET caps_json = ?, title = ?, error = ?, refreshed_at = ?, qgs_mtime = ?, cfg_mtime = ? '
                    'WHERE instance=? AND repository=? AND project=?',
                    (json.dumps(data), data.get('title'), err, cdb.now_str(), step['qgs_mtime'], step['cfg_mtime']) + key)
            n_tile = len(data.get('tile_layers') or [])
            groups = data.get('seed_groups') or []
            as_groups = (' (gelesen als Gruppen: %s)' % ', '.join(groups)) if groups else ''
            if err:
                self.job_log(cur.id, '%s/%s: %s%s' % (step['repository'], step['project'], err, as_groups), 'warn')
            elif n_tile:
                self.job_log(cur.id, '%s/%s: %d cachefähige Layer%s' % (step['repository'], step['project'], n_tile, as_groups))
            self.enqueue_size_scans(*key)
        cur.last_message = None
        done = (self.db('SELECT tiles_done FROM jobs WHERE id = ?', (cur.id,)).fetchone()['tiles_done'] or 0) + 1
        self.update_job(cur.id, tiles_done=done)
        return True

    def complete_seed_step(self, step, rc):
        cur = self.cur
        r = cur.row
        dur = time.time() - step['t0']
        if rc != 0:
            cur.last_message = cur.last_message or 'Seeding von Stufe %d fehlgeschlagen (Exit-Code %s)' % (step['level'], rc)
            return False
        if step['errors'] >= step['tiles']:
            cur.last_message = ('alle %d Kacheln von Stufe %d fehlgeschlagen – ist der QGIS-Pool erreichbar?'
                                % (step['tiles'], step['level']))
            return False
        self.db('INSERT INTO seeded_levels(instance, repository, project, layer, tms, level, seeded_at, image_format, '
                'tiles, errors, forced, bbox, job_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(instance, repository, project, layer, tms, level) DO UPDATE SET '
                'seeded_at=excluded.seeded_at, image_format=excluded.image_format, tiles=excluded.tiles, '
                'errors=excluded.errors, forced=excluded.forced, bbox=excluded.bbox, job_id=excluded.job_id',
                (r['instance'], r['repository'], r['project'], r['layer'], r['tms'], step['level'], cdb.now_str(),
                 cur.image_format, step['tiles'], step['errors'], int(bool(r['force'])), r['bbox'], cur.id))
        cur.tiles_done_before += step['tiles']
        self.update_job(cur.id, tiles_done=cur.tiles_done_before, errors=cur.errors)
        rate = step['tiles'] / dur if dur > 0 else 0
        self.job_log(cur.id, 'Stufe %d fertig: %d Kacheln in %s (%.1f Kacheln/s)%s' % (
            step['level'], step['tiles'], fmt_duration(dur), rate,
            ', %d Fehler' % step['errors'] if step['errors'] else ''),
            'warn' if step['errors'] else 'info')
        return True

    def finish_success(self):
        cur = self.cur
        r = cur.row
        msg = None
        status = 'done'
        if cur.kind == 'seed':
            self.enqueue_size_scans(r['instance'], r['repository'], r['project'], r['layer'], r['tms'])
            msg = '%d Fehler' % cur.errors if cur.errors else None
        elif cur.kind == 'refresh':
            failed = self.db("SELECT COUNT(*) c FROM projects WHERE error IS NOT NULL").fetchone()['c']
            msg = '%d Projekt(e) mit Fehlern' % failed if failed else None
        self.finish(status, msg)

    def finish(self, status, message=None):
        cur = self.cur
        if not cur:
            return
        self.update_job(cur.id, status=status, finished_at=cdb.now_str(), message=message, pid=None,
                        pause_reason=None, current_level=None)
        label = {'done': 'Fertig', 'failed': 'Fehlgeschlagen', 'cancelled': 'Abgebrochen'}.get(status, status)
        self.job_log(cur.id, '%s%s' % (label, (': ' + message) if message else ''),
                     'error' if status == 'failed' else 'info')
        log('job %d (%s) %s%s' % (cur.id, cur.kind, status, (': ' + message) if message else ''))
        self.cur = None
        self.heartbeat(force=True)

    def abort_current(self, status, message):
        cur = self.cur
        if cur.child:
            self.unregister(cur.child)
            cur.child.terminate()
            cur.child = None
        self.finish(status, message)


def fmt_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return '%d s' % seconds
    if seconds < 3600:
        return '%d min %d s' % (seconds // 60, seconds % 60)
    return '%d h %d min' % (seconds // 3600, (seconds % 3600) // 60)


# ---------------------------------------------------------------------------
def cmd_check(fcfg):
    """Validate the setup and every configured instance (runs `env` through the helper)."""
    ok = True
    print('config file:', fcfg.path, '' if fcfg.exists else '(not present – defaults used)')
    db_dir = os.path.dirname(fcfg.db_path) or '.'
    print('db:', fcfg.db_path, '(dir writable)' if os.access(db_dir, os.W_OK) else '(dir NOT writable!)')
    print('helper:', fcfg.helper_path, 'ok' if os.path.isfile(fcfg.helper_path) else 'MISSING')
    print('php:', fcfg.php, 'ok' if os.access(fcfg.php, os.X_OK) else 'NOT FOUND')
    print('running as:', getpass.getuser())
    print('allowed instance directories:', ', '.join(fcfg.instances_base))
    print('pools allowed:', 'any host' if fcfg.allow_remote_pools else 'localhost only')
    if os.geteuid() == 0:
        print('WARNING: running as root – run the worker as the Lizmap PHP user (e.g. www-data)')
        ok = False
    if not os.access(db_dir, os.W_OK):
        print('settings: (cannot open the cache DB)')
        return 1
    conn = cdb.connect(fcfg.db_path)
    cdb.init_schema(conn)
    settings, errors = load_settings(conn, fcfg)
    cfg = Config(fcfg, settings)
    for e in errors:
        print('settings problem:', e)
        ok = False
    if not cfg.instances:
        print('no instances configured yet – add them in the dashboard: Cache-Verwaltung → Einstellungen → Instanzen suchen')
    for p in cfg.pools:
        print('pool %s: %s' % (p['name'], p['url']))
    print('windows:', cfg.windows or 'always')
    for inst in cfg.instances:
        url, _ = cfg.pool_url(None)
        args = [cfg.php, cfg.helper_path, '--lizmap=%s' % inst['lizmap_path']]
        if url:
            args.append('--wms-url=%s' % url)
        args.append('env')
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=120)
            data = json.loads(out.stdout.strip().splitlines()[-1])
        except Exception as e:
            print('instance %s: FAILED (%s)' % (inst['name'], e))
            ok = False
            continue
        if data.get('error'):
            print('instance %s: FAILED (%s)' % (inst['name'], data['error']))
            ok = False
        else:
            print('instance %s: Lizmap %s, %d repositories, cache %s at %s, WMS %s%s' % (
                inst['name'], data['lizmap_version'], data['repositories'], data['cache_storage_type'],
                data['cache_root_directory'], data['wms_server_url'],
                '' if data['webp_cache_fix'] else ', WebP cache fix MISSING'))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description='Lizmap tile cache worker')
    ap.add_argument('--config', default=os.environ.get('LIZMAP_CACHE_WORKER_CONFIG', DEFAULT_CONFIG_PATH))
    ap.add_argument('command', nargs='?', choices=['run', 'check'], default='run')
    a = ap.parse_args()
    try:
        fcfg = FileConfig(a.config)
    except (OSError, ValueError) as e:
        print('config error: %s' % e, file=sys.stderr)
        return 2
    if a.command == 'check':
        return cmd_check(fcfg)
    if os.geteuid() == 0:
        print('refusing to run as root: run the worker as the Lizmap PHP user (e.g. www-data)', file=sys.stderr)
        return 2
    Worker(fcfg).run()
    return 0


if __name__ == '__main__':
    sys.exit(main())
