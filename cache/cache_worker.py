#!/usr/bin/env python3
"""
Lizmap tile cache worker for the QGIS Server Monitoring Dashboard.

Runs as its own systemd service AS THE LIZMAP PHP USER (e.g. www-data), so the
tiles it writes have the same owner as the ones Lizmap writes itself. It
executes the jobs the dashboard puts into the shared cache DB:

  refresh  read projects + tile capabilities of every configured Lizmap instance
  seed     generate tiles for one layer / tile matrix set / level range
  clean    delete the cache of one layer

Only one job runs at a time. Seeding is split into one Lizmap call per zoom
level, which gives an exact per-level ledger and lets an interrupted job resume
at the level where it stopped. A running seed job is paused (SIGSTOP) while the
server is busy or outside the configured time window, and continued (SIGCONT)
afterwards. Tile requests can be routed to a dedicated QGIS Server pool.

stdlib only. Usage:
    cache_worker.py [--config /etc/lizmap-cache-worker.json]
    cache_worker.py --config ... check        # validate config + instances, then exit

Copyright (c) 2025 meyerlor - MIT License (see LICENSE)
"""

import argparse
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
from collections import deque
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cache_db as cdb  # noqa: E402

VERSION = '1.0'
DEFAULT_CONFIG_PATH = '/etc/lizmap-cache-worker.json'

DEFAULTS = {
    'db_path': '/var/lib/lizmap-cache/cache.db',
    'php': '/usr/bin/php',
    'helper_path': os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lizmap_cache_helper.php'),
    'instances': [],             # [{"name": "...", "lizmap_path": ".../lizmap", "php": optional}]
    'pools': [],                 # [{"name": "...", "url": "http://127.0.0.1:.../ows/", "monitor_pool": "qgis-pool4"}]
    'default_pool': None,        # pool name used when a job does not choose one; None = instance's own URL
    'windows': [],               # ["20:00-06:00"]; empty = always allowed
    'load_pause': {
        'enabled': True,
        'cpu_percent': 85,
        'user_p95_ms': 5000,
        'watch_pools': None,     # dashboard pool keys whose p95 counts; None = all except seeding pools
        'resume_below_factor': 0.8,
        'resume_after_s': 120,
        'check_interval_s': 5,
    },
    'inventory_refresh_hours': 24,
    'size_scan_interval_min': 360,
    'job_history_days': 30,
    'log_lines_per_job': 3000,
    'step_timeout_s': 600,       # for refresh steps (capabilities); seeding has no timeout
}

MAX_LOGGED_TILE_ERRORS = 50     # per level; further errors are only counted
EARLY_ABORT_ERRORS = 200        # abort a level that fails (almost) every tile

PRIO_CLEAN = 20
PRIO_MANUAL = 10
PRIO_REFRESH = 5
PRIO_SCHEDULE = 0

RE_PROGRESS = re.compile(r'^Progression: (\d+)%, (\d+) tiles generated on (\d+) tiles')
RE_TILE_ERROR = re.compile(r'^Error for tile: ')
RE_NOT_CACHED = re.compile(r'^Error, tile not cached: ')
RE_HELPER_ERROR = re.compile(r'^Error: (.*)')
RE_NOISE = re.compile(r"^The TileMatrixSet '")


def log(msg):
    print('[%s] %s' % (cdb.now_str(), msg), flush=True)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
class Config:
    def __init__(self, path):
        self.path = path
        with open(path, encoding='utf-8') as f:
            raw = json.load(f)
        data = json.loads(json.dumps(DEFAULTS))
        for k, v in raw.items():
            if k == 'load_pause' and isinstance(v, dict):
                data['load_pause'].update(v)
            else:
                data[k] = v
        self.__dict__.update(data)
        self.windows_parsed = [parse_window(w) for w in self.windows]
        names = [i['name'] for i in self.instances]
        if len(names) != len(set(names)):
            raise ValueError('instance names must be unique')
        for inst in self.instances:
            if not re.match(r'^[A-Za-z0-9_.-]+$', inst.get('name', '')):
                raise ValueError('invalid instance name: %r' % inst.get('name'))
        if self.default_pool and not self.pool(self.default_pool):
            raise ValueError('default_pool %r is not in pools' % self.default_pool)

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

    def summary(self):
        return {
            'pools': [{'name': p['name'], 'url': p['url'], 'monitor_pool': p.get('monitor_pool')} for p in self.pools],
            'default_pool': self.default_pool,
            'windows': self.windows,
            'load_pause': self.load_pause,
            'instances': [i['name'] for i in self.instances],
            'size_scan_interval_min': self.size_scan_interval_min,
            'inventory_refresh_hours': self.inventory_refresh_hours,
        }


