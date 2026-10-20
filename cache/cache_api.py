"""
Dashboard API for the Lizmap tile-cache management (Flask blueprint).

The dashboard never executes anything: it validates requests against the
inventory the worker collected and writes jobs/schedules into the shared cache
DB. The worker (cache_worker.py, running as the Lizmap PHP user) does the work.

Write endpoints require the X-CSRF-Token header (GET /api/cache/csrf) and a
same-origin Origin header: HTTP basic auth is sent by the browser automatically,
so without a token any other site could trigger jobs in the user's name.

Copyright (c) 2025 meyerlor - MIT License (see LICENSE)
"""

import hashlib
import hmac
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

from flask import Blueprint, current_app, jsonify, request

import cache_db as cdb

bp = Blueprint('cache', __name__, url_prefix='/api/cache')

CACHE_DB_PATH = os.environ.get('MONITORING_CACHE_DB', '/var/lib/lizmap-cache/cache.db')
WORKER_ONLINE_S = 30          # heartbeat older than this = worker offline
LOAD_WINDOW_S = 300           # p95 window for the load signal


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@bp.errorhandler(ApiError)
def _api_error(e):
    return jsonify({'error': str(e)}), e.status


@bp.app_context_processor
def _static_version():
    """?v=<mtime> for static/cache.js so browsers never run a stale copy after a deploy."""
    try:
        v = int(os.path.getmtime(os.path.join(current_app.static_folder, 'cache.js')))
    except (OSError, TypeError):
        v = 0
    return {'cache_static_v': v}


def open_db():
    if not os.path.exists(CACHE_DB_PATH):
        raise ApiError('Cache-Datenbank nicht gefunden (%s) – ist der Cache-Worker eingerichtet?' % CACHE_DB_PATH, 503)
    try:
        conn = cdb.connect(CACHE_DB_PATH)
        conn.execute('SELECT 1 FROM jobs LIMIT 1')
        return conn
    except sqlite3.Error as e:
        raise ApiError('Cache-Datenbank nicht nutzbar: %s' % e, 503)


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def jload(text, default=None):
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def current_user():
    auth = request.authorization
    if auth and auth.username:
        return auth.username
    return request.headers.get('X-Remote-User') or 'dashboard'


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
def csrf_token():
    key = str(current_app.config.get('SECRET_KEY') or '').encode()
    return hmac.new(key, b'lizmap-cache-csrf', hashlib.sha256).hexdigest()


def require_csrf():
    if not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), csrf_token()):
        raise ApiError('CSRF-Token ungültig – bitte die Seite neu laden', 403)
    origin = request.headers.get('Origin')
    if origin and urlparse(origin).netloc != request.host:
        raise ApiError('Anfrage von fremder Herkunft abgelehnt', 403)


@bp.route('/csrf')
def get_csrf():
    return jsonify({'token': csrf_token()})


# ---------------------------------------------------------------------------
# Load signal (called from monitor.py's log thread)
# ---------------------------------------------------------------------------
def write_load_signal(response_times):
    """p95 of the last 5 min per monitored pool -> meta 'load_signal' for the worker."""
    if not os.path.exists(CACHE_DB_PATH):
        return
    cutoff = datetime.now().timestamp() - LOAD_WINDOW_S
    pools = {}
    for pool, values in response_times.items():
        recent = sorted(rt for ts, rt in list(values) if ts >= cutoff)
        pools[pool] = recent[int(len(recent) * 0.95)] if len(recent) >= 5 else None
    conn = cdb.connect(CACHE_DB_PATH)
    try:
        cdb.meta_set(conn, 'load_signal', {'ts': cdb.now_str(), 'ts_epoch': time.time(), 'pools': pools})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Status / inventory
# ---------------------------------------------------------------------------
def worker_status(conn):
    w = cdb.meta_get(conn, 'worker') or {}
    # epoch, not local time: worker and dashboard may run with different TZ settings
    age = time.time() - float(w['last_seen_epoch']) if w.get('last_seen_epoch') else None
    w['online'] = bool(age is not None and age <= WORKER_ONLINE_S and w.get('state') != 'stopped')
    w['age_s'] = int(age) if age is not None else None
    return w


