# QGIS Server Monitoring Dashboard

Real-time monitoring dashboard for QGIS Server instances. Collects system metrics (CPU, RAM, Disk I/O), parses QGIS and PHP-FPM logs, tracks response times, and stores historical data in SQLite for analysis.

> **Note:** This project has been developed and tested with [Py-QGIS-Server](https://github.com/3liz/py-qgis-server). If you are running a "pure" QGIS Server setup (e.g. spawned via FastCGI/Apache), the log format and service unit names may differ — small adjustments to the pool configuration and log-parsing patterns in `monitor.py` will be needed.

![Python](https://img.shields.io/badge/python-3.8+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **Live system metrics** — CPU, memory, disk I/O, network usage pushed via WebSocket
- **QGIS log parsing** — Extracts response times, project names, users, and layer info from QGIS Server logs
- **Multi-pool support** — Monitor multiple QGIS Server pools side by side
- **PHP-FPM monitoring** — Tracks errors and warnings from PHP-FPM
- **Historical analytics** — Performance trends, peak-hour analysis, response-time distributions, per-project rankings
- **SQLite storage** — Persistent data with configurable retention periods
- **Log rotation handling** — Supports both `create` and `copytruncate` rotation methods
- **Web-based dashboard** — Single-page UI with charts (Chart.js) and real-time updates (Socket.IO)

> **Note:** Requests from the `admin` user and from users that are not logged in (`N/A`) are excluded from all aggregated statistics (performance trends, peak hours, project rankings, etc.). They will still appear in the raw Queries Log.

## Screenshots from the Dashboard:
**Overview:**

<img width="1549" height="983" alt="grafik" src="https://github.com/user-attachments/assets/6211f58f-880e-4be8-8056-d7856ecd46c6" />

**Single requests / most active users + projects**

<img width="945" height="1004" alt="grafik" src="https://github.com/user-attachments/assets/0410896b-af5e-4d19-9ed8-7be5554da2a1" />

**Deep performance analysis:**

<img width="936" height="882" alt="grafik" src="https://github.com/user-attachments/assets/f9ebc3ba-e548-4536-9174-6f1302221b42" />

**Historical system data (CPU+RAM):**
<img width="1504" height="898" alt="grafik" src="https://github.com/user-attachments/assets/bc78828a-367d-49d0-a488-773acda886da" />



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

Edit the `QGIS_POOLS` dictionary. Each entry maps a pool name to its systemd service unit and log file path:

```python
QGIS_POOLS = {
    'qgis-pool1': {
        'service_unit': 'qgis.service',
        'log_file': '/var/log/qgis/qgis-server.log',
    },
    # Add more pools here...
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

The monitoring process needs read access to the QGIS and PHP-FPM log files. Either run it as a user that has access, or add the service user to the appropriate group:

```bash
sudo usermod -aG adm www-data
```

## Disclaimer

**USE AT YOUR OWN RISK.** This software is provided "as is", without warranty of any kind. See the [LICENSE](LICENSE) file for details.

## License

[MIT](LICENSE)
