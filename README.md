# QGIS Server Monitoring Dashboard

Real-time monitoring dashboard for QGIS Server instances. Collects system metrics (CPU, RAM, Disk I/O), parses QGIS and PHP-FPM logs, tracks response times, and stores historical data in SQLite for analysis.

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

## Requirements

- Python 3.8+
- QGIS Server (with file-based or journalctl logging)
- The following Python packages:

```
flask
flask-socketio
eventlet
psutil
```

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
# /etc/systemd/system/qgis-monitoring.service
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
sudo systemctl enable --now qgis-monitoring.service
```

### Reverse proxy (Nginx)

If you want to serve the dashboard behind Nginx with WebSocket support:

```nginx
location /monitoring/ {
    proxy_pass http://127.0.0.1:8080/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
}
```

### Log file permissions

The monitoring process needs read access to the QGIS and PHP-FPM log files. Either run it as a user that has access, or add the service user to the appropriate group:

```bash
sudo usermod -aG adm www-data
```

## Disclaimer

**USE AT YOUR OWN RISK.** This software is provided "as is", without warranty of any kind. See the [LICENSE](LICENSE) file for details.

## License

[MIT](LICENSE)