@bp.route('/status')
def get_status():
    try:
        conn = open_db()
    except ApiError as e:
        return jsonify({'available': False, 'reason': str(e), 'db_path': CACHE_DB_PATH})
    try:
        instances = []
        for r in rows(conn.execute('SELECT * FROM instances ORDER BY name')):
            r['env'] = jload(r.pop('env_json'), {})
            instances.append(r)
        counts = {r['status']: r['n'] for r in conn.execute(
            'SELECT status, COUNT(*) n FROM jobs WHERE status IN (?,?,?) GROUP BY status', cdb.ACTIVE_STATUSES)}
        return jsonify({
            'available': True,
            'db_path': CACHE_DB_PATH,
            'worker': worker_status(conn),
            'config': cdb.meta_get(conn, 'worker_config') or {},
            'load_signal': cdb.meta_get(conn, 'load_signal'),
            'instances': instances,
            'queue': counts,
        })
    finally:
        conn.close()


@bp.route('/projects')
def get_projects():
    conn = open_db()
    try:
        sizes = {}
        for r in conn.execute('SELECT instance, repository, project, SUM(bytes) b, SUM(files) f '
                              'FROM layer_sizes GROUP BY instance, repository, project'):
            sizes[(r['instance'], r['repository'], r['project'])] = (r['b'], r['f'])
        scheds = {}
        for r in conn.execute('SELECT instance, repository, project, COUNT(*) n FROM schedules '
                              'WHERE enabled = 1 GROUP BY instance, repository, project'):
            scheds[(r['instance'], r['repository'], r['project'])] = r['n']
        result = []
        for r in conn.execute('SELECT instance, repository, project, repo_label, title, error, refreshed_at, caps_json '
                              'FROM projects ORDER BY instance, repository, project'):
            caps = jload(r['caps_json'], {}) or {}
            key = (r['instance'], r['repository'], r['project'])
            b, f = sizes.get(key, (None, None))
            result.append({
                'instance': r['instance'], 'repository': r['repository'], 'project': r['project'],
                'repo_label': r['repo_label'], 'title': r['title'] or r['project'],
                'error': r['error'], 'refreshed_at': r['refreshed_at'],
                'tile_layers': len(caps.get('tile_layers') or []),
                'layers': len(caps.get('cfg_layers') or []),
                'bytes': b, 'files': f, 'schedules': scheds.get(key, 0),
            })
        return jsonify(result)
    finally:
        conn.close()


def load_project(conn, instance, repository, project):
    row = conn.execute('SELECT * FROM projects WHERE instance=? AND repository=? AND project=?',
                       (instance, repository, project)).fetchone()
    if not row:
        raise ApiError('Projekt nicht im Inventar: %s/%s/%s' % (instance, repository, project), 404)
    caps = jload(row['caps_json'])
    return row, caps


def instance_env(conn, instance):
    row = conn.execute('SELECT env_json FROM instances WHERE name = ?', (instance,)).fetchone()
    return jload(row['env_json'], {}) if row else {}


def ttl_seconds(cfg_layer, env):
    ttl = cfg_layer.get('cache_expiration') if cfg_layer else None
    if ttl is None:
        ttl = env.get('cache_expiration_default') or 0
    return int(ttl or 0)


def is_webp(fmt):
    return bool(fmt) and 'webp' in fmt.lower()


