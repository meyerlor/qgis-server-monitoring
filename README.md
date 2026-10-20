# QGIS Server Monitoring Dashboard

A web-based monitoring dashboard designed to track **QGIS Server request performance** across multiple server pools. Parses QGIS Server logs in real time, records every `GetMap` request with its response time, project, user, and layer information, and stores everything in SQLite for long-term analysis. Helps you identify slow projects, heavy users, peak-hour bottlenecks, and per-pool performance differences — so you can find the culprits behind slow map rendering.

System metrics (CPU, RAM) are collected as a secondary data source for correlation.

> **Note:** This project has been developed and tested with [Py-QGIS-Server](https://github.com/3liz/py-qgis-server). If you are running a "pure" QGIS Server setup (e.g. spawned via FastCGI/Apache), the log format and service unit names may differ — small adjustments to the pool configuration and log-parsing patterns in `monitor.py` will be needed.

![Python](https://img.shields.io/badge/python-3.8+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## Features

### Request performance monitoring
- **Per-pool response times** — Live average, P95, min/max across configurable time windows (10 min, 30 min, 1 h, 24 h)
- **Slowest requests** — Rolling top-5 slowest `GetMap` requests per pool with project, user, and layer details
- **Historical analytics** — Performance trends, peak-hour request volume, day-of-week patterns, per-project rankings, pool comparison
- **Request history** — Searchable log of individual requests with filters for project, pool, and time range
- **User & project breakdowns** — Most active users, slowest projects, request counts

### Dynamic pool support
- **Configure any number of pools** — Just edit the `QGIS_POOLS` dictionary; the entire dashboard (cards, tables, charts, filters) adapts automatically
- **Pool comparison chart** — Side-by-side response time comparison across all configured pools

### Usage log (Nutzungsprotokoll)
- **Print layout tracking** — `GetPrint` and `GetPrintAtlas` requests are logged with the layout name (`TEMPLATE`) and the layers used, stored in separate columns
- **Lizmap WFS-T editing** — Edits made through QGIS Desktop (WFS Transaction with INSERT/UPDATE/DELETE in the request) are detected and logged with action type and layer name
- **Lizmap web edits** — Lizmap saves web edits through its PHP edition controller directly into the database, so they never reach the QGIS Server logs. Saves (`POST …/edition/saveFeature`, create or update) and deletes are read from the nginx access log; the user is correlated with the QGIS Server log.
- **Feature info & GetFeature** — `GetFeatureInfo` and `GetFeature` requests are tracked per user, project, and layer

### Tile cache management (Cache-Verwaltung)
- **Seed Lizmap's tile cache from the dashboard** — projects are discovered through each configured Lizmap instance (repositories from its own configuration, including access-restricted projects); the layer tree is shown in QGIS order and grouping
- **Per layer**: tile matrix set, zoom level range (with scale and tile count per level), optional bbox, tile estimate, "recompute" (overwrite) and clear cache
- **What is cached**: seeded levels per tile matrix set (with date, image format, errors and TTL expiry), cache size and tile count measured on disk
- **Schedules**: recompute a layer automatically every N hours/days
- **Background queue** with live log; one job at a time, split per zoom level (interrupted jobs resume at the level where they stopped)
- **Production safeguards**: tile requests can go to a **dedicated QGIS Server pool**; jobs pause automatically while the server is busy (CPU / users' p95 response time) and can be limited to a time window; manual pause / resume / cancel
- **Read-only .cfg**: image format and cache expiry are set in the Lizmap plugin; the dashboard shows them (greyed out) with a hint, and warns about WebP on Lizmap versions without the WebP cache fix
- The dashboard never runs commands itself: a separate worker service, running as Lizmap's PHP user, executes the jobs (see [Tile cache worker](#tile-cache-worker))

### Filtering & i18n
- **Admin / anonymous toggles** — Include or exclude admin and anonymous users from all statistics (default: excluded)
- **German / English** — Full UI localization with language switcher, persisted in browser
- **Hide usernames** — Toggle next to the language switcher blurs all usernames across the entire dashboard (useful for presentations); state is persisted in browser

### Infrastructure
- **SQLite storage** — Persistent data with configurable retention (default: 180 days for requests, 30 days for system metrics)
- **Log rotation handling** — Supports both `create` and `copytruncate` rotation methods
- **Live system metrics** — CPU, memory usage pushed via WebSocket (secondary to request performance)
- **Single-page dashboard** — Charts (Chart.js) with real-time updates (Socket.IO)

## Screenshots

**Live Monitor — per-pool response times & slowest requests:**

<img width="1549" height="983" alt="grafik" src="https://github.com/user-attachments/assets/6211f58f-880e-4be8-8056-d7856ecd46c6" />

**Request History — individual requests, most active users & projects:**

<img width="945" height="1004" alt="grafik" src="https://github.com/user-attachments/assets/0410896b-af5e-4d19-9ed8-7be5554da2a1" />

**Analytics — performance trends, peak hours, project rankings, pool comparison:**

<img width="936" height="888" alt="grafik" src="https://github.com/user-attachments/assets/a288cab2-fad4-4179-8367-815b30a5fc9a" />


**System History (CPU & RAM):**

<img width="1504" height="898" alt="grafik" src="https://github.com/user-attachments/assets/bc78828a-367d-49d0-a488-773acda886da" />


**Deep Usage insight:**

<img width="1488" height="995" alt="grafik" src="https://github.com/user-attachments/assets/97e45b88-b0e7-4396-a347-cb18a2307683" />


## Prerequisites

### 1. QGIS Server logging

The monitoring dashboard relies on detailed QGIS Server log output. Make sure debug-level logging and request profiling are enabled.

**Py-QGIS-Server** — in your `server.conf`:

```ini
[logging]
level = debug
```

**QGIS Server environment** — in your `qgis-service.env` (or equivalent systemd `EnvironmentFile`):

```bash
QGIS_SERVER_LOG_LEVEL=0
QGIS_SERVER_LOG_PROFILE=TRUE
```

### 2. Log rotation for QGIS logs

QGIS Server logs grow very fast at debug level. Set up logrotate to keep them under control:

```bash
sudo nano /etc/logrotate.d/qgis-server
```

```
/var/log/qgis/qgis-server.log {
    daily
    rotate 2
    copytruncate
    compress
    delaycompress
    missingok
    notifempty
    dateext
    dateformat -%Y%m%d
}
```

> If you run multiple pools, add a similar entry for each pool log file (e.g. `qgis-server-pool2.log`, `qgis-server-pool3.log`).

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/meyerlor/qgis-server-monitoring.git
cd qgis-server-monitoring
```

### 2. Install dependencies

```bash
pip install flask flask-socketio eventlet psutil
```

### 3. Configure

Open `monitor.py` and edit the **CONFIGURATION** section at the top of the file. The most important settings are:

| Variable | Default | Description |
|---|---|---|
| `HOST` | `0.0.0.0` | Listen address |
| `PORT` | `8080` | Listen port |
| `DB_PATH` | `/opt/monitoring/monitoring.db` | SQLite database location |
| `REQUEST_RETENTION_DAYS` | `180` | Days to keep request data |
| `SYSTEM_METRICS_RETENTION_DAYS` | `30` | Days to keep system metrics |
| `DEBUG_LOG` | `/var/log/monitoring-debug.log` | Debug log path (`None` to disable) |
| `QGIS_POOLS` | 3 pools | Dict of pool name → service unit + log file |
| `PHP_FPM_SERVICE_UNIT` | `php8.3-fpm.service` | systemd unit for PHP-FPM |
| `PHP_FPM_LOG_FILE` | `/var/log/php8.3-fpm.log` | PHP-FPM log path |

#### Adding / removing pools

Edit the `QGIS_POOLS` dictionary. Each entry maps a pool name to its systemd service unit and log file path. The dashboard adapts automatically — no frontend changes needed.

```python
QGIS_POOLS = {
    'qgis-pool1': {
        'service_unit': 'qgis.service',
        'log_file': '/var/log/qgis/qgis-server.log',
    },
    # Add or remove pools as needed — the dashboard will
    # generate cards, tables, filters, and charts dynamically.
}
```

### 4. Prepare directories

```bash
# Create database directory
sudo mkdir -p /opt/monitoring
sudo chown $USER:$USER /opt/monitoring

# Create debug log (optional)
sudo touch /var/log/monitoring-debug.log
sudo chown $USER:$USER /var/log/monitoring-debug.log
```

### 5. Run

```bash
python3 monitor.py
```

Then open `http://<your-server-ip>:8080` in a browser.

## Production Deployment

For a production setup it is recommended to run the dashboard as a systemd service.

### Create a systemd service

```ini
# /etc/systemd/system/monitoring-dashboard.service
[Unit]
Description=QGIS Server Monitoring Dashboard
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/monitoring
ExecStart=/usr/bin/python3 /opt/monitoring/monitor.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now monitoring-dashboard.service
```

#### Start / Stop / Restart

```bash
sudo systemctl stop monitoring-dashboard.service
sudo systemctl start monitoring-dashboard.service
sudo systemctl restart monitoring-dashboard.service
```

Check the service status and logs:

```bash
sudo systemctl status monitoring-dashboard.service
sudo journalctl -u monitoring-dashboard.service -f
```

### Reverse proxy (Nginx)

It is **strongly recommended** to put the dashboard behind Nginx with password protection and SSL/TLS enabled.

#### Create HTTP Basic Auth credentials

```bash
sudo apt install apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd-monitoring admin
```

#### Enable SSL with Certbot

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.example.com
```

#### Example Nginx configuration

```nginx
server {
    server_name your-domain.example.com;

    # HTTP Basic Auth
    auth_basic "GIS Monitoring Dashboard";
    auth_basic_user_file /etc/nginx/.htpasswd-monitoring;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;

        # WebSocket support (required for live updates)
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        # Standard proxy headers
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Timeouts for long-lived connections
        proxy_read_timeout 300;
        proxy_connect_timeout 300;
        proxy_send_timeout 300;
    }

    # SSL configuration will be added by Certbot
    listen 443 ssl;
    # ssl_certificate     /etc/letsencrypt/live/your-domain.example.com/fullchain.pem;
    # ssl_certificate_key /etc/letsencrypt/live/your-domain.example.com/privkey.pem;
}

server {
    listen 80;
    server_name your-domain.example.com;
    return 301 https://$host$request_uri;
}
```

> Replace `your-domain.example.com` with your actual domain. After running `certbot --nginx`, the SSL certificate paths will be filled in automatically.

### Log file permissions

The monitoring process needs read access to the QGIS Server log files and the nginx access log. Logs it cannot read are skipped with a warning. Either run it as a user that has access, or add the service user to the appropriate group (nginx logs are usually `www-data:adm 640`):

```bash
sudo usermod -aG adm monitoring
```

### Tile cache worker

The *Cache-Verwaltung* tab needs a separate worker service. The dashboard only stores jobs in a shared SQLite database; the worker executes them with a small PHP helper (`cache/lizmap_cache_helper.php`) that boots each Lizmap installation exactly like Lizmap's own `console.php` and calls Lizmap's seeding classes. Nothing is installed into Lizmap and no Lizmap configuration file is changed. The only difference from `php console.php wmts:cache:seed` is that the helper can send the tile requests of its own process to another QGIS Server pool (`wmsServerURL` is replaced in memory only).

Requirements: Lizmap Web Client 3.8 or newer, Python 3.8+ (stdlib only, no venv needed), PHP CLI.

**1. Shared group and database directory** — the worker runs as Lizmap's PHP user (`www-data`), the dashboard as its own user; both need write access to the cache DB:

```bash
sudo groupadd --system lizcache
sudo usermod -aG lizcache www-data
sudo usermod -aG lizcache monitoring        # the dashboard's service user (or root)
sudo install -d -o www-data -g lizcache -m 2770 /var/lib/lizmap-cache
```

**2. Configuration** — copy `cache/lizmap-cache-worker.example.json` to `/etc/lizmap-cache-worker.json` and adjust:

| Key | Meaning |
|-----|---------|
| `instances` | One entry per Lizmap installation: `name` and `lizmap_path` (the `lizmap/` directory that contains `application.init.php`) |
| `pools` | QGIS Server pools the tile requests can be sent to (`name`, `url`). `monitor_pool` = the dashboard's pool key of the same pool, so its load is not counted as user load |
| `default_pool` | Pool used when a job does not choose one; omit to use each instance's own `wmsServerURL` |
| `windows` | Optional time windows (`"20:00-06:00"`). Jobs marked "only in time window" and all schedules (by default) only run inside them |
| `load_pause` | Pause seeding while the machine's CPU is above `cpu_percent` or the users' p95 response time (from the dashboard) is above `user_p95_ms`; resumes after `resume_after_s` below 80 % of the limits |

Check the configuration (as the worker user):

```bash
sudo -u www-data python3 /opt/monitoring/cache/cache_worker.py --config /etc/lizmap-cache-worker.json check
```

**3. Service** — install `cache/lizmap-cache-worker.service` (adjust paths/user if needed):

```bash
sudo cp /opt/monitoring/cache/lizmap-cache-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lizmap-cache-worker
journalctl -u lizmap-cache-worker -f
```

The worker reads all projects on its first start (see the queue in the tab). The dashboard finds the database at `/var/lib/lizmap-cache/cache.db`; set `MONITORING_CACHE_DB` in the dashboard's service to use another path. Restart the dashboard after adding the group so it picks up the new group membership.

Notes:
- Never run the worker or the helper as root: files created in Lizmap's temp and cache directories would become unwritable for Lizmap. Both refuse to start as root.
- Seeding uses the layer settings from the project's `.cfg` (server cache enabled, not *single tile*, image format, cache expiry). Change them in the Lizmap plugin; the tab shows why a layer cannot be seeded.
- Lizmap's cache keys are hashes, so which levels are cached cannot be read back from the cache. The tab shows the levels seeded through the worker; sizes include tiles cached by normal map use.
- With `cacheStorageType=redis`, sizes cannot be measured and "clear cache" flushes the whole project (a Lizmap limitation).
- Basic auth user names are recorded per job (the browser sends them to the dashboard through the proxy).

## Disclaimer

**USE AT YOUR OWN RISK.** This software is provided "as is", without warranty of any kind. See the [LICENSE](LICENSE) file for details.

## License

[MIT](LICENSE)
