/*
 * Cache-Verwaltung tab of the GIS Monitoring Dashboard.
 *
 * Talks only to /api/cache/*: the dashboard validates and stores jobs, the
 * separate cache worker (running as the Lizmap PHP user) executes them.
 * Image format and cache expiry belong to the project's .cfg and are shown
 * read-only with a hint where to change them (Lizmap plugin).
 *
 * Copyright (c) 2025 meyerlor - MIT License (see LICENSE)
 */
(function () {
    'use strict';

    // -----------------------------------------------------------------------
    // i18n (merged into the dashboard's translations object)
    // -----------------------------------------------------------------------
    const I18N = {
        de: {
            tab_cache: '🧱 Cache-Verwaltung',
            cache_unavailable_title: 'Cache-Verwaltung nicht eingerichtet',
            cache_unavailable_help: 'Der Cache-Worker läuft als eigener Dienst unter dem PHP-Benutzer von Lizmap (z.&nbsp;B. <code>www-data</code>). Einmalig auf dem Server als root einrichten: <code>sudo /opt/monitoring/cache/install-worker.sh</code> – danach diese Seite neu laden. Details: README, Abschnitt „Tile cache worker“.',
            cache_worker_title: '⚙️ Cache-Worker',
            cache_load_title: '🛡️ Betriebsschutz',
            cache_instances_title: '🗂️ Lizmap-Instanzen',
            cache_refresh: 'Projekte neu einlesen',
            cache_refresh_full: 'Alle neu analysieren',
            cache_refresh_tip: 'Sucht neue und entfernte Projekte und liest nur die Projekte neu, deren .qgs oder .cfg sich seit dem letzten Einlesen geändert hat. Schnell – für den Alltag, z. B. nach dem Hochladen eines Projekts.',
            cache_refresh_full_tip: 'Liest alle Projekte vollständig neu, auch unveränderte. Sinnvoll nach einem Update von Lizmap oder QGIS Server oder wenn die Anzeige veraltet wirkt. Dauert bei vielen Projekten einige Minuten.',
            c_refresh_hint_none: 'Erst in den Einstellungen Instanzen auswählen und speichern.',
            c_refresh_hint_busy: 'Projekte werden gerade eingelesen (Job #{id}) – danach wieder verfügbar.',
            c_webp_tip: 'Betrifft nur Layer mit dem Bildformat image/webp – PNG und JPEG werden normal gecacht. Lizmap-Versionen ohne den WebP-Cache-Fix (#7232, noch in keinem Release) speichern WebP-Kacheln als JPEG, ohne Transparenz.',
            cache_projects_title: '📁 Projekte',
            cache_search: 'Suchen…',
            cache_only_seedable: 'nur Projekte mit cachefähigen Layern',
            cache_pick_project: 'Projekt links auswählen',
            cache_view_tms: 'Kachelgitter',
            cache_lg_seeded: 'geseedet',
            cache_lg_expired: 'abgelaufen (TTL)',
            cache_lg_errors: 'mit Fehlern',
            cache_lg_active: 'in Arbeit / Warteschlange',
            cache_lg_none: 'nicht geseedet',
            cache_col_layer: 'Layer',
            cache_col_format: 'Format',
            cache_col_ttl: 'Ablauf',
            cache_col_levels: 'Stufen',
            cache_col_size: 'Größe',
            cache_col_schedule: 'Zeitplan',
            cache_col_target: 'Ziel',
            cache_col_status: 'Status',
            cache_col_progress: 'Fortschritt',
            cache_col_by: 'von',
            cache_queue_title: '📋 Warteschlange',
            cache_show_recent: 'Verlauf anzeigen',
            cache_log_title: '📜 Live-Protokoll',
            cache_log_follow: 'automatisch scrollen',
            // dynamic
            c_online: 'online', c_offline: 'offline', c_state_idle: 'bereit', c_state_running: 'arbeitet',
            c_state_paused: 'pausiert', c_state_stopped: 'gestoppt',
            c_worker_status: 'Status', c_worker_user: 'läuft als', c_worker_since: 'gestartet', c_worker_seen: 'letztes Lebenszeichen',
            c_worker_job: 'aktueller Job', c_worker_offline_hint: 'Der Worker meldet sich nicht. Läuft der Dienst lizmap-cache-worker?',
            c_seconds_ago: 'vor {n} s', c_cpu: 'CPU', c_p95: 'Antwortzeit Nutzer (p95)', c_window: 'Zeitfenster',
            c_window_open: 'offen', c_window_closed: 'geschlossen', c_window_always: 'immer erlaubt',
            c_load_pause: 'Pause bei Last', c_load_off: 'aus', c_load_rule: 'CPU > {cpu} % oder p95 > {p95} ms',
            c_default_pool: 'Seeding-Pool (Standard)', c_instance_default: 'Instanz-Standard',
            c_queue: 'Warteschlange', c_queue_val: '{r} aktiv · {q} wartend · {p} pausiert',
            c_no_instances: 'Noch keine Instanz – unten in den Einstellungen „Instanzen suchen“.',
            c_refreshed: 'eingelesen', c_never: 'nie', c_cache: 'Cache', c_webp_ok: 'WebP-Cache ok',
            c_webp_nofix: 'WebP: ab nächstem Lizmap-Release', c_webp_nogd: 'WebP: GD ohne WebP',
            c_tile_layers: '{n} cachefähig', c_schedules_n: '{n} Zeitplan/-pläne', c_no_projects: 'Keine Projekte gefunden',
            c_proj_error: 'Fehler beim Einlesen',
            c_st_not_cached: 'Server-Cache in der .cfg nicht aktiviert – im Lizmap-Plugin beim Layer „Server-Cache“ aktivieren',
            c_st_single_tile: 'wird als Einzelbild (singleTile) ausgeliefert – im Lizmap-Plugin „Kacheln“ aktivieren, um zu cachen',
            c_st_overview: 'Übersichtskarten-Layer (wird nicht gecacht)',
            c_st_caps_missing: 'Cache aktiv, aber nicht in den WMTS-Capabilities (Layer nicht veröffentlicht?)',
            c_st_not_in_cfg: 'nicht in der .cfg (nicht veröffentlicht)',
            c_not_in_tms: 'nicht in diesem Kachelgitter',
            c_ttl_never: 'nie', c_files: '{n} Kacheln', c_scanned: 'gemessen', c_size_redis: 'n. v. (Redis)',
            c_every: 'alle {v}', c_hours: 'Std.', c_days: 'Tage', c_paused_sched: 'deaktiviert',
            c_level_tip: 'Stufe {z} · 1:{scale} · {tiles} Kacheln',
            c_level_seeded: 'geseedet {t} ({fmt})', c_level_expired: 'abgelaufen', c_level_errors: '{n} Fehler',
            c_level_none: 'nicht geseedet', c_level_active: 'Job #{ids} in Arbeit/Warteschlange',
            c_ed_tms: 'Kachelgitter', c_ed_from: 'Stufe von', c_ed_to: 'Stufe bis', c_ed_bbox: 'Begrenzung (BBox, optional)',
            c_ed_bbox_sub: 'xmin,ymin,xmax,ymax im KBS des Kachelgitters', c_ed_pool: 'QGIS-Pool fürs Seeding',
            c_ed_force: 'neu rechnen (vorhandene Kacheln überschreiben)', c_ed_window: 'nur im Zeitfenster',
            c_ed_format: 'Bildformat', c_ed_format_hint: 'wird in der .cfg festgelegt (Lizmap-Plugin → Layer → Bildformat)',
            c_ed_ttl: 'Cache-Ablauf', c_ed_ttl_hint: 'wird in der .cfg festgelegt (Lizmap-Plugin → Layer → Cache-Ablaufzeit)',
            c_ed_metatile: 'Metatile', c_ed_seed: 'Seeding starten', c_ed_reseed: 'Neu rechnen starten', c_ed_clean: 'Cache löschen',
            c_ed_estimate: '≈ {tiles} Kacheln in {levels} Stufen', c_ed_est_size: '≈ {size} Speicher',
            c_ed_est_time: '≈ {time} Rechenzeit', c_ed_est_unknown: 'Rechenzeit noch unbekannt (kein früherer Job)',
            c_ed_est_big: 'Großer Job – erwäge eine BBox oder weniger Stufen.',
            c_sched_title: '⏱ Zeitplan (automatisch neu rechnen)', c_sched_interval: 'Intervall',
            c_sched_enabled: 'aktiv', c_sched_save: 'Zeitplan speichern', c_sched_delete: 'Zeitplan löschen',
            c_sched_uses: 'Verwendet Kachelgitter, Stufen, BBox und Pool von oben. Läuft immer als „neu rechnen“.',
            c_sched_next: 'nächster Lauf: {t}', c_sched_last: 'letzter Lauf: {t}',
            c_w_webp_no_fix: 'Der Layer nutzt WebP, aber diese Lizmap-Version hat den WebP-Cache-Fix (#7232) nicht: gecachte Kacheln werden als JPEG gespeichert und verlieren die Transparenz. Lizmap aktualisieren, bevor geseedet wird.',
            c_w_webp_no_gd: 'Der Layer nutzt WebP, aber PHP-GD dieser Instanz kann kein WebP: Metatiles werden nicht genutzt.',
            c_w_schedule_after_ttl: 'Das Zeitplan-Intervall ist länger als der Cache-Ablauf – Kacheln laufen vor dem nächsten Seeding ab.',
            c_w_format_changed: 'Das Bildformat in der .cfg wurde seit dem Seeding geändert – vorhandene Kacheln im alten Format werden nicht mehr genutzt. Cache löschen und neu seeden.',
            c_w_clean_all: 'Löscht den Cache dieses Layers in ALLEN Kachelgittern. Fortfahren?',
            c_w_storage_redis: 'Diese Instanz speichert den Cache in Redis: Größen können nicht gemessen werden, und „Cache löschen“ leert bei Redis den Cache des ganzen Projekts.',
            c_w_proj_error: 'Projekt konnte nicht vollständig gelesen werden: {e}',
            c_job_seed: 'Seeding', c_job_clean: 'Löschen', c_job_refresh: 'Inventar',
            c_js_queued: 'wartet', c_js_queued_window: 'wartet auf Zeitfenster', c_js_running: 'läuft', c_js_paused: 'pausiert',
            c_js_done: 'fertig', c_js_failed: 'fehlgeschlagen', c_js_cancelled: 'abgebrochen',
            c_pr_load: 'Last', c_pr_window: 'Zeitfenster', c_pr_user: 'manuell',
            c_eta: 'noch ≈ {t}', c_errors_n: '{n} Fehler', c_projects_n: '{d}/{t} Projekte', c_all_instances: 'alle Instanzen',
            c_act_pause: 'Pausieren', c_act_resume: 'Fortsetzen', c_act_cancel: 'Abbrechen', c_confirm_cancel: 'Job #{id} abbrechen?',
            c_no_jobs: 'Keine Jobs', c_log_pick: 'Job in der Warteschlange anklicken, um sein Protokoll zu sehen.',
            c_log_empty: '(noch keine Einträge)',
            c_toast_queued: 'Job #{id} eingereiht', c_toast_saved: 'Zeitplan gespeichert – nächster Lauf {t}',
            c_toast_deleted: 'Zeitplan gelöscht', c_err: 'Fehler: {e}',
            c_force_badge: 'neu rechnen', c_window_badge: 'Zeitfenster',
            cache_settings_title: '⚙️ Einstellungen',
            s_summary: '{i} Instanz(en) · {p} Pool(s) · Standard-Pool: {d}',
            s_bounds: 'Erlaubte Instanz-Ordner: {b} · Pools: {p} · festgelegt in {f}',
            s_bounds_local: 'nur localhost', s_bounds_any: 'beliebige Hosts',
            s_file_missing: '(Datei nicht vorhanden – Standardwerte)',
            s_inst_title: 'Lizmap-Instanzen',
            s_inst_help: 'Der Worker sucht in den erlaubten Ordnern nach Lizmap-Installationen (<Ordner>/<Name>/lizmap). Angehakte Instanzen werden verwaltet.',
            s_discover: 'Instanzen suchen', s_discovering: 'Suche läuft …', s_last_discovery: 'zuletzt gesucht: {t}',
            s_no_discovery: 'Noch nicht gesucht – „Instanzen suchen“ klicken.',
            s_col_manage: 'verwalten', s_col_name: 'Name', s_col_path: 'Pfad', s_col_version: 'Lizmap',
            s_col_qgis: 'QGIS Server (wmsServerURL)', s_col_cache: 'Cache', s_col_webp: 'WebP',
            s_not_found: 'bei der letzten Suche nicht gefunden',
            s_pools_title: 'QGIS-Pools fürs Seeding',
            s_pools_help: 'Optional: Kachelanfragen beim Seeding an einen eigenen QGIS-Server-Pool schicken, damit die Pools der Nutzer frei bleiben. Ohne Pool nutzt jede Instanz ihren eigenen QGIS Server.',
            s_col_url: 'URL', s_col_monitor: 'Dashboard-Pool',
            s_col_monitor_tip: 'Derselbe Pool im Dashboard – seine Last zählt dann nicht als Nutzerlast (sonst bremst sich das Seeding selbst aus)',
            s_col_default: 'Standard', s_col_test: 'Test',
            s_add_pool: '+ Pool hinzufügen', s_suggest: 'Von den Instanzen genutzte QGIS Server:',
            s_no_default: 'kein Standard-Pool (jede Instanz nutzt ihren eigenen QGIS Server)',
            s_test: 'Testen', s_testing: 'teste …', s_remove: 'Pool entfernen',
            s_ops_title: 'Betrieb', s_windows: 'Zeitfenster', s_windows_sub: 'z. B. 20:00-06:00, mehrere mit Komma. Leer = jederzeit.',
            s_load_enabled: 'Seeding bei Serverlast pausieren', s_cpu: 'CPU-Grenze (%)', s_p95: 'Grenze Antwortzeit Nutzer p95 (ms)',
            s_resume: 'Fortsetzen nach (s) unter 80 %', s_inv_h: 'Projekte neu einlesen alle (Std., 0 = nie)',
            s_size_min: 'Cache-Größen messen alle (Min.)', s_hist: 'Job-Verlauf aufbewahren (Tage)',
            s_save: 'Einstellungen speichern', s_discard: 'Verwerfen',
            s_saved: 'Gespeichert – der Worker übernimmt die Einstellungen in wenigen Sekunden',
            s_unsaved: 'ungespeicherte Änderungen', s_saved_by: 'zuletzt gespeichert von {u} am {t}',
            s_worker_errors: 'Der Worker hat diese Einstellungen verworfen:',
            s_offline_discover: 'Der Worker läuft nicht – die Suche startet, sobald er wieder läuft.',
            s_none: 'keiner', s_errors: 'Nicht gespeichert:',
        },
        en: {
            tab_cache: '🧱 Cache Management',
            cache_unavailable_title: 'Cache management not set up',
            cache_unavailable_help: 'The cache worker runs as its own service as Lizmap\'s PHP user (e.g. <code>www-data</code>). Set it up once on the server as root: <code>sudo /opt/monitoring/cache/install-worker.sh</code> – then reload this page. Details: README, section “Tile cache worker”.',
            cache_worker_title: '⚙️ Cache worker',
            cache_load_title: '🛡️ Production safeguards',
            cache_instances_title: '🗂️ Lizmap instances',
            cache_refresh: 'Rescan projects',
            cache_refresh_full: 'Re-analyse all',
            cache_refresh_tip: 'Finds new and removed projects and only re-reads projects whose .qgs or .cfg changed since the last scan. Fast – for everyday use, e.g. after uploading a project.',
            cache_refresh_full_tip: 'Re-reads all projects completely, including unchanged ones. Useful after updating Lizmap or QGIS Server, or when the view looks outdated. Takes a few minutes with many projects.',
            c_refresh_hint_none: 'Select and save instances in the settings first.',
            c_refresh_hint_busy: 'Projects are being read right now (job #{id}) – available again afterwards.',
            c_webp_tip: 'Only affects layers with the image format image/webp – PNG and JPEG are cached normally. Lizmap versions without the WebP cache fix (#7232, not released yet) store WebP tiles as JPEG, without transparency.',
            cache_projects_title: '📁 Projects',
            cache_search: 'Search…',
            cache_only_seedable: 'only projects with cacheable layers',
            cache_pick_project: 'Pick a project on the left',
            cache_view_tms: 'Tile matrix set',
            cache_lg_seeded: 'seeded', cache_lg_expired: 'expired (TTL)', cache_lg_errors: 'with errors',
            cache_lg_active: 'running / queued', cache_lg_none: 'not seeded',
            cache_col_layer: 'Layer', cache_col_format: 'Format', cache_col_ttl: 'Expiry', cache_col_levels: 'Levels',
            cache_col_size: 'Size', cache_col_schedule: 'Schedule', cache_col_target: 'Target', cache_col_status: 'Status',
            cache_col_progress: 'Progress', cache_col_by: 'by',
            cache_queue_title: '📋 Queue', cache_show_recent: 'show history',
            cache_log_title: '📜 Live log', cache_log_follow: 'auto-scroll',
            c_online: 'online', c_offline: 'offline', c_state_idle: 'idle', c_state_running: 'working',
            c_state_paused: 'paused', c_state_stopped: 'stopped',
            c_worker_status: 'Status', c_worker_user: 'runs as', c_worker_since: 'started', c_worker_seen: 'last heartbeat',
            c_worker_job: 'current job', c_worker_offline_hint: 'The worker does not report. Is the lizmap-cache-worker service running?',
            c_seconds_ago: '{n} s ago', c_cpu: 'CPU', c_p95: 'User response time (p95)', c_window: 'Time window',
            c_window_open: 'open', c_window_closed: 'closed', c_window_always: 'always allowed',
            c_load_pause: 'Pause on load', c_load_off: 'off', c_load_rule: 'CPU > {cpu} % or p95 > {p95} ms',
            c_default_pool: 'Seeding pool (default)', c_instance_default: 'instance default',
            c_queue: 'Queue', c_queue_val: '{r} active · {q} waiting · {p} paused',
            c_no_instances: 'No instance yet – use “Find instances” in the settings below.',
            c_refreshed: 'scanned', c_never: 'never', c_cache: 'Cache', c_webp_ok: 'WebP cache ok',
            c_webp_nofix: 'WebP: from next Lizmap release', c_webp_nogd: 'WebP: GD without WebP',
            c_tile_layers: '{n} cacheable', c_schedules_n: '{n} schedule(s)', c_no_projects: 'No projects found',
            c_proj_error: 'Scan error',
            c_st_not_cached: 'Server cache not enabled in the .cfg – enable “Server cache” for the layer in the Lizmap plugin',
            c_st_single_tile: 'served as a single image (singleTile) – enable “Tiled” in the Lizmap plugin to cache it',
            c_st_overview: 'overview map layer (not cached)',
            c_st_caps_missing: 'cache enabled but not in the WMTS capabilities (layer not published?)',
            c_st_not_in_cfg: 'not in the .cfg (not published)',
            c_not_in_tms: 'not in this tile matrix set',
            c_ttl_never: 'never', c_files: '{n} tiles', c_scanned: 'measured', c_size_redis: 'n/a (Redis)',
            c_every: 'every {v}', c_hours: 'h', c_days: 'days', c_paused_sched: 'disabled',
            c_level_tip: 'Level {z} · 1:{scale} · {tiles} tiles',
            c_level_seeded: 'seeded {t} ({fmt})', c_level_expired: 'expired', c_level_errors: '{n} errors',
            c_level_none: 'not seeded', c_level_active: 'job #{ids} running/queued',
            c_ed_tms: 'Tile matrix set', c_ed_from: 'Level from', c_ed_to: 'Level to', c_ed_bbox: 'Extent (bbox, optional)',
            c_ed_bbox_sub: 'xmin,ymin,xmax,ymax in the tile matrix set CRS', c_ed_pool: 'QGIS pool for seeding',
            c_ed_force: 'recompute (overwrite existing tiles)', c_ed_window: 'only in time window',
            c_ed_format: 'Image format', c_ed_format_hint: 'set in the .cfg (Lizmap plugin → layer → image format)',
            c_ed_ttl: 'Cache expiry', c_ed_ttl_hint: 'set in the .cfg (Lizmap plugin → layer → cache expiration)',
            c_ed_metatile: 'Metatile', c_ed_seed: 'Start seeding', c_ed_reseed: 'Start recompute', c_ed_clean: 'Clear cache',
            c_ed_estimate: '≈ {tiles} tiles in {levels} levels', c_ed_est_size: '≈ {size} storage',
            c_ed_est_time: '≈ {time} processing', c_ed_est_unknown: 'duration unknown yet (no previous job)',
            c_ed_est_big: 'Large job – consider a bbox or fewer levels.',
            c_sched_title: '⏱ Schedule (recompute automatically)', c_sched_interval: 'Interval',
            c_sched_enabled: 'enabled', c_sched_save: 'Save schedule', c_sched_delete: 'Delete schedule',
            c_sched_uses: 'Uses the tile matrix set, levels, bbox and pool from above. Always runs as “recompute”.',
            c_sched_next: 'next run: {t}', c_sched_last: 'last run: {t}',
            c_w_webp_no_fix: 'This layer uses WebP, but this Lizmap version lacks the WebP cache fix (#7232): cached tiles are stored as JPEG and lose transparency. Update Lizmap before seeding.',
            c_w_webp_no_gd: 'This layer uses WebP, but PHP GD on this instance has no WebP support: metatiles are not used.',
            c_w_schedule_after_ttl: 'The schedule interval is longer than the cache expiry – tiles expire before the next seeding.',
            c_w_format_changed: 'The image format in the .cfg changed since seeding – existing tiles in the old format are no longer used. Clear the cache and seed again.',
            c_w_clean_all: 'Clears this layer\'s cache in ALL tile matrix sets. Continue?',
            c_w_storage_redis: 'This instance stores the cache in Redis: sizes cannot be measured, and “clear cache” flushes the whole project with Redis.',
            c_w_proj_error: 'The project could not be read completely: {e}',
            c_job_seed: 'Seeding', c_job_clean: 'Clear', c_job_refresh: 'Inventory',
            c_js_queued: 'queued', c_js_queued_window: 'waiting for time window', c_js_running: 'running', c_js_paused: 'paused',
            c_js_done: 'done', c_js_failed: 'failed', c_js_cancelled: 'cancelled',
            c_pr_load: 'load', c_pr_window: 'time window', c_pr_user: 'manual',
            c_eta: '≈ {t} left', c_errors_n: '{n} errors', c_projects_n: '{d}/{t} projects', c_all_instances: 'all instances',
            c_act_pause: 'Pause', c_act_resume: 'Resume', c_act_cancel: 'Cancel', c_confirm_cancel: 'Cancel job #{id}?',
            c_no_jobs: 'No jobs', c_log_pick: 'Click a job in the queue to see its log.', c_log_empty: '(no entries yet)',
            c_toast_queued: 'Job #{id} queued', c_toast_saved: 'Schedule saved – next run {t}',
            c_toast_deleted: 'Schedule deleted', c_err: 'Error: {e}',
            c_force_badge: 'recompute', c_window_badge: 'time window',
            cache_settings_title: '⚙️ Settings',
            s_summary: '{i} instance(s) · {p} pool(s) · default pool: {d}',
            s_bounds: 'Allowed instance directories: {b} · pools: {p} · set in {f}',
            s_bounds_local: 'localhost only', s_bounds_any: 'any host',
            s_file_missing: '(file not present – defaults)',
            s_inst_title: 'Lizmap instances',
            s_inst_help: 'The worker looks for Lizmap installations in the allowed directories (<directory>/<name>/lizmap). Ticked instances are managed.',
            s_discover: 'Find instances', s_discovering: 'searching …', s_last_discovery: 'last search: {t}',
            s_no_discovery: 'Not searched yet – click “Find instances”.',
            s_col_manage: 'manage', s_col_name: 'Name', s_col_path: 'Path', s_col_version: 'Lizmap',
            s_col_qgis: 'QGIS Server (wmsServerURL)', s_col_cache: 'Cache', s_col_webp: 'WebP',
            s_not_found: 'not found by the last search',
            s_pools_title: 'QGIS pools for seeding',
            s_pools_help: 'Optional: send the tile requests of seeding jobs to a dedicated QGIS Server pool so the users\' pools stay free. Without a pool each instance uses its own QGIS Server.',
            s_col_url: 'URL', s_col_monitor: 'Dashboard pool',
            s_col_monitor_tip: 'The same pool in the dashboard – its load is then not counted as user load (otherwise seeding would throttle itself)',
            s_col_default: 'Default', s_col_test: 'Test',
            s_add_pool: '+ Add pool', s_suggest: 'QGIS Servers used by the instances:',
            s_no_default: 'no default pool (each instance uses its own QGIS Server)',
            s_test: 'Test', s_testing: 'testing …', s_remove: 'Remove pool',
            s_ops_title: 'Operation', s_windows: 'Time windows', s_windows_sub: 'e.g. 20:00-06:00, several separated by commas. Empty = any time.',
            s_load_enabled: 'Pause seeding while the server is busy', s_cpu: 'CPU limit (%)', s_p95: 'User p95 response time limit (ms)',
            s_resume: 'Resume after (s) below 80 %', s_inv_h: 'Rescan projects every (h, 0 = never)',
            s_size_min: 'Measure cache sizes every (min)', s_hist: 'Keep job history (days)',
            s_save: 'Save settings', s_discard: 'Discard',
            s_saved: 'Saved – the worker applies the settings within a few seconds',
            s_unsaved: 'unsaved changes', s_saved_by: 'last saved by {u} on {t}',
            s_worker_errors: 'The worker discarded these settings:',
            s_offline_discover: 'The worker is not running – the search starts once it runs again.',
            s_none: 'none', s_errors: 'Not saved:',
        },
    };
    Object.assign(translations.de, I18N.de);
    Object.assign(translations.en, I18N.en);

    function tr(key, vars) {
        let s = t(key);
        if (vars) for (const k in vars) s = s.split('{' + k + '}').join(vars[k]);
        return s;
    }

    // -----------------------------------------------------------------------
    // State
    // -----------------------------------------------------------------------
    const S = {
        inited: false, timers: [], csrf: null, status: null, projects: [], sel: null, project: null,
        viewTms: null, openLayer: null, editor: {}, collapsed: new Set(), jobs: { active: [], recent: [] },
        logJob: null, logLastId: 0, estimateSeq: 0, busy: false,
    };

    // -----------------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------------
    const $ = (id) => document.getElementById(id);
    function esc(v) {
        return String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }
    const fmtNum = (n) => (n == null ? '–' : Number(n).toLocaleString(getLocale()));
    function fmtBytes(b) {
        if (b == null) return '–';
        const u = ['B', 'KB', 'MB', 'GB', 'TB'];
        let i = 0, v = Number(b);
        while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
        return v.toLocaleString(getLocale(), { maximumFractionDigits: i ? 1 : 0 }) + ' ' + u[i];
    }
    function fmtDur(s) {
        if (s == null) return '–';
        s = Math.max(0, Math.round(s));
        if (s < 60) return s + ' s';
        if (s < 3600) return Math.floor(s / 60) + ' min';
        if (s < 86400) return Math.floor(s / 3600) + ' h ' + Math.floor((s % 3600) / 60) + ' min';
        return Math.floor(s / 86400) + ' d ' + Math.floor((s % 86400) / 3600) + ' h';
    }
    function fmtTime(ts) {
        if (!ts) return '–';
        const d = new Date(ts.replace(' ', 'T'));
        if (isNaN(d)) return ts;
        return d.toLocaleString(getLocale(), { day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit' });
    }
    function fmtTTL(sec) {
        if (!sec) return tr('c_ttl_never');
        return fmtDur(sec);
    }
    const fmtScale = (s) => (s == null ? '?' : Math.round(s).toLocaleString(getLocale()));
    function fmtInterval(h) {
        h = Number(h);
        return h % 24 === 0 ? tr('c_every', { v: (h / 24) + ' ' + tr('c_days') }) : tr('c_every', { v: h + ' ' + tr('c_hours') });
    }

    function toast(msg, kind) {
        const el = document.createElement('div');
        el.className = 'cache-toast ' + (kind || '');
        el.setAttribute('role', 'status');
        el.textContent = msg;
        document.body.appendChild(el);
        setTimeout(() => el.remove(), kind === 'err' ? 7000 : 3500);
    }

    async function getCsrf(force) {
        if (S.csrf && !force) return S.csrf;
        const r = await fetch('/api/cache/csrf', { credentials: 'same-origin' });
        S.csrf = (await r.json()).token;
        return S.csrf;
    }

    async function api(path, opts, retried) {
        opts = opts || {};
        const init = { method: opts.method || 'GET', credentials: 'same-origin', headers: {} };
        if (init.method !== 'GET') {
            init.headers['X-CSRF-Token'] = await getCsrf();
            if (opts.body !== undefined) {
                init.headers['Content-Type'] = 'application/json';
                init.body = JSON.stringify(opts.body);
            }
        }
        const r = await fetch('/api/cache' + path, init);
        let data = null;
        try { data = await r.json(); } catch (e) { /* empty */ }
        if (r.status === 403 && !retried && init.method !== 'GET') {
            await getCsrf(true);            // dashboard restarted -> new token
            return api(path, opts, true);
        }
        if (!r.ok) throw new Error((data && data.error) || ('HTTP ' + r.status));
        return data;
    }

    function qs(o) {
        return Object.keys(o).filter((k) => o[k] !== undefined && o[k] !== null && o[k] !== '')
            .map((k) => encodeURIComponent(k) + '=' + encodeURIComponent(o[k])).join('&');
    }

    function applyExtraI18n(root) {
        (root || document).querySelectorAll('[data-i18n-placeholder]').forEach((el) => { el.placeholder = t(el.dataset.i18nPlaceholder); });
        (root || document).querySelectorAll('[data-i18n-title]').forEach((el) => { el.title = t(el.dataset.i18nTitle); });
    }

    // -----------------------------------------------------------------------
    // Lifecycle (called by switchTab)
    // -----------------------------------------------------------------------
    window.cacheTabStart = async function () {
        cacheTabStop();
        if (!S.inited) {
            S.inited = true;
            applyExtraI18n($('tab-cache'));
        }
        await loadStatus();
        if (!S.status || !S.status.available) {
            S.timers.push(setInterval(loadStatus, 10000));
            return;
        }
        await Promise.all([loadProjects(), loadJobs(), loadSettings()]);
        // first run: nothing configured yet -> show the settings right away
        if (S.form && !S.form.instances.length && !S.settingsOpen) cacheToggleSettings(true);
        if (S.sel) await loadProject();
        S.timers.push(setInterval(loadStatus, 5000));
        S.timers.push(setInterval(loadJobs, 2000));
        S.timers.push(setInterval(loadLog, 1500));
        // no periodic rebuild while an editor is open (it would close dropdowns and
        // jump); loadJobs() refreshes the project when one of its jobs finishes
        S.timers.push(setInterval(() => { if (S.sel && !S.busy && !S.openLayer) loadProject(true); }, 10000));
        S.timers.push(setInterval(loadProjects, 60000));
    };

    window.cacheTabStop = function () {
        S.timers.forEach(clearInterval);
        S.timers = [];
    };

    window.cacheOnLanguage = function () {
        applyExtraI18n($('tab-cache'));
        if (!S.inited) return;
        renderStatus();
        renderSettings();
        renderProjects();
        renderProject();
        renderJobs();
        renderLogHead();
    };

    // -----------------------------------------------------------------------
    // Status cards
    // -----------------------------------------------------------------------
    async function loadStatus() {
        try {
            S.status = await api('/status');
        } catch (e) {
            S.status = { available: false, reason: e.message };
        }
        renderStatus();
    }

    function kv(label, value, raw) {
        return '<div class="cache-kv"><span>' + esc(label) + '</span><span>' + (raw ? value : esc(value)) + '</span></div>';
    }

    function renderStatus() {
        const st = S.status;
        const avail = st && st.available;
        $('cache-unavailable').style.display = avail ? 'none' : '';
        $('cache-main').style.display = avail ? '' : 'none';
        if (!avail) {
            $('cache-unavailable-reason').textContent = (st && st.reason) || '';
            return;
        }
        const w = st.worker || {};
        const cfg = st.config || {};
        const stateKey = 'c_state_' + (w.state || 'stopped');
        const dot = !w.online ? 'off' : (w.state === 'paused' ? 'paused' : 'on');
        let html = kv(tr('c_worker_status'), '<span class="cache-dot ' + dot + '"></span>' + esc(w.online ? tr(stateKey) : tr('c_offline')), true);
        if (w.user) html += kv(tr('c_worker_user'), w.user);
        html += kv(tr('c_worker_seen'), w.age_s != null ? tr('c_seconds_ago', { n: w.age_s }) : '–');
        if (w.started_at) html += kv(tr('c_worker_since'), fmtTime(w.started_at));
        if (w.job_id) html += kv(tr('c_worker_job'), '#' + w.job_id + (w.pause_reason ? ' (' + tr('c_pr_' + w.pause_reason) + ')' : ''));
        if (!w.online) html += '<div class="cache-warn err">' + esc(tr('c_worker_offline_hint')) + '</div>';
        $('cache-worker-box').innerHTML = html;

        const lp = cfg.load_pause || {};
        let lhtml = kv(tr('c_cpu'), w.cpu_percent != null ? w.cpu_percent + ' %' : '–');
        lhtml += kv(tr('c_p95'), w.user_p95_ms != null ? fmtNum(Math.round(w.user_p95_ms)) + ' ms' : '–');
        lhtml += kv(tr('c_load_pause'), lp.enabled === false ? tr('c_load_off') : tr('c_load_rule', { cpu: lp.cpu_percent, p95: fmtNum(lp.user_p95_ms) }));
        const win = (cfg.windows || []).length
            ? (cfg.windows.join(', ') + ' · ' + (w.in_window ? tr('c_window_open') : tr('c_window_closed')))
            : tr('c_window_always');
        lhtml += kv(tr('c_window'), win);
        const dp = cfg.default_pool ? (cfg.pools || []).find((p) => p.name === cfg.default_pool) : null;
        lhtml += kv(tr('c_default_pool'), dp ? dp.name + ' (' + dp.url + ')' : tr('c_instance_default'));
        const q = st.queue || {};
        lhtml += kv(tr('c_queue'), tr('c_queue_val', { r: q.running || 0, q: q.queued || 0, p: q.paused || 0 }));
        $('cache-load-box').innerHTML = lhtml;

        let ihtml = '';
        if (!(st.instances || []).length) ihtml = '<div class="cache-hint">' + esc(tr('c_no_instances')) + '</div>';
        (st.instances || []).forEach((i) => {
            const env = i.env || {};
            let badges = '';
            if (env.lizmap_version) badges += '<span class="cache-badge">Lizmap ' + esc(env.lizmap_version) + '</span> ';
            if (env.cache_storage_type) badges += '<span class="cache-badge">' + esc(tr('c_cache')) + ': ' + esc(env.cache_storage_type) + '</span> ';
            if (env.lizmap_version) badges += webpBadge(env, tr('c_webp_ok'));
            ihtml += '<div class="cache-kv"><span>' + esc(i.name) + '</span><span>' + badges + '</span></div>';
            ihtml += '<div class="cache-muted cache-small" style="margin:-2px 0 6px;">' + esc(tr('c_refreshed')) + ': ' + esc(i.refreshed_at ? fmtTime(i.refreshed_at) : tr('c_never')) + '</div>';
            if (i.error) ihtml += '<div class="cache-warn err">' + esc(i.error) + '</div>';
        });
        $('cache-instances-box').innerHTML = ihtml;
        updateRefreshButtons();
    }

    // WebP support is informational: it only matters for layers that use image/webp
    // (those get a red warning in the layer editor), so neutral grey unless ok
    function webpBadge(env, okLabel) {
        const tip = ' title="' + esc(tr('c_webp_tip')) + '"';
        if (env.webp_cache_fix && env.gd_webp) return '<span class="cache-badge green"' + tip + '>' + esc(okLabel) + '</span>';
        return '<span class="cache-badge"' + tip + '>' + esc(tr(env.webp_cache_fix ? 'c_webp_nogd' : 'c_webp_nofix')) + '</span>';
    }

    function updateRefreshButtons() {
        const job = (S.jobs.active || []).find((j) => j.kind === 'refresh');
        const none = !((S.status && S.status.instances) || []).length;
        ['cache-refresh-btn', 'cache-refresh-full-btn'].forEach((id) => { const b = $(id); if (b) b.disabled = !!job || none; });
        const hint = $('cache-refresh-hint');
        if (hint) hint.textContent = none ? tr('c_refresh_hint_none') : (job ? tr('c_refresh_hint_busy', { id: job.id }) : '');
    }

    window.cacheRefreshInventory = async function (force) {
        try {
            const r = await api('/jobs', { method: 'POST', body: { kind: 'refresh', force: !!force } });
            toast(tr('c_toast_queued', { id: r.id }), 'ok');
            S.logJob = r.id; S.logLastId = 0;
            await loadJobs();
            await loadLog();
        } catch (e) { toast(tr('c_err', { e: e.message }), 'err'); }
    };

    // -----------------------------------------------------------------------
    // Settings: instances, pools, operation (stored in the cache DB; the worker
    // validates them again against the boundaries of its root-owned file)
    // -----------------------------------------------------------------------
    S.settingsOpen = false;
    S.formDirty = false;
    S.poolTests = {};        // url -> {state: running|ok|err, msg}
    S.discoverJob = null;

    async function loadSettings(force) {
        try {
            const d = await api('/settings');
            S.settingsData = d;
            if (!S.formDirty || force || !S.form) { S.form = JSON.parse(JSON.stringify(d.settings)); S.formDirty = false; }
        } catch (e) { S.settingsData = null; }
        renderSettings();
    }

    window.cacheToggleSettings = function (open) {
        S.settingsOpen = typeof open === 'boolean' ? open : !S.settingsOpen;
        $('cache-settings-body').style.display = S.settingsOpen ? '' : 'none';
        $('cache-settings-caret').textContent = S.settingsOpen ? '▾' : '▸';
        $('cache-settings-toggle').setAttribute('aria-expanded', S.settingsOpen ? 'true' : 'false');
        if (S.settingsOpen) renderSettings();
    };

    function markDirty() {
        S.formDirty = true;
        const el = document.getElementById('cache-set-dirty');
        if (el) el.textContent = '● ' + tr('s_unsaved');
    }

    async function pollJob(id, onDone, tries) {
        tries = tries || 0;
        try {
            const d = await api('/jobs/' + id + '/log?after=999999999');
            if (['done', 'failed', 'cancelled'].includes(d.job.status)) { onDone(d.job); return; }
        } catch (e) { onDone({ status: 'failed', message: e.message }); return; }
        if (tries > 600) { onDone({ status: 'failed', message: 'timeout' }); return; }
        setTimeout(() => pollJob(id, onDone, tries + 1), 1000);
    }

    function poolSuggestions() {
        const disc = (S.settingsData && S.settingsData.discovery && S.settingsData.discovery.candidates) || [];
        const have = new Set((S.form.pools || []).map((p) => p.url.replace(/\/+$/, '')));
        const byUrl = {};
        disc.forEach((c) => {
            const u = c.env && c.env.wms_server_url;
            if (!u) return;
            const k = u.replace(/\/+$/, '');
            if (have.has(k)) return;
            (byUrl[u] = byUrl[u] || []).push(c.name);
        });
        return Object.keys(byUrl).map((u) => ({ url: u, names: byUrl[u] }));
    }

    function renderSettingsSummary() {
        const el = $('cache-settings-summary');
        if (!el || !S.form) return;
        el.textContent = '— ' + tr('s_summary', { i: S.form.instances.length, p: S.form.pools.length, d: S.form.default_pool || tr('s_none') });
    }

    function renderSettings() {
        renderSettingsSummary();
        const body = $('cache-settings-body');
        if (!body || !S.settingsOpen) return;
        const d = S.settingsData;
        if (!d || !S.form) { body.innerHTML = '<div class="no-data">…</div>'; return; }
        const f = S.form;
        const b = d.boundaries || {};
        const workerOnline = S.status && S.status.worker && S.status.worker.online;
        const discovering = !!S.discoverJob;
        let h = '<div class="cache-info">' + esc(tr('s_bounds', {
            b: (b.instances_base || []).join(', '),
            p: b.allow_remote_pools ? tr('s_bounds_any') : tr('s_bounds_local'),
            f: (d.config_path || '/etc/lizmap-cache-worker.json') + (d.config_file_exists === false ? ' ' + tr('s_file_missing') : ''),
        })) + '</div>';
        if ((d.worker_errors || []).length) {
            h += '<div class="cache-warn err">' + esc(tr('s_worker_errors')) + '<ul style="margin:4px 0 0 18px;">'
                + d.worker_errors.map((e) => '<li>' + esc(e) + '</li>').join('') + '</ul></div>';
        }

        // --- instances ---------------------------------------------------
        h += '<div class="cache-set-section"><h3>' + esc(tr('s_inst_title')) + '</h3>'
            + '<div class="cache-muted cache-small">' + esc(tr('s_inst_help')) + '</div>'
            + '<div class="cache-actions" style="margin-top:8px;"><button class="cache-btn" data-s="discover"' + (discovering ? ' disabled' : '') + '>'
            + esc(discovering ? tr('s_discovering') : tr('s_discover')) + '</button>'
            + '<span class="cache-muted cache-small">' + esc(d.discovery ? tr('s_last_discovery', { t: fmtTime(d.discovery.ts) }) : tr('s_no_discovery')) + '</span>'
            + (!workerOnline ? '<span class="cache-small" style="color:#fcd34d;">' + esc(tr('s_offline_discover')) + '</span>' : '')
            + '</div>';
        const cands = (d.discovery && d.discovery.candidates) || [];
        const rows = cands.map((c) => ({ c, found: true }));
        f.instances.forEach((i) => { if (!cands.some((c) => c.lizmap_path === i.lizmap_path)) rows.push({ c: { name: i.name, lizmap_path: i.lizmap_path }, found: false }); });
        if (rows.length) {
            h += '<div style="overflow-x:auto;"><table class="cache-set-table"><thead><tr><th>' + esc(tr('s_col_manage')) + '</th><th>' + esc(tr('s_col_name'))
                + '</th><th>' + esc(tr('s_col_version')) + '</th><th>' + esc(tr('s_col_qgis')) + '</th><th>' + esc(tr('s_col_cache')) + '</th><th>'
                + esc(tr('s_col_webp')) + '</th><th>' + esc(tr('s_col_path')) + '</th></tr></thead><tbody>';
            rows.forEach(({ c, found }) => {
                const managed = f.instances.some((i) => i.lizmap_path === c.lizmap_path);
                const env = c.env || {};
                const webp = c.env ? webpBadge(env, 'ok') : '';
                h += '<tr class="' + (managed ? '' : 'off') + '"><td><input type="checkbox" data-s="inst" data-path="' + esc(c.lizmap_path) + '" data-name="' + esc(c.name) + '"'
                    + (managed ? ' checked' : '') + (c.error && !managed ? ' disabled' : '') + ' aria-label="' + esc(tr('s_col_manage') + ' ' + c.name) + '"></td>'
                    + '<td><b>' + esc(c.name) + '</b></td>'
                    + '<td>' + esc(env.lizmap_version || '–') + '</td>'
                    + '<td class="cache-small">' + esc(env.wms_server_url || '–') + '</td>'
                    + '<td class="cache-small">' + esc(env.cache_storage_type ? env.cache_storage_type + (env.cache_storage_type !== 'redis' ? ' · ' + env.cache_root_directory : '') : '–') + '</td>'
                    + '<td>' + webp + '</td>'
                    + '<td class="cache-small cache-muted">' + esc(c.lizmap_path)
                    + (c.error ? '<div style="color:#fca5a5;">' + esc(c.error) + '</div>' : '')
                    + (!found ? '<div style="color:#fcd34d;">' + esc(tr('s_not_found')) + '</div>' : '') + '</td></tr>';
            });
            h += '</tbody></table></div>';
        }
        h += '</div>';

        // --- pools -------------------------------------------------------
        const monitorPools = d.monitor_pools || [];
        h += '<div class="cache-set-section"><h3>' + esc(tr('s_pools_title')) + '</h3>'
            + '<div class="cache-muted cache-small">' + esc(tr('s_pools_help')) + '</div>';
        if (f.pools.length) {
            h += '<div style="overflow-x:auto;"><table class="cache-set-table"><thead><tr><th>' + esc(tr('s_col_name')) + '</th><th>' + esc(tr('s_col_url'))
                + '</th><th title="' + esc(tr('s_col_monitor_tip')) + '">' + esc(tr('s_col_monitor')) + ' ⓘ</th><th>' + esc(tr('s_col_default')) + '</th><th>'
                + esc(tr('s_col_test')) + '</th><th></th></tr></thead><tbody>';
            f.pools.forEach((p, i) => {
                const t = S.poolTests[p.url];
                const tres = !t ? '' : t.state === 'running' ? '<span class="cache-muted">' + esc(tr('s_testing')) + '</span>'
                    : '<span class="' + (t.state === 'ok' ? 'cache-test-ok' : 'cache-test-err') + '">' + (t.state === 'ok' ? '✓ ' : '✗ ') + esc(t.msg || '') + '</span>';
                h += '<tr><td style="min-width:150px;"><input class="cache-input" data-s="pool" data-i="' + i + '" data-k="name" value="' + esc(p.name) + '" aria-label="' + esc(tr('s_col_name')) + '"></td>'
                    + '<td style="min-width:240px;"><input class="cache-input" data-s="pool" data-i="' + i + '" data-k="url" value="' + esc(p.url) + '" spellcheck="false" placeholder="http://127.0.0.1:7004/" aria-label="' + esc(tr('s_col_url')) + '"></td>'
                    + '<td><select class="cache-select" data-s="pool" data-i="' + i + '" data-k="monitor_pool" aria-label="' + esc(tr('s_col_monitor')) + '"><option value="">–</option>'
                    + monitorPools.map((m) => '<option' + (m === p.monitor_pool ? ' selected' : '') + '>' + esc(m) + '</option>').join('') + '</select></td>'
                    + '<td style="text-align:center;"><input type="radio" name="cache-default-pool" data-s="default" data-i="' + i + '"' + (f.default_pool === p.name ? ' checked' : '') + ' aria-label="' + esc(tr('s_col_default')) + '"></td>'
                    + '<td class="cache-small" style="min-width:180px;"><button class="cache-btn icon" data-s="test" data-i="' + i + '"' + (t && t.state === 'running' ? ' disabled' : '') + '>' + esc(tr('s_test')) + '</button> ' + tres + '</td>'
                    + '<td><button class="cache-btn icon danger" data-s="rmpool" data-i="' + i + '" title="' + esc(tr('s_remove')) + '" aria-label="' + esc(tr('s_remove')) + '">✕</button></td></tr>';
            });
            h += '</tbody></table></div>'
                + '<label class="cache-check" style="margin-top:8px;"><input type="radio" name="cache-default-pool" data-s="default" data-i=""' + (!f.default_pool ? ' checked' : '') + '><span>' + esc(tr('s_no_default')) + '</span></label>';
        }
        h += '<div class="cache-actions" style="margin-top:8px;"><button class="cache-btn" data-s="addpool">' + esc(tr('s_add_pool')) + '</button></div>';
        const sugg = poolSuggestions();
        if (sugg.length) {
            h += '<div class="cache-small cache-muted" style="margin-top:8px;">' + esc(tr('s_suggest')) + '</div>'
                + sugg.map((s) => '<span class="cache-chip" role="button" tabindex="0" data-s="suggest" data-url="' + esc(s.url) + '">+ ' + esc(s.url) + ' <span class="cache-muted">(' + esc(s.names.join(', ')) + ')</span></span>').join('');
        }
        h += '</div>';

        // --- operation ---------------------------------------------------
        const lp = f.load_pause || {};
        h += '<div class="cache-set-section"><h3>' + esc(tr('s_ops_title')) + '</h3><div class="cache-set-grid">'
            + '<div class="cache-field"><label>' + esc(tr('s_windows')) + '</label><input class="cache-input" data-s="op" data-k="windows" value="' + esc((f.windows || []).join(', ')) + '" placeholder="20:00-06:00" aria-label="' + esc(tr('s_windows')) + '"><div class="sub">' + esc(tr('s_windows_sub')) + '</div></div>'
            + '<div class="cache-field"><label>' + esc(tr('s_cpu')) + '</label><input class="cache-input" type="number" min="10" max="100" data-s="op" data-k="cpu_percent" value="' + esc(lp.cpu_percent) + '" aria-label="' + esc(tr('s_cpu')) + '"></div>'
            + '<div class="cache-field"><label>' + esc(tr('s_p95')) + '</label><input class="cache-input" type="number" min="0" data-s="op" data-k="user_p95_ms" value="' + esc(lp.user_p95_ms) + '" aria-label="' + esc(tr('s_p95')) + '"></div>'
            + '<div class="cache-field"><label>' + esc(tr('s_resume')) + '</label><input class="cache-input" type="number" min="0" data-s="op" data-k="resume_after_s" value="' + esc(lp.resume_after_s) + '" aria-label="' + esc(tr('s_resume')) + '"></div>'
            + '<div class="cache-field"><label>' + esc(tr('s_inv_h')) + '</label><input class="cache-input" type="number" min="0" data-s="op" data-k="inventory_refresh_hours" value="' + esc(f.inventory_refresh_hours) + '" aria-label="' + esc(tr('s_inv_h')) + '"></div>'
            + '<div class="cache-field"><label>' + esc(tr('s_size_min')) + '</label><input class="cache-input" type="number" min="10" data-s="op" data-k="size_scan_interval_min" value="' + esc(f.size_scan_interval_min) + '" aria-label="' + esc(tr('s_size_min')) + '"></div>'
            + '<div class="cache-field"><label>' + esc(tr('s_hist')) + '</label><input class="cache-input" type="number" min="1" data-s="op" data-k="job_history_days" value="' + esc(f.job_history_days) + '" aria-label="' + esc(tr('s_hist')) + '"></div>'
            + '</div><label class="cache-check" style="margin-top:10px;"><input type="checkbox" data-s="op" data-k="load_enabled"' + (lp.enabled !== false ? ' checked' : '') + '><span>' + esc(tr('s_load_enabled')) + '</span></label></div>';

        // --- save --------------------------------------------------------
        h += '<div id="cache-set-errors"></div><div class="cache-actions">'
            + '<button class="cache-btn primary" data-s="save">' + esc(tr('s_save')) + '</button>'
            + '<button class="cache-btn" data-s="discard">' + esc(tr('s_discard')) + '</button>'
            + '<span class="cache-small" id="cache-set-dirty" style="color:#fcd34d;">' + (S.formDirty ? '● ' + esc(tr('s_unsaved')) : '') + '</span>'
            + (d.meta ? '<span class="cache-muted cache-small">' + esc(tr('s_saved_by', { u: d.meta.updated_by, t: fmtTime(d.meta.updated_at) })) + '</span>' : '')
            + '</div>';
        body.innerHTML = h;
        wireSettings(body);
    }

    function wireSettings(body) {
        const f = S.form;
        body.querySelectorAll('[data-s]').forEach((el) => {
            const kind = el.dataset.s;
            if (kind === 'inst') {
                el.addEventListener('change', () => {
                    const path = el.dataset.path;
                    f.instances = f.instances.filter((i) => i.lizmap_path !== path);
                    if (el.checked) f.instances.push({ name: el.dataset.name, lizmap_path: path });
                    markDirty(); renderSettings();
                });
            } else if (kind === 'pool') {
                el.addEventListener(el.tagName === 'SELECT' ? 'change' : 'input', () => {
                    const p = f.pools[+el.dataset.i];
                    const k = el.dataset.k;
                    if (k === 'name' && f.default_pool === p.name) f.default_pool = el.value.trim();
                    p[k] = k === 'monitor_pool' ? (el.value || null) : (k === 'name' ? el.value.trim() : el.value.trim());
                    markDirty(); renderSettingsSummary();
                });
            } else if (kind === 'default') {
                el.addEventListener('change', () => {
                    f.default_pool = el.dataset.i === '' ? null : f.pools[+el.dataset.i].name;
                    markDirty(); renderSettingsSummary();
                });
            } else if (kind === 'op') {
                el.addEventListener(el.type === 'checkbox' ? 'change' : 'input', () => {
                    const k = el.dataset.k;
                    f.load_pause = f.load_pause || {};
                    if (k === 'windows') f.windows = el.value.split(',').map((x) => x.trim()).filter(Boolean);
                    else if (k === 'load_enabled') f.load_pause.enabled = el.checked;
                    else if (['cpu_percent', 'user_p95_ms', 'resume_after_s'].includes(k)) f.load_pause[k] = el.value === '' ? null : Number(el.value);
                    else f[k] = el.value === '' ? null : Number(el.value);
                    markDirty();
                });
            } else if (kind === 'addpool') {
                el.addEventListener('click', () => { f.pools.push({ name: 'Pool ' + (f.pools.length + 1), url: 'http://127.0.0.1:', monitor_pool: null }); markDirty(); renderSettings(); });
            } else if (kind === 'rmpool') {
                el.addEventListener('click', () => {
                    const p = f.pools.splice(+el.dataset.i, 1)[0];
                    if (p && f.default_pool === p.name) f.default_pool = null;
                    markDirty(); renderSettings();
                });
            } else if (kind === 'suggest') {
                const add = () => {
                    let host = el.dataset.url;
                    try { const u = new URL(el.dataset.url); host = u.host; } catch (e) { /* keep url */ }
                    f.pools.push({ name: 'Pool ' + host, url: el.dataset.url, monitor_pool: null });
                    markDirty(); renderSettings();
                };
                el.addEventListener('click', add);
                el.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); add(); } });
            } else if (kind === 'test') {
                el.addEventListener('click', async () => {
                    const url = f.pools[+el.dataset.i].url;
                    S.poolTests[url] = { state: 'running' };
                    renderSettings();
                    try {
                        const r = await api('/jobs', { method: 'POST', body: { kind: 'pool_test', url } });
                        pollJob(r.id, (job) => {
                            S.poolTests[url] = { state: job.status === 'done' ? 'ok' : 'err', msg: job.message };
                            renderSettings();
                        });
                    } catch (e) { S.poolTests[url] = { state: 'err', msg: e.message }; renderSettings(); }
                });
            } else if (kind === 'discover') {
                el.addEventListener('click', async () => {
                    try {
                        const r = await api('/jobs', { method: 'POST', body: { kind: 'discover' } });
                        S.discoverJob = r.id;
                        S.logJob = r.id; S.logLastId = 0;
                        renderSettings(); loadJobs(); loadLog();
                        pollJob(r.id, async () => { S.discoverJob = null; await loadSettings(); });
                    } catch (e) { toast(tr('c_err', { e: e.message }), 'err'); }
                });
            } else if (kind === 'save') {
                el.addEventListener('click', saveSettings);
            } else if (kind === 'discard') {
                el.addEventListener('click', () => loadSettings(true));
            }
        });
    }

    async function saveSettings() {
        const box = $('cache-set-errors');
        box.innerHTML = '';
        try {
            const init = { method: 'POST', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': await getCsrf() },
                body: JSON.stringify({ settings: S.form }) };
            let r = await fetch('/api/cache/settings', init);
            if (r.status === 403) { init.headers['X-CSRF-Token'] = await getCsrf(true); r = await fetch('/api/cache/settings', init); }
            const d = await r.json();
            if (!r.ok) {
                box.innerHTML = '<div class="cache-warn err">' + esc(tr('s_errors')) + '<ul style="margin:4px 0 0 18px;">'
                    + (d.errors || [d.error]).map((e) => '<li>' + esc(e) + '</li>').join('') + '</ul></div>';
                return;
            }
            S.formDirty = false;
            toast(tr('s_saved'), 'ok');
            await loadSettings(true);
            setTimeout(() => { loadStatus(); loadProjects(); }, 4000);
        } catch (e) { toast(tr('c_err', { e: e.message }), 'err'); }
    }

    // -----------------------------------------------------------------------
    // Project list
    // -----------------------------------------------------------------------
    async function loadProjects() {
        try { S.projects = await api('/projects'); } catch (e) { S.projects = []; }
        renderProjects();
    }

    window.cacheRenderProjects = renderProjects;
    function renderProjects() {
        const box = $('cache-proj-list');
        if (!box) return;
        const q = ($('cache-proj-search').value || '').toLowerCase();
        const only = $('cache-only-seedable').checked;
        const list = S.projects.filter((p) => (!only || p.tile_layers > 0 || p.error)
            && (!q || (p.title + ' ' + p.project + ' ' + p.repository + ' ' + (p.repo_label || '') + ' ' + p.instance).toLowerCase().includes(q)));
        if (!list.length) { box.innerHTML = '<div class="no-data">' + esc(tr('c_no_projects')) + '</div>'; return; }
        const multiInst = new Set(S.projects.map((p) => p.instance)).size > 1;
        let html = '', group = null;
        list.forEach((p) => {
            const g = (multiInst ? p.instance + ' · ' : '') + (p.repo_label || p.repository);
            if (g !== group) { group = g; html += '<div class="cache-proj-group">' + esc(g) + '</div>'; }
            const active = S.sel && S.sel.instance === p.instance && S.sel.repository === p.repository && S.sel.project === p.project;
            let meta = [];
            if (p.error) meta.push('⚠️ ' + tr('c_proj_error'));
            meta.push(tr('c_tile_layers', { n: p.tile_layers }));
            if (p.bytes) meta.push(fmtBytes(p.bytes));
            if (p.schedules) meta.push('⏱ ' + p.schedules);
            html += '<div class="cache-proj-item' + (active ? ' active' : '') + (p.tile_layers ? '' : ' dim') + '" tabindex="0" role="button"'
                + ' data-i="' + esc(p.instance) + '" data-r="' + esc(p.repository) + '" data-p="' + esc(p.project) + '">'
                + '<div class="t">' + esc(p.title) + (p.title !== p.project ? ' <span class="cache-muted cache-small">(' + esc(p.project) + ')</span>' : '') + '</div>'
                + '<div class="m">' + esc(meta.join(' · ')) + '</div></div>';
        });
        box.innerHTML = html;
        box.querySelectorAll('.cache-proj-item').forEach((el) => {
            const go = () => selectProject(el.dataset.i, el.dataset.r, el.dataset.p);
            el.addEventListener('click', go);
            el.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); go(); } });
        });
    }

    async function selectProject(instance, repository, project) {
        S.sel = { instance, repository, project };
        S.openLayer = null;
        S.project = null;
        renderProjects();
        await loadProject();
    }

    // -----------------------------------------------------------------------
    // Project detail + tree
    // -----------------------------------------------------------------------
    async function loadProject(background) {
        if (!S.sel) return;
        // keep the editor's inputs untouched while the user types
        if (background && document.activeElement && $('cache-tree-body') && $('cache-tree-body').contains(document.activeElement)
            && /INPUT|SELECT/.test(document.activeElement.tagName)) return;
        try {
            const p = await api('/project?' + qs(S.sel));
            S.project = p;
            const refs = (p.tile_matrix_sets || []).map((t) => t.ref);
            if (!S.viewTms || !refs.includes(S.viewTms)) {
                // prefer the grid that already has cache on disk, then the project CRS, then the first
                const withCache = refs.find((r) => allTiles(p.tree).some((n) => (tmsOf(n.tile, r) || {}).size && tmsOf(n.tile, r).size.files));
                S.viewTms = withCache || (refs.includes(p.projection) ? p.projection : refs[0]) || null;
            }
        } catch (e) {
            S.project = { error: e.message, tree: [], tile_matrix_sets: [] };
        }
        renderProject();
    }

    function allTiles(nodes, out) {
        out = out || [];
        (nodes || []).forEach((n) => { if (n.tile) out.push(n); if (n.children) allTiles(n.children, out); });
        return out;
    }
    function tmsOf(tile, ref) { return tile ? (tile.tile_matrix_sets || []).find((x) => x.ref === ref) : null; }

    window.cacheSetViewTms = function (ref) { S.viewTms = ref; renderProject(); };

    function renderProject() {
        const p = S.project;
        $('cache-project-empty').style.display = p ? 'none' : '';
        $('cache-project').style.display = p ? '' : 'none';
        if (!p) return;
        $('cache-project-title').textContent = p.title || (S.sel && S.sel.project) || '';
        const meta = [p.repo_label || p.repository, p.project, p.projection, p.qgs_path,
            tr('c_refreshed') + ': ' + fmtTime(p.refreshed_at)].filter(Boolean);
        $('cache-project-meta').textContent = meta.join(' · ');
        const tmsSel = $('cache-view-tms');
        tmsSel.innerHTML = (p.tile_matrix_sets || []).map((t) => '<option value="' + esc(t.ref) + '"' + (t.ref === S.viewTms ? ' selected' : '') + '>' + esc(t.ref) + '</option>').join('');
        let warn = '';
        if (p.error) warn += '<div class="cache-warn err">' + esc(tr('c_w_proj_error', { e: p.error })) + '</div>';
        if (p.env && p.env.cache_storage_type === 'redis') warn += '<div class="cache-warn">' + esc(tr('c_w_storage_redis')) + '</div>';
        $('cache-project-warnings').innerHTML = warn;
        renderTree();
    }

    function levelClass(lv) {
        if (lv.jobs && lv.jobs.length) return 'active';
        if (!lv.seeded_at) return '';
        if (lv.expired) return 'expired';
        if (lv.seeded_errors) return 'errors';
        return 'seeded';
    }

    function levelTip(lv) {
        let s = tr('c_level_tip', { z: lv.id, scale: fmtScale(lv.scale), tiles: fmtNum(lv.tiles) });
        if (lv.seeded_at) {
            s += '\n' + tr('c_level_seeded', { t: fmtTime(lv.seeded_at), fmt: lv.seeded_format || '?' });
            if (lv.seeded_bbox) s += ' · BBox';
            if (lv.expired) s += ' · ' + tr('c_level_expired');
            if (lv.seeded_errors) s += ' · ' + tr('c_level_errors', { n: lv.seeded_errors });
        } else s += '\n' + tr('c_level_none');
        if (lv.jobs && lv.jobs.length) s += '\n' + tr('c_level_active', { ids: lv.jobs.join(', #') });
        return s;
    }

    function levelBar(tile, sel) {
        const tm = tmsOf(tile, S.viewTms);
        if (!tm) return '<span class="cache-hint">' + esc(tr('c_not_in_tms')) + '</span>';
        return '<span class="cache-levels">' + tm.levels.map((lv) => {
            const inSel = sel && sel.tms === S.viewTms && lv.id >= sel.zmin && lv.id <= sel.zmax;
            return '<span class="cache-lv ' + levelClass(lv) + (inSel ? ' sel' : '') + '" title="' + esc(levelTip(lv)) + '">' + lv.id + '</span>';
        }).join('') + '</span>';
    }

    function sizeCell(tile) {
        const tm = tmsOf(tile, S.viewTms);
        if (!tm) return '–';
        if (S.project.env && S.project.env.cache_storage_type === 'redis') return '<span class="cache-hint">' + esc(tr('c_size_redis')) + '</span>';
        const sz = tm.size;
        if (!sz || sz.bytes == null) return '–';
        return '<span title="' + esc(tr('c_scanned') + ': ' + fmtTime(sz.scanned_at)) + '">' + esc(fmtBytes(sz.bytes))
            + (sz.files != null ? '<br><span class="cache-muted cache-small">' + esc(tr('c_files', { n: fmtNum(sz.files) })) + '</span>' : '') + '</span>';
    }

    function scheduleCell(tile) {
        const scheds = (tile.tile_matrix_sets || []).filter((x) => x.schedule);
        if (!scheds.length) return '–';
        return scheds.map((x) => {
            const s = x.schedule;
            return '<span class="cache-badge ' + (s.enabled ? 'blue' : '') + '" title="' + esc(tr('c_sched_next', { t: fmtTime(s.next_run) })) + '">'
                + esc(x.ref + ': ' + (s.enabled ? fmtInterval(s.interval_hours) : tr('c_paused_sched'))) + '</span>';
        }).join('<br>');
    }

    function statusHint(status) {
        return { not_cached: 'c_st_not_cached', single_tile: 'c_st_single_tile', overview: 'c_st_overview',
            caps_missing: 'c_st_caps_missing', not_in_cfg: 'c_st_not_in_cfg' }[status];
    }

    function renderTree() {
        const body = $('cache-tree-body');
        const rowsHtml = [];
        const walk = (nodes, depth, path) => {
            (nodes || []).forEach((n) => {
                const key = path + '/' + n.name;
                const isGroup = n.type === 'group';
                const collapsed = isGroup && S.collapsed.has(key);
                const pad = 'padding-left:' + (8 + depth * 18) + 'px';
                const caret = isGroup ? '<span class="cache-caret">' + (collapsed ? '▸' : '▾') + '</span>' : '<span class="cache-caret"></span>';
                const label = esc(n.title || n.name) + (n.title && n.title !== n.name ? ' <span class="cache-muted cache-small">(' + esc(n.name) + ')</span>' : '');
                if (n.tile) {
                    const open = S.openLayer === n.tile.name;
                    const sel = open ? S.editor[n.tile.name] : null;
                    const viewTm = tmsOf(n.tile, S.viewTms);
                    const hasWarn = (n.tile.warnings || []).length > 0 || (viewTm && (viewTm.warnings || []).length > 0);
                    const warnIcon = hasWarn ? ' ⚠️' : '';
                    rowsHtml.push('<tr class="row seedable' + (open ? ' open' : '') + (isGroup ? ' group' : '') + '" tabindex="0" data-layer="' + esc(n.tile.name) + '" data-key="' + esc(key) + '">'
                        + '<td style="' + pad + '">' + caret + label + warnIcon + '</td>'
                        + '<td><span class="cache-badge">' + esc(shortFmt(n.tile.image_format)) + '</span></td>'
                        + '<td>' + esc(fmtTTL(n.tile.ttl)) + '</td>'
                        + '<td>' + levelBar(n.tile, sel) + '</td>'
                        + '<td>' + sizeCell(n.tile) + '</td>'
                        + '<td>' + scheduleCell(n.tile) + '</td></tr>');
                    if (open) rowsHtml.push(editorRow(n));
                } else if (isGroup) {
                    rowsHtml.push('<tr class="row group" tabindex="0" data-key="' + esc(key) + '"><td colspan="6" style="' + pad + '">' + caret + label + '</td></tr>');
                } else {
                    rowsHtml.push('<tr class="row dim"><td style="' + pad + '">' + caret + label + '</td>'
                        + '<td>' + (n.image_format ? '<span class="cache-badge">' + esc(shortFmt(n.image_format)) + '</span>' : '') + '</td>'
                        + '<td colspan="4" class="cache-hint">' + esc(tr(statusHint(n.status) || 'c_st_not_in_cfg')) + '</td></tr>');
                }
                if (isGroup && !collapsed) walk(n.children, depth + 1, key);
            });
        };
        walk(S.project.tree, 0, '');
        body.innerHTML = rowsHtml.join('') || '<tr><td colspan="6" class="no-data">' + esc(tr('c_no_projects')) + '</td></tr>';
        body.querySelectorAll('tr.row').forEach((tr_) => {
            const act = (ev) => {
                if (ev.target.closest('.cache-caret') && tr_.dataset.key && tr_.classList.contains('group')) { toggleGroup(tr_.dataset.key); return; }
                if (tr_.dataset.layer) toggleEditor(tr_.dataset.layer);
                else if (tr_.dataset.key) toggleGroup(tr_.dataset.key);
            };
            tr_.addEventListener('click', act);
            tr_.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') { ev.preventDefault(); act(ev); } });
        });
        if (S.openLayer) wireEditor();
    }

    function shortFmt(f) {
        if (!f) return '?';
        return f.replace('image/', '').replace('; mode=8bit', ' 8bit').toUpperCase();
    }

    function toggleGroup(key) {
        if (S.collapsed.has(key)) S.collapsed.delete(key); else S.collapsed.add(key);
        renderTree();
    }

    function findTileNode(name) { return allTiles(S.project.tree).find((n) => n.tile.name === name); }

    function defaultEditor(tile) {
        const ref = tmsOf(tile, S.viewTms) ? S.viewTms : tile.tile_matrix_sets[0].ref;
        const tm = tmsOf(tile, ref);
        const lv = tm.levels;
        const zmin = lv[0].id;
        let zmax = zmin, acc = 0;
        for (const l of lv) { acc += l.tiles; if (acc > 20000) break; zmax = l.id; }
        const cfg = (S.status && S.status.config) || {};
        const sched = tm.schedule;
        return {
            tms: ref, zmin: sched ? sched.zmin : zmin, zmax: sched ? sched.zmax : zmax, bbox: sched && sched.bbox ? sched.bbox : '',
            pool: sched && sched.pool ? sched.pool : (cfg.default_pool || ''), force: false, window_only: false,
            interval: sched ? sched.interval_hours : 168, sched_enabled: sched ? !!sched.enabled : true,
            sched_window: sched ? !!sched.window_only : true,
        };
    }

    function toggleEditor(name) {
        if (S.openLayer === name) { S.openLayer = null; renderTree(); return; }
        S.openLayer = name;
        const node = findTileNode(name);
        if (node && !S.editor[name]) S.editor[name] = defaultEditor(node.tile);
        renderTree();
        requestEstimate();
    }

    function levelOptions(tm, selected) {
        return tm.levels.map((l) => '<option value="' + l.id + '"' + (l.id === selected ? ' selected' : '') + '>'
            + l.id + ' · 1:' + fmtScale(l.scale) + ' · ' + fmtNum(l.tiles) + '</option>').join('');
    }

    function editorRow(n) {
        const tile = n.tile;
        const ed = S.editor[tile.name];
        const tm = tmsOf(tile, ed.tms) || tile.tile_matrix_sets[0];
        const cfg = (S.status && S.status.config) || {};
        const pools = cfg.pools || [];
        const sched = tm.schedule;
        let warn = '';
        (tile.warnings || []).forEach((w) => { warn += '<div class="cache-warn' + (w === 'webp_no_fix' ? ' err' : '') + '">' + esc(tr('c_w_' + w)) + '</div>'; });
        (tm.warnings || []).forEach((w) => { warn += '<div class="cache-warn">' + esc(tr('c_w_' + w)) + '</div>'; });
        const intervalDays = ed.interval % 24 === 0;
        return '<tr class="cache-editor" data-editor="' + esc(tile.name) + '"><td colspan="6">'
            + warn
            + '<div class="cache-editor-grid">'
            + '<div class="cache-field"><label>' + esc(tr('c_ed_tms')) + '</label><select class="cache-select" data-f="tms" aria-label="' + esc(tr('c_ed_tms')) + '">'
            + tile.tile_matrix_sets.map((x) => '<option' + (x.ref === ed.tms ? ' selected' : '') + '>' + esc(x.ref) + '</option>').join('') + '</select></div>'
            + '<div class="cache-field"><label>' + esc(tr('c_ed_from')) + '</label><select class="cache-select" data-f="zmin" aria-label="' + esc(tr('c_ed_from')) + '">' + levelOptions(tm, ed.zmin) + '</select></div>'
            + '<div class="cache-field"><label>' + esc(tr('c_ed_to')) + '</label><select class="cache-select" data-f="zmax" aria-label="' + esc(tr('c_ed_to')) + '">' + levelOptions(tm, ed.zmax) + '</select></div>'
            + '<div class="cache-field"><label>' + esc(tr('c_ed_bbox')) + '</label><input class="cache-input" data-f="bbox" aria-label="' + esc(tr('c_ed_bbox')) + '" value="' + esc(ed.bbox) + '" placeholder="xmin,ymin,xmax,ymax" spellcheck="false">'
            + '<div class="sub">' + esc(tr('c_ed_bbox_sub')) + ' (' + esc(ed.tms) + ')</div></div>'
            + '<div class="cache-field"><label>' + esc(tr('c_ed_pool')) + '</label><select class="cache-select" data-f="pool" aria-label="' + esc(tr('c_ed_pool')) + '">'
            + '<option value="">' + esc(tr('c_instance_default')) + (S.project.env && S.project.env.wms_server_url ? ' (' + esc(S.project.env.wms_server_url) + ')' : '') + '</option>'
            + pools.map((p) => '<option value="' + esc(p.name) + '"' + (p.name === ed.pool ? ' selected' : '') + '>' + esc(p.name + ' (' + p.url + ')') + '</option>').join('')
            + '</select></div>'
            + '</div>'
            + '<div class="cache-editor-grid">'
            + '<div class="cache-field"><label>' + esc(tr('c_ed_format')) + '</label><select class="cache-select" disabled aria-label="' + esc(tr('c_ed_format')) + '"><option>' + esc(tile.image_format || '?') + '</option></select>'
            + '<div class="sub">' + esc(tr('c_ed_format_hint')) + '</div></div>'
            + '<div class="cache-field"><label>' + esc(tr('c_ed_ttl')) + '</label><input class="cache-input" disabled aria-label="' + esc(tr('c_ed_ttl')) + '" value="' + esc(fmtTTL(tile.ttl)) + '">'
            + '<div class="sub">' + esc(tr('c_ed_ttl_hint')) + '</div></div>'
            + (tile.metatile_size ? '<div class="cache-field"><label>' + esc(tr('c_ed_metatile')) + '</label><input class="cache-input" disabled value="' + esc(tile.metatile_size) + '"></div>' : '')
            + '</div>'
            + '<label class="cache-check"><input type="checkbox" data-f="force"' + (ed.force ? ' checked' : '') + '><span>' + esc(tr('c_ed_force')) + '</span></label>'
            + ((cfg.windows || []).length ? '<label class="cache-check"><input type="checkbox" data-f="window_only"' + (ed.window_only ? ' checked' : '') + '><span>' + esc(tr('c_ed_window')) + ' (' + esc(cfg.windows.join(', ')) + ')</span></label>' : '')
            + '<div class="cache-estimate" data-estimate>…</div>'
            + '<div class="cache-actions">'
            + '<button class="cache-btn primary" data-act="seed">' + esc(ed.force ? tr('c_ed_reseed') : tr('c_ed_seed')) + '</button>'
            + '<button class="cache-btn danger" data-act="clean">' + esc(tr('c_ed_clean')) + '</button>'
            + '</div>'
            + '<div class="cache-section-title">' + esc(tr('c_sched_title')) + '</div>'
            + '<div class="cache-info">' + esc(tr('c_sched_uses')) + '</div>'
            + '<div class="cache-actions" style="margin-top:8px;">'
            + '<label class="cache-muted cache-small">' + esc(tr('c_sched_interval')) + '</label>'
            + '<input class="cache-input" type="number" min="1" step="1" style="width:90px;" data-f="interval_val" aria-label="' + esc(tr('c_sched_interval')) + '" value="' + (intervalDays ? ed.interval / 24 : ed.interval) + '">'
            + '<select class="cache-select" data-f="interval_unit"><option value="h"' + (intervalDays ? '' : ' selected') + '>' + esc(tr('c_hours')) + '</option>'
            + '<option value="d"' + (intervalDays ? ' selected' : '') + '>' + esc(tr('c_days')) + '</option></select>'
            + '<label class="cache-check"><input type="checkbox" data-f="sched_enabled"' + (ed.sched_enabled ? ' checked' : '') + '><span>' + esc(tr('c_sched_enabled')) + '</span></label>'
            + ((cfg.windows || []).length ? '<label class="cache-check"><input type="checkbox" data-f="sched_window"' + (ed.sched_window ? ' checked' : '') + '><span>' + esc(tr('c_ed_window')) + '</span></label>' : '')
            + '<button class="cache-btn" data-act="sched-save">' + esc(tr('c_sched_save')) + '</button>'
            + (sched ? '<button class="cache-btn danger" data-act="sched-delete">' + esc(tr('c_sched_delete')) + '</button>' : '')
            + '</div>'
            + (sched ? '<div class="cache-muted cache-small" style="margin-top:6px;">' + esc(tr('c_sched_next', { t: fmtTime(sched.next_run) }))
                + (sched.last_run ? ' · ' + esc(tr('c_sched_last', { t: fmtTime(sched.last_run) })) : '') + '</div>' : '')
            + '</td></tr>';
    }

    function wireEditor() {
        const row = document.querySelector('tr.cache-editor');
        if (!row) return;
        const name = row.dataset.editor;
        const ed = S.editor[name];
        row.addEventListener('click', (ev) => ev.stopPropagation());
        row.querySelectorAll('[data-f]').forEach((el) => {
            el.addEventListener(el.tagName === 'INPUT' && el.type !== 'checkbox' ? 'input' : 'change', () => {
                const f = el.dataset.f;
                if (el.type === 'checkbox') ed[f] = el.checked;
                else if (f === 'zmin' || f === 'zmax') ed[f] = parseInt(el.value, 10);
                else if (f === 'interval_val' || f === 'interval_unit') {
                    const v = parseFloat(row.querySelector('[data-f=interval_val]').value) || 1;
                    ed.interval = row.querySelector('[data-f=interval_unit]').value === 'd' ? v * 24 : v;
                } else ed[f] = el.value;
                if (f === 'zmin' && ed.zmax < ed.zmin) ed.zmax = ed.zmin;
                if (f === 'zmax' && ed.zmin > ed.zmax) ed.zmin = ed.zmax;
                if (f === 'tms') {
                    const tm = tmsOf(findTileNode(name).tile, ed.tms);
                    ed.zmin = Math.max(ed.zmin, tm.levels[0].id); ed.zmax = Math.min(ed.zmax, tm.levels[tm.levels.length - 1].id);
                    if (ed.zmax < ed.zmin) ed.zmax = ed.zmin;
                    S.viewTms = ed.tms;
                }
                if (['tms', 'zmin', 'zmax', 'force'].includes(f)) {
                    renderProject();      // level highlight, option lists, button label
                    const again = document.querySelector('tr.cache-editor [data-f="' + f + '"]');
                    if (again) again.focus();
                }
                if (['tms', 'zmin', 'zmax', 'bbox'].includes(f)) requestEstimate();
            });
        });
        row.querySelectorAll('[data-act]').forEach((b) => b.addEventListener('click', () => editorAction(name, b.dataset.act, b)));
        if (S.lastEstimate && S.lastEstimate.layer === name) showEstimate(S.lastEstimate.data, S.lastEstimate.error);
    }

    function target(name) {
        const ed = S.editor[name];
        return Object.assign({}, S.sel, { layer: name, tms: ed.tms, zmin: ed.zmin, zmax: ed.zmax, bbox: (ed.bbox || '').trim() || null });
    }

    let estimateTimer = null;
    function requestEstimate() {
        clearTimeout(estimateTimer);
        estimateTimer = setTimeout(async () => {
            const name = S.openLayer;
            if (!name) return;
            const seq = ++S.estimateSeq;
            let data = null, error = null;
            try { data = await api('/estimate?' + qs(target(name))); } catch (e) { error = e.message; }
            if (seq !== S.estimateSeq || S.openLayer !== name) return;
            S.lastEstimate = { layer: name, data, error };
            showEstimate(data, error);
        }, 250);
    }

    function showEstimate(d, error) {
        const el = document.querySelector('tr.cache-editor [data-estimate]');
        if (!el) return;
        if (error) { el.innerHTML = '<span style="color:#fca5a5;">' + esc(error) + '</span>'; return; }
        if (!d) { el.textContent = '…'; return; }
        const parts = [tr('c_ed_estimate', { tiles: fmtNum(d.tiles), levels: d.levels.length })];
        if (d.est_bytes != null) parts.push(tr('c_ed_est_size', { size: fmtBytes(d.est_bytes) }));
        parts.push(d.est_seconds != null ? tr('c_ed_est_time', { time: fmtDur(d.est_seconds) }) : tr('c_ed_est_unknown'));
        el.innerHTML = esc(parts.join(' · ')) + (d.tiles > 200000 ? '<div style="color:#fcd34d; margin-top:4px;">' + esc(tr('c_ed_est_big')) + '</div>' : '');
    }

    async function editorAction(name, act, btn) {
        const ed = S.editor[name];
        btn.disabled = true;
        S.busy = true;
        try {
            if (act === 'seed') {
                const r = await api('/jobs', { method: 'POST', body: Object.assign(target(name), { kind: 'seed', pool: ed.pool || null, force: !!ed.force, window_only: !!ed.window_only }) });
                toast(tr('c_toast_queued', { id: r.id }), 'ok');
                S.logJob = r.id; S.logLastId = 0;
            } else if (act === 'clean') {
                if (!confirm(tr('c_w_clean_all'))) return;
                const r = await api('/jobs', { method: 'POST', body: Object.assign({}, S.sel, { kind: 'clean', layer: name }) });
                toast(tr('c_toast_queued', { id: r.id }), 'ok');
                S.logJob = r.id; S.logLastId = 0;
            } else if (act === 'sched-save') {
                const r = await api('/schedules', { method: 'POST', body: Object.assign(target(name), { interval_hours: ed.interval, pool: ed.pool || null, enabled: !!ed.sched_enabled, window_only: !!ed.sched_window }) });
                toast(tr('c_toast_saved', { t: fmtTime(r.next_run) }), 'ok');
            } else if (act === 'sched-delete') {
                const tm = tmsOf(findTileNode(name).tile, ed.tms);
                if (tm && tm.schedule) { await api('/schedules/' + tm.schedule.id, { method: 'DELETE' }); toast(tr('c_toast_deleted'), 'ok'); }
            }
            await Promise.all([loadJobs(), loadProject()]);
            loadLog();
        } catch (e) {
            toast(tr('c_err', { e: e.message }), 'err');
        } finally {
            S.busy = false;
            btn.disabled = false;
        }
    }

    // -----------------------------------------------------------------------
    // Queue
    // -----------------------------------------------------------------------
    async function loadJobs() {
        const before = new Map((S.jobs.active || []).map((j) => [j.id, j]));
        try { S.jobs = await api('/jobs?limit=30'); } catch (e) { return; }
        // a job of the shown project finished or moved to the next level -> refresh its tree
        if (S.sel) {
            const nowActive = new Map(S.jobs.active.map((j) => [j.id, j]));
            const changed = [...before.values()].some((j) => j.project === S.sel.project && j.repository === S.sel.repository
                && j.instance === S.sel.instance && (!nowActive.has(j.id) || nowActive.get(j.id).current_level !== j.current_level));
            if (changed && !S.busy) loadProject(true);   // true = keep a focused input untouched
        }
        if (!S.logJob) {
            const run = S.jobs.active.find((j) => j.status === 'running') || S.jobs.active[0] || S.jobs.recent[0];
            if (run) { S.logJob = run.id; S.logLastId = 0; }
        }
        renderJobs();
        updateRefreshButtons();
    }

    window.cacheRenderJobs = renderJobs;
    function jobStatusBadge(j) {
        const w = (S.status && S.status.worker) || {};
        let key = 'c_js_' + j.status, cls = '';
        if (j.status === 'queued' && j.window_only && w.in_window === false) key = 'c_js_queued_window';
        if (j.status === 'running') cls = 'blue';
        else if (j.status === 'paused') cls = 'amber';
        else if (j.status === 'done') cls = j.errors ? 'amber' : 'green';
        else if (j.status === 'failed') cls = 'red';
        let s = '<span class="cache-badge ' + cls + '">' + esc(tr(key)) + (j.status === 'paused' && j.pause_reason ? ' · ' + esc(tr('c_pr_' + j.pause_reason)) : '') + '</span>';
        if (j.message && j.status !== 'running') s += '<div class="cache-muted cache-small" style="max-width:280px; overflow-wrap:anywhere;">' + esc(j.message) + '</div>';
        return s;
    }

    function jobTarget(j) {
        if (j.kind === 'refresh') return '<b>' + esc(tr('c_job_refresh')) + '</b> · ' + esc(j.instance || tr('c_all_instances'));
        let s = '<b>' + esc(tr('c_job_' + j.kind)) + '</b> · ' + esc(j.project) + ' / ' + esc(j.layer);
        if (j.kind === 'seed') {
            s += '<div class="cache-muted cache-small">' + esc(j.tms) + (j.bbox ? ' · BBox' : '') + (j.pool ? ' · ' + esc(j.pool) : '')
                + (j.force ? ' · ' + esc(tr('c_force_badge')) : '') + (j.window_only ? ' · ' + esc(tr('c_window_badge')) : '')
                + (j.schedule_id ? ' · ⏱' : '') + '</div>';
        }
        return s;
    }

    function jobProgress(j) {
        if (j.kind === 'refresh') {
            return j.tiles_total ? esc(tr('c_projects_n', { d: j.tiles_done || 0, t: j.tiles_total })) : '';
        }
        if (j.kind !== 'seed' || !j.tiles_total) return '';
        const pct = j.percent || 0;
        let s = '<span class="cache-pbar' + (j.status === 'paused' ? ' paused' : '') + '"><div style="width:' + pct + '%"></div></span> '
            + '<span class="cache-small">' + pct + ' %</span><div class="cache-muted cache-small">' + fmtNum(j.tiles_done) + ' / ' + fmtNum(j.tiles_total)
            + (j.current_level != null && ['running', 'paused'].includes(j.status) ? ' · z' + j.current_level : '')
            + (j.eta_s != null ? ' · ' + esc(tr('c_eta', { t: fmtDur(j.eta_s) })) : '') + '</div>';
        if (j.errors) s += '<div class="cache-small" style="color:#fca5a5;">' + esc(tr('c_errors_n', { n: fmtNum(j.errors) })) + '</div>';
        return s;
    }

    function jobActions(j) {
        if (!['queued', 'running', 'paused'].includes(j.status)) return '';
        let s = '';
        if (j.status === 'paused') s += '<button class="cache-btn icon" data-job="' + j.id + '" data-a="resume" title="' + esc(tr('c_act_resume')) + '" aria-label="' + esc(tr('c_act_resume')) + '">▶</button> ';
        else if (j.kind === 'seed') s += '<button class="cache-btn icon" data-job="' + j.id + '" data-a="pause" title="' + esc(tr('c_act_pause')) + '" aria-label="' + esc(tr('c_act_pause')) + '">⏸</button> ';
        s += '<button class="cache-btn icon danger" data-job="' + j.id + '" data-a="cancel" title="' + esc(tr('c_act_cancel')) + '" aria-label="' + esc(tr('c_act_cancel')) + '">✕</button>';
        return s;
    }

    function renderJobs() {
        const body = $('cache-jobs-body');
        if (!body) return;
        const list = S.jobs.active.concat($('cache-show-recent').checked ? S.jobs.recent : []);
        if (!list.length) { body.innerHTML = '<tr><td colspan="7" class="no-data">' + esc(tr('c_no_jobs')) + '</td></tr>'; return; }
        body.innerHTML = list.map((j) => '<tr data-id="' + j.id + '" class="' + (j.id === S.logJob ? 'sel' : '') + '">'
            + '<td>' + j.id + '</td><td>' + jobTarget(j) + '</td>'
            + '<td>' + (j.kind === 'seed' ? esc(j.zmin + '–' + j.zmax) : '') + '</td>'
            + '<td>' + jobStatusBadge(j) + '</td><td>' + jobProgress(j) + '</td>'
            + '<td class="username cache-small">' + esc(j.created_by || '') + '<div class="cache-muted">' + esc(fmtTime(j.created_at)) + '</div></td>'
            + '<td style="white-space:nowrap;">' + jobActions(j) + '</td></tr>').join('');
        body.querySelectorAll('tr[data-id]').forEach((row) => row.addEventListener('click', () => {
            const id = parseInt(row.dataset.id, 10);
            if (S.logJob !== id) { S.logJob = id; S.logLastId = 0; $('cache-log').innerHTML = ''; loadLog(); renderJobs(); }
        }));
        body.querySelectorAll('button[data-job]').forEach((b) => b.addEventListener('click', async (ev) => {
            ev.stopPropagation();
            const id = b.dataset.job, a = b.dataset.a;
            if (a === 'cancel' && !confirm(tr('c_confirm_cancel', { id }))) return;
            b.disabled = true;
            try { await api('/jobs/' + id + '/action', { method: 'POST', body: { action: a } }); setTimeout(loadJobs, 600); }
            catch (e) { toast(tr('c_err', { e: e.message }), 'err'); b.disabled = false; }
        }));
    }

    // -----------------------------------------------------------------------
    // Live log
    // -----------------------------------------------------------------------
    let logInFlight = false;
    async function loadLog() {
        if (!S.logJob || logInFlight) { renderLogHead(); return; }
        logInFlight = true;
        const jobId = S.logJob;
        try {
            const d = await api('/jobs/' + jobId + '/log?after=' + S.logLastId);
            if (jobId !== S.logJob) return;
            S.logJobInfo = d.job;
            const box = $('cache-log');
            if (S.logLastId === 0) box.innerHTML = '';
            const follow = $('cache-log-follow').checked;
            const frag = document.createDocumentFragment();
            d.lines.forEach((l) => {
                const div = document.createElement('div');
                div.className = 'ln ' + (l.level || 'info') + (l.line.startsWith('──') ? ' step' : '');
                const ts = document.createElement('span');
                ts.className = 'ts';
                ts.textContent = (l.ts || '').slice(11);
                div.appendChild(ts);
                div.appendChild(document.createTextNode(l.line));
                frag.appendChild(div);
                S.logLastId = Math.max(S.logLastId, l.id);
            });
            box.appendChild(frag);
            if (!box.childNodes.length) box.innerHTML = '<div class="cache-hint">' + esc(tr('c_log_empty')) + '</div>';
            if (follow && d.lines.length) box.scrollTop = box.scrollHeight;
        } catch (e) { /* job may have been purged */ } finally { logInFlight = false; }
        renderLogHead();
    }

    function renderLogHead() {
        const head = $('cache-log-head');
        if (!head) return;
        const j = S.logJobInfo && S.logJobInfo.id === S.logJob ? S.logJobInfo : null;
        if (!S.logJob) { head.textContent = tr('c_log_pick'); return; }
        if (!j) { head.textContent = '#' + S.logJob; return; }
        const tgt = j.kind === 'refresh' ? tr('c_job_refresh') : (j.project + ' / ' + j.layer + (j.tms ? ' · ' + j.tms : ''));
        head.textContent = '#' + j.id + ' · ' + tr('c_job_' + j.kind) + ' · ' + tgt + ' · ' + tr('c_js_' + j.status);
    }

    applyTranslations();
})();