@bp.route('/project')
def get_project():
    a = request.args
    instance, repository, project = a.get('instance', ''), a.get('repository', ''), a.get('project', '')
    conn = open_db()
    try:
        prow, caps = load_project(conn, instance, repository, project)
        env = instance_env(conn, instance)
        caps = caps or {}
        cfg_by_name = {}
        for l in caps.get('cfg_layers') or []:
            cfg_by_name.setdefault(l['name'], l)
        tile_by_id = {t['id']: t for t in caps.get('tile_layers') or [] if t.get('id')}
        tile_by_name = {t['name']: t for t in caps.get('tile_layers') or []}
        tms_levels = {t['ref']: {lv['id']: lv for lv in t['levels']} for t in caps.get('tile_matrix_sets') or []}

        ledger = {}
        for r in conn.execute('SELECT * FROM seeded_levels WHERE instance=? AND repository=? AND project=?',
                              (instance, repository, project)):
            ledger.setdefault((r['layer'], r['tms']), {})[r['level']] = dict(r)
        sizes = {(r['layer'], r['tms']): dict(r) for r in conn.execute(
            'SELECT * FROM layer_sizes WHERE instance=? AND repository=? AND project=?', (instance, repository, project))}
        schedules = {(r['layer'], r['tms']): dict(r) for r in conn.execute(
            'SELECT * FROM schedules WHERE instance=? AND repository=? AND project=?', (instance, repository, project))}
        active = rows(conn.execute(
            'SELECT id, kind, layer, tms, zmin, zmax, status, pause_reason, current_level, tiles_done, tiles_total '
            'FROM jobs WHERE instance=? AND repository=? AND project=? AND status IN (?,?,?)',
            (instance, repository, project) + cdb.ACTIVE_STATUSES))
        now = datetime.now()
        used_tiles = set()

        def tile_info(cfg, tile):
            fmt = tile.get('image_format') or (cfg or {}).get('image_format')
            ttl = ttl_seconds(cfg, env)
            warnings = []
            if is_webp(fmt) and not env.get('gd_webp'):
                warnings.append('webp_no_gd')
            if is_webp(fmt) and not env.get('webp_cache_fix'):
                warnings.append('webp_no_fix')
            tms_out = []
            for link in tile['tile_matrix_sets']:
                lvl_defs = tms_levels.get(link['ref'], {})
                led = ledger.get((tile['name'], link['ref']), {})
                levels = []
                for lim in link['limits']:
                    z = lim['id']
                    seeded = led.get(z)
                    expired = False
                    if seeded and ttl > 0:
                        t = cdb.parse_time(seeded['seeded_at'])
                        expired = bool(t and t + timedelta(seconds=ttl) < now)
                    jobs_here = [j['id'] for j in active if j['kind'] == 'seed' and j['layer'] == tile['name']
                                 and j['tms'] == link['ref'] and j['zmin'] <= z <= j['zmax']]
                    levels.append({
                        'id': z, 'tiles': lim['tiles'],
                        'scale': (lvl_defs.get(z) or {}).get('scale_denominator'),
                        'seeded_at': seeded['seeded_at'] if seeded else None,
                        'seeded_format': seeded['image_format'] if seeded else None,
                        'seeded_tiles': seeded['tiles'] if seeded else None,
                        'seeded_errors': seeded['errors'] if seeded else None,
                        'seeded_bbox': seeded['bbox'] if seeded else None,
                        'expired': expired,
                        'jobs': jobs_here,
                    })
                sched = schedules.get((tile['name'], link['ref']))
                tms_warn = []
                if sched and sched['enabled'] and ttl > 0 and float(sched['interval_hours']) * 3600 > ttl:
                    tms_warn.append('schedule_after_ttl')
                if any(lv['seeded_format'] and fmt and lv['seeded_format'] != fmt for lv in levels):
                    tms_warn.append('format_changed')
                tms_out.append({
                    'ref': link['ref'], 'levels': levels, 'size': sizes.get((tile['name'], link['ref'])),
                    'schedule': sched, 'warnings': tms_warn,
                })
            return {
                'name': tile['name'], 'cache_dir': tile['cache_dir'], 'image_format': fmt, 'ttl': ttl,
                'metatile_size': (cfg or {}).get('metatile_size'), 'tile_matrix_sets': tms_out, 'warnings': warnings,
            }

        def merge(nodes):
            out = []
            for n in nodes:
                cfg = cfg_by_name.get(n['name'])
                node = {'type': n['type'], 'name': n['name'], 'title': (cfg or {}).get('title') or n['name']}
                if cfg is None:
                    node['status'] = 'not_in_cfg'
                else:
                    node['status'] = cfg['seed_status']
                    node['image_format'] = cfg.get('image_format')
                    tile = tile_by_id.get(cfg.get('id')) if cfg.get('id') else None
                    tile = tile or tile_by_name.get(cfg.get('shortname') or '') or tile_by_name.get(cfg['name'])
                    if cfg['seed_status'] == 'ok':
                        if tile:
                            node['tile'] = tile_info(cfg, tile)
                            used_tiles.add(tile['name'])
                        else:
                            node['status'] = 'caps_missing'
                if n['type'] == 'group':
                    node['children'] = merge(n.get('children') or [])
                out.append(node)
            return out

        tree = merge(caps.get('layer_tree') or [])
        # tile layers not reachable through the tree (should not happen, but never hide them)
        extra = [t for t in caps.get('tile_layers') or [] if t['name'] not in used_tiles]
        for t in extra:
            cfg = next((c for c in caps.get('cfg_layers') or [] if c.get('id') == t.get('id')), None)
            tree.append({'type': 'layer', 'name': t['name'], 'title': t.get('title') or t['name'],
                         'status': 'ok', 'tile': tile_info(cfg, t), 'image_format': t.get('image_format')})

        return jsonify({
            'instance': instance, 'repository': repository, 'project': project,
            'repo_label': prow['repo_label'], 'title': prow['title'] or project, 'qgs_path': prow['qgs_path'],
            'projection': caps.get('projection'), 'refreshed_at': prow['refreshed_at'],
            'error': prow['error'] or caps.get('error'),
            'env': {k: env.get(k) for k in ('lizmap_version', 'cache_storage_type', 'cache_root_directory',
                                            'cache_expiration_default', 'gd_webp', 'webp_cache_fix',
                                            'wms_server_url')},
            'tile_matrix_sets': [{'ref': t['ref'], 'unit': t.get('unit')} for t in caps.get('tile_matrix_sets') or []],
            'tree': tree,
            'active_jobs': active,
        })
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Validation shared by jobs, estimates and schedules
# ---------------------------------------------------------------------------
def _int(value, name):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError('%s muss eine Ganzzahl sein' % name)