def parse_window(text):
    m = re.match(r'^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$', text)
    if not m:
        raise ValueError('invalid time window %r (expected HH:MM-HH:MM)' % text)
    a = int(m.group(1)) * 60 + int(m.group(2))
    b = int(m.group(3)) * 60 + int(m.group(4))
    if a >= 24 * 60 or b > 24 * 60:
        raise ValueError('invalid time window %r' % text)
    return a, b


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

    def terminate(self, grace=10):
        self.resume()
        self.signal(signal.SIGTERM)
        deadline = time.time() + grace
        while time.time() < deadline and self.proc.poll() is None:
            time.sleep(0.2)
        if self.proc.poll() is None:
            self.signal(signal.SIGKILL)
            self.proc.wait(5)


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
        self.wms_url = None        # pool override for the helper, None = instance URL
        self.force_caps = False
        self.caps_total = 0
        self.image_format = None


class Worker:
    def __init__(self, cfg):
        self.cfg = cfg
        self.conn = cdb.connect(cfg.db_path)
        cdb.init_schema(self.conn)
        self.cpu = CpuSampler()
        self.cur = None
        self.started_at = cdb.now_str()
        self.stop_requested = False
        self.last = {'heartbeat': 0, 'schedule': 0, 'load': 0, 'maintenance': 0, 'size_full': 0}
        self.cpu_value = None
        self.p95_value = None
        self.scanner = SizeScanner(cfg.db_path)
        self.scanner.start()
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
        rows = self.db("SELECT id FROM jobs WHERE status = 'running'").fetchall()
        for r in rows:
            self.update_job(r['id'], status='queued', pid=None)
            self.job_log(r['id'], 'Worker neu gestartet – Job wird fortgesetzt', 'warn')
        cdb.meta_set(self.conn, 'worker_config', self.cfg.summary())
        for inst in self.cfg.instances:
            self.db('INSERT INTO instances(name, lizmap_path) VALUES (?, ?) '
                    'ON CONFLICT(name) DO UPDATE SET lizmap_path = excluded.lizmap_path',
                    (inst['name'], inst['lizmap_path']))
        names = [i['name'] for i in self.cfg.instances]
        if names:
            self.db('DELETE FROM instances WHERE name NOT IN (%s)' % ','.join('?' * len(names)), names)
        else:
            self.db('DELETE FROM instances')
        never = self.db('SELECT COUNT(*) c FROM instances WHERE refreshed_at IS NULL').fetchone()['c']
        if never and not self.active_job('refresh'):
            self.enqueue({'kind': 'refresh', 'priority': PRIO_REFRESH, 'created_by': 'Worker (Start)'})

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
            self.sel.unregister(self.cur.child.proc.stdout)
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
                    self.cur.row['pause_reason'] = None
                    self.cur.load_ok_since = None
                    self.cur.child.resume()
                    self.update_job(jid, status='running', pause_reason=None)
                    self.job_log(jid, 'Fortgesetzt (manuell)')
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
        if not hours or self.active_job('refresh'):
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
        rows = self.db("SELECT * FROM jobs WHERE status = 'queued' ORDER BY priority DESC, id ASC").fetchall()
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
        url, err = self.cfg.pool_url(cur.row.get('pool'))
        if err:
            raise ValueError(err)
        cur.wms_url = url
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
        self.job_log(cur.id, 'Seeding %s / %s / %s, %s, Stufen %s–%s, %d Kacheln%s%s, Pool: %s%s' % (
            r['repository'], r['project'], r['layer'], r['tms'], r['zmin'], r['zmax'], total,
            ', BBox ' + r['bbox'] if r['bbox'] else '', ', neu rechnen (force)' if r['force'] else '',
            pool or 'Instanz-Standard', (' (%s)' % url) if url else ''))
        if done:
            self.job_log(cur.id, 'Fortsetzung: Stufen %s bereits erledigt' % ', '.join(str(z) for z in sorted(done)))

    # clean --------------------------------------------------------------------
    def plan_clean(self, cur):
        r = cur.row
        if not self.cfg.instance(r['instance']):
            raise ValueError('Instanz nicht konfiguriert: %s' % r['instance'])
        url, _ = self.cfg.pool_url(r['pool'])
        cur.wms_url = url
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
                args = self.helper_args(r['instance'], cur.wms_url, 'seed', r['repository'], r['project'],
                                        r['layer'], r['tms'], step['level'], step['level'], *extra)
                step['done'] = 0
                step['errors'] = 0
                step['not_cached'] = 0
                step['t0'] = time.time()
                self.update_job(cur.id, current_level=step['level'])
                self.job_log(cur.id, '── Stufe %d: %d Kacheln' % (step['level'], step['tiles']))
            elif step['type'] == 'clean':
                args = self.helper_args(r['instance'], cur.wms_url, 'clean', r['repository'], r['project'], r['layer'])
            else:
                raise ValueError('unknown step %s' % step['type'])
            cur.child = Child(args)
        except (OSError, ValueError) as e:
            self.finish('failed', 'Start fehlgeschlagen: %s' % e)
            return
        self.sel.register(cur.child.proc.stdout, selectors.EVENT_READ)
        self.update_job(cur.id, pid=cur.child.proc.pid)

    def pump(self):
        cur = self.cur
        if not cur or not cur.child:
            return
        for line in cur.child.read_lines():
            self.handle_line(line)
            if self.cur is not cur:      # job aborted while handling output
                return
        self.flush_progress()

    def handle_line(self, line):
        cur = self.cur
        st = cur.step['type']
        if st in ('env', 'projects', 'caps'):
            cur.output.append(line)
            return
        if not line.strip() or RE_NOISE.match(line):
            return
        if st == 'seed':
            m = RE_PROGRESS.match(line)
            if m:
                cur.step['done'] = int(m.group(2))
                self.job_log(cur.id, line)
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
        for line in cur.child.flush_rest():
            self.handle_line(line)
            if self.cur is not cur:      # aborted while handling the remaining output
                return
        self.sel.unregister(cur.child.proc.stdout)
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
                               or old['qgs_mtime'] != p['qgs_mtime'] or old['cfg_mtime'] != p['cfg_mtime'])
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
            if err:
                self.job_log(cur.id, '%s/%s: %s' % (step['repository'], step['project'], err), 'warn')
            elif n_tile:
                self.job_log(cur.id, '%s/%s: %d cachefähige Layer' % (step['repository'], step['project'], n_tile))
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
            self.sel.unregister(cur.child.proc.stdout)
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
def cmd_check(cfg):
    """Validate config and every instance (runs `env` through the helper)."""
    ok = True
    print('config:', cfg.path)
    print('db:', cfg.db_path, '(dir writable)' if os.access(os.path.dirname(cfg.db_path) or '.', os.W_OK) else '(dir NOT writable!)')
    print('helper:', cfg.helper_path, 'ok' if os.path.isfile(cfg.helper_path) else 'MISSING')
    print('running as:', getpass.getuser())
    if os.geteuid() == 0:
        print('WARNING: running as root – run the worker as the Lizmap PHP user (e.g. www-data)')
        ok = False
    for p in cfg.pools:
        print('pool %s: %s' % (p['name'], p['url']))
    print('windows:', cfg.windows or 'always')
    for inst in cfg.instances:
        url, _ = cfg.pool_url(None)
        args = [inst.get('php') or cfg.php, cfg.helper_path, '--lizmap=%s' % inst['lizmap_path']]
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
        cfg = Config(a.config)
    except (OSError, ValueError) as e:
        print('config error: %s' % e, file=sys.stderr)
        return 2
    if a.command == 'check':
        return cmd_check(cfg)
    if os.geteuid() == 0:
        print('refusing to run as root: run the worker as the Lizmap PHP user (e.g. www-data)', file=sys.stderr)
        return 2
    Worker(cfg).run()
    return 0


if __name__ == '__main__':
    sys.exit(main())