def validate_seed_target(conn, data, need_levels=True):
    instance, repository, project = data.get('instance'), data.get('repository'), data.get('project')
    layer, tms = data.get('layer'), data.get('tms')
    for k, v in (('instance', instance), ('repository', repository), ('project', project), ('layer', layer)):
        if not v or not isinstance(v, str):
            raise ApiError('Feld fehlt: %s' % k)
    _, caps = load_project(conn, instance, repository, project)
    if not caps or not cdb.find_tile_layer(caps, layer):
        raise ApiError('Layer ist nicht seedbar: %s' % layer)
    out = {'instance': instance, 'repository': repository, 'project': project, 'layer': layer}
    if not need_levels:
        return out, caps
    if not tms or not isinstance(tms, str):
        raise ApiError('Feld fehlt: tms')
    zmin, zmax = _int(data.get('zmin'), 'zmin'), _int(data.get('zmax'), 'zmax')
    if zmax < zmin:
        raise ApiError('Stufe „bis“ muss ≥ „von“ sein')
    try:
        bbox = cdb.parse_bbox(data.get('bbox'))
        plan = cdb.plan_levels(caps, layer, tms, zmin, zmax, bbox)
    except ValueError as e:
        raise ApiError(str(e))
    out.update({'tms': tms, 'zmin': zmin, 'zmax': zmax,
                'bbox': ','.join(repr(x) for x in bbox) if bbox else None})
    return out, caps, plan


def validate_pool(conn, pool):
    if pool in (None, ''):
        return None
    cfg = cdb.meta_get(conn, 'worker_config') or {}
    if pool not in [p['name'] for p in cfg.get('pools') or []]:
        raise ApiError('Unbekannter Seeding-Pool: %s' % pool)
    return pool


@bp.route('/estimate')
def get_estimate():
    conn = open_db()
    try:
        target, caps, plan = validate_seed_target(conn, request.args)
        tiles = sum(n for _, n in plan)
        size = conn.execute('SELECT bytes, files FROM layer_sizes WHERE instance=? AND repository=? AND project=? '
                            'AND layer=? AND tms=?', (target['instance'], target['repository'], target['project'],
                                                      target['layer'], target['tms'])).fetchone()
        bytes_per_tile = (size['bytes'] / size['files']) if size and size['files'] else None
        # throughput from finished seed jobs (same instance first, then all)
        rate = None
        for where, args in (('AND instance = ?', (target['instance'],)), ('', ())):
            r = conn.execute(
                "SELECT SUM(tiles_done) t, SUM((julianday(finished_at) - julianday(started_at)) * 86400) s "
                "FROM jobs WHERE kind='seed' AND status='done' AND tiles_done > 0 %s" % where, args).fetchone()
            if r and r['t'] and r['s'] and r['s'] > 0:
                rate = r['t'] / r['s']
                break
        return jsonify({
            'levels': [{'id': z, 'tiles': n} for z, n in plan],
            'tiles': tiles,
            'bytes_per_tile': bytes_per_tile,
            'est_bytes': int(bytes_per_tile * tiles) if bytes_per_tile else None,
            'tiles_per_s': rate,
            'est_seconds': int(tiles / rate) if rate else None,
        })
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
def job_view(j):
    j = dict(j)
    total, done = j.get('tiles_total') or 0, j.get('tiles_done') or 0
    j['percent'] = round(100.0 * done / total, 1) if total else None
    j['eta_s'] = None
    if j['status'] == 'running' and j['kind'] == 'seed' and done and total:
        started = cdb.parse_time(j.get('started_at'))
        if started:
            elapsed = (datetime.now() - started).total_seconds()
            if elapsed > 0:
                j['eta_s'] = int((total - done) / (done / elapsed))
    return j


@bp.route('/jobs')
def get_jobs():
    limit = max(1, min(int(request.args.get('limit', 40) or 40), 200))
    conn = open_db()
    try:
        active = [job_view(j) for j in conn.execute(
            'SELECT * FROM jobs WHERE status IN (?,?,?) ORDER BY '
            "CASE status WHEN 'running' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END, priority DESC, id ASC",
            cdb.ACTIVE_STATUSES)]
        recent = [job_view(j) for j in conn.execute(
            'SELECT * FROM jobs WHERE status NOT IN (?,?,?) ORDER BY id DESC LIMIT ?',
            cdb.ACTIVE_STATUSES + (limit,))]
        return jsonify({'active': active, 'recent': recent, 'worker': worker_status(conn)})
    finally:
        conn.close()


@bp.route('/jobs/<int:job_id>/log')
def get_job_log(job_id):
    after = max(0, int(request.args.get('after', 0) or 0))
    limit = max(1, min(int(request.args.get('limit', 500) or 500), 2000))
    conn = open_db()
    try:
        j = conn.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
        if not j:
            raise ApiError('Job nicht gefunden', 404)
        if after == 0:   # first load: the most recent lines
            lines = rows(conn.execute('SELECT * FROM (SELECT id, ts, level, line FROM job_log WHERE job_id = ? '
                                      'ORDER BY id DESC LIMIT ?) ORDER BY id', (job_id, limit)))
        else:
            lines = rows(conn.execute('SELECT id, ts, level, line FROM job_log WHERE job_id = ? AND id > ? '
                                      'ORDER BY id LIMIT ?', (job_id, after, limit)))
        return jsonify({'job': job_view(j), 'lines': lines})
    finally:
        conn.close()


@bp.route('/jobs', methods=['POST'])
def post_job():
    require_csrf()
    data = request.get_json(silent=True) or {}
    kind = data.get('kind')
    conn = open_db()
    try:
        job = {'kind': kind, 'created_by': current_user(), 'force': 0, 'window_only': 0}
        if kind == 'refresh':
            inst = data.get('instance') or None
            if inst and not conn.execute('SELECT 1 FROM instances WHERE name = ?', (inst,)).fetchone():
                raise ApiError('Unbekannte Instanz: %s' % inst)
            if conn.execute("SELECT 1 FROM jobs WHERE kind='refresh' AND status IN (?,?,?)", cdb.ACTIVE_STATUSES).fetchone():
                raise ApiError('Projekte werden bereits eingelesen', 409)
            job.update({'instance': inst, 'force': 1 if data.get('force') else 0, 'priority': 15,
                        'pool': validate_pool(conn, data.get('pool'))})
        elif kind == 'seed':
            target, _, plan = validate_seed_target(conn, data)
            dup = conn.execute("SELECT id FROM jobs WHERE kind='seed' AND status IN (?,?,?) AND instance=? AND repository=? "
                               "AND project=? AND layer=? AND tms=?",
                               cdb.ACTIVE_STATUSES + (target['instance'], target['repository'], target['project'],
                                                      target['layer'], target['tms'])).fetchone()
            if dup:
                raise ApiError('Für diesen Layer läuft bereits ein Seeding-Job (#%d)' % dup['id'], 409)
            job.update(target)
            job.update({'force': 1 if data.get('force') else 0, 'window_only': 1 if data.get('window_only') else 0,
                        'pool': validate_pool(conn, data.get('pool')), 'priority': 10})
        elif kind == 'clean':
            target, _ = validate_seed_target(conn, data, need_levels=False)
            job.update(target)
            job['priority'] = 20
        else:
            raise ApiError('Unbekannter Job-Typ')
        cols = list(job)
        cur = conn.execute('INSERT INTO jobs(%s, status, created_at) VALUES (%s, \'queued\', ?)'
                           % (', '.join(cols), ', '.join('?' * len(cols))), [job[c] for c in cols] + [cdb.now_str()])
        cdb.add_log(conn, cur.lastrowid, 'Eingereiht von %s' % job['created_by'])
        conn.commit()
        return jsonify({'id': cur.lastrowid}), 201
    finally:
        conn.close()


@bp.route('/jobs/<int:job_id>/action', methods=['POST'])
def post_job_action(job_id):
    require_csrf()
    action = (request.get_json(silent=True) or {}).get('action')
    if action not in ('pause', 'resume', 'cancel'):
        raise ApiError('Unbekannte Aktion')
    conn = open_db()
    try:
        j = conn.execute('SELECT status FROM jobs WHERE id = ?', (job_id,)).fetchone()
        if not j:
            raise ApiError('Job nicht gefunden', 404)
        if j['status'] not in cdb.ACTIVE_STATUSES:
            raise ApiError('Job ist bereits beendet', 409)
        conn.execute('UPDATE jobs SET requested_action = ? WHERE id = ?', (action, job_id))
        cdb.add_log(conn, job_id, '%s angefordert von %s' % (
            {'pause': 'Pause', 'resume': 'Fortsetzen', 'cancel': 'Abbruch'}[action], current_user()))
        conn.commit()
        return jsonify({'ok': True})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------
@bp.route('/schedules', methods=['POST'])
def post_schedule():
    require_csrf()
    data = request.get_json(silent=True) or {}
    conn = open_db()
    try:
        target, _, plan = validate_seed_target(conn, data)
        try:
            interval = float(data.get('interval_hours'))
        except (TypeError, ValueError):
            raise ApiError('Intervall fehlt')
        if not (1 <= interval <= 24 * 366):
            raise ApiError('Intervall muss zwischen 1 Stunde und 1 Jahr liegen')
        pool = validate_pool(conn, data.get('pool'))
        enabled = 1 if data.get('enabled', True) else 0
        window_only = 1 if data.get('window_only', True) else 0
        key = (target['instance'], target['repository'], target['project'], target['layer'], target['tms'])
        # first run: interval after the most recent seeding of the range, else now
        last = conn.execute('SELECT MAX(seeded_at) m FROM seeded_levels WHERE instance=? AND repository=? AND project=? '
                            'AND layer=? AND tms=? AND level BETWEEN ? AND ?',
                            key + (target['zmin'], target['zmax'])).fetchone()['m']
        base = cdb.parse_time(last)
        nxt = base + timedelta(hours=interval) if base else datetime.now()
        if nxt < datetime.now():
            nxt = datetime.now()
        conn.execute(
            'INSERT INTO schedules(instance, repository, project, layer, tms, zmin, zmax, bbox, interval_hours, pool, '
            'window_only, enabled, next_run, created_by, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(instance, repository, project, layer, tms) DO UPDATE SET zmin=excluded.zmin, '
            'zmax=excluded.zmax, bbox=excluded.bbox, interval_hours=excluded.interval_hours, pool=excluded.pool, '
            'window_only=excluded.window_only, enabled=excluded.enabled, next_run=excluded.next_run, '
            'created_by=excluded.created_by, updated_at=excluded.updated_at',
            key + (target['zmin'], target['zmax'], target['bbox'], interval, pool, window_only, enabled,
                   nxt.strftime(cdb.TIME_FMT), current_user(), cdb.now_str()))
        conn.commit()
        return jsonify({'ok': True, 'next_run': nxt.strftime(cdb.TIME_FMT)})
    finally:
        conn.close()


@bp.route('/schedules/<int:schedule_id>', methods=['DELETE'])
def delete_schedule(schedule_id):
    require_csrf()
    conn = open_db()
    try:
        conn.execute('DELETE FROM schedules WHERE id = ?', (schedule_id,))
        conn.commit()
        return jsonify({'ok': True})
    finally:
        conn.close()
