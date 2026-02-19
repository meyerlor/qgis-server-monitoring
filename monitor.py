#!/usr/bin/env python3
"""
QGIS Server Monitoring Dashboard
Collects CPU, RAM, Disk I/O metrics and parses QGIS/PHP logs in real-time.
Stores historical data in SQLite database for analysis.

Copyright (c) 2025 meyerlor
Released under the MIT License. See LICENSE file for details.

USE AT YOUR OWN RISK. This software is provided "as is", without warranty
of any kind. See LICENSE for the full disclaimer.
"""

from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit
import psutil
import time
from datetime import datetime, timedelta
import threading
import re
import os
from collections import deque
from pathlib import Path
import subprocess
import shlex
import sqlite3
import json
import sys

# =============================================================================
# CONFIGURATION - Adjust these variables to match your environment
# =============================================================================

# -- Network / Server --------------------------------------------------------
# Host and port the dashboard web server listens on
HOST = '0.0.0.0'
PORT = 8080

# Flask secret key (change this to a random string in production)
SECRET_KEY = 'your-secret-key-change-this'

# -- Database ----------------------------------------------------------------
# Path to the SQLite database file (will be created if it does not exist)
DB_PATH = '/opt/monitoring/monitoring.db'

# -- Data Retention ----------------------------------------------------------
# How long to keep request data in the database (in days)
REQUEST_RETENTION_DAYS = 180

# How long to keep system metrics in the database (in days)
SYSTEM_METRICS_RETENTION_DAYS = 30

# -- Debug Logging -----------------------------------------------------------
# Path to the debug log file (set to None to disable debug logging)
DEBUG_LOG = '/var/log/monitoring-debug.log'

# -- QGIS Server Pools ------------------------------------------------------
# Define your QGIS server pools here.  Each pool needs:
#   - a systemd service unit name (for journalctl)
#   - a log file path (fallback when journalctl is unavailable)
#
# Add or remove pools as needed. The pool names (keys) are used throughout
# the dashboard UI.

QGIS_POOLS = {
    'qgis-pool1': {
        'service_unit': 'qgis.service',
        'log_file': '/var/log/qgis/qgis-server.log',
    },
    'qgis-pool2': {
        'service_unit': 'qgis-pool2.service',
        'log_file': '/var/log/qgis/qgis-server-pool2.log',
    },
    'qgis-pool3': {
        'service_unit': 'qgis-pool3.service',
        'log_file': '/var/log/qgis/qgis-server-pool3.log',
    },
}

# -- PHP-FPM -----------------------------------------------------------------
# Systemd unit and log file for PHP-FPM monitoring
PHP_FPM_SERVICE_UNIT = 'php8.3-fpm.service'
PHP_FPM_LOG_FILE = '/var/log/php8.3-fpm.log'

# -- In-Memory Buffer Sizes -------------------------------------------------
# Maximum number of response-time entries kept in memory per pool
RESPONSE_TIMES_MAXLEN = 10000

# Maximum number of recent error/warning entries kept per pool
RECENT_ISSUES_MAXLEN = 20

# Number of slowest requests to track per pool (rolling window)
SLOWEST_REQUESTS_COUNT = 5

# Rolling window for slowest requests (in seconds, default 10 minutes)
SLOWEST_REQUESTS_WINDOW = 600

# -- Monitoring Intervals ----------------------------------------------------
# How often system metrics are pushed to clients (in seconds)
METRICS_PUSH_INTERVAL = 2

# How often aggregated log stats are pushed to clients (in seconds)
STATS_PUSH_INTERVAL = 5

# How often the cleanup job runs (in seconds, default 24 hours)
CLEANUP_INTERVAL = 86400

# Abandoned request tracking timeout (in seconds, default 5 minutes)
REQUEST_TRACKING_TIMEOUT = 300

# =============================================================================
# END OF CONFIGURATION
# =============================================================================

# Build derived lookup dicts from the pool configuration above
POOL_NAMES = list(QGIS_POOLS.keys())

SERVICE_UNITS = {name: cfg['service_unit'] for name, cfg in QGIS_POOLS.items()}
SERVICE_UNITS['php-fpm'] = PHP_FPM_SERVICE_UNIT

LOG_FILES_FALLBACK = {name: cfg['log_file'] for name, cfg in QGIS_POOLS.items()}
LOG_FILES_FALLBACK['php-fpm'] = PHP_FPM_LOG_FILE

def debug_log(message):
    """Write debug message to file"""
    if not DEBUG_LOG:
        return
    try:
        with open(DEBUG_LOG, 'a') as f:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            f.write(f"[{timestamp}] {message}\n")
            f.flush()
    except:
        pass  # Ignore errors in debug logging

app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

def init_database():
    """Initialize SQLite database with required tables"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Requests table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME NOT NULL,
            pool TEXT NOT NULL,
            project TEXT NOT NULL,
            user TEXT,
            layers TEXT,
            request_type TEXT,
            response_time_ms INTEGER NOT NULL,
            request_id TEXT
        )
    ''')
    
    # Indexes for better query performance
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON requests(timestamp)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_project ON requests(project)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_pool ON requests(pool)')
    
    # System metrics table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS system_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME NOT NULL,
            cpu_percent REAL,
            memory_percent REAL,
            memory_used_gb REAL,
            memory_available_gb REAL,
            memory_total_gb REAL,
            disk_read_mb REAL,
            disk_write_mb REAL,
            network_sent_mb REAL,
            network_recv_mb REAL
        )
    ''')
    
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sys_timestamp ON system_metrics(timestamp)')
    
    conn.commit()
    conn.close()
    debug_log(f"Database initialized at {DB_PATH}")
    print(f"Database initialized at {DB_PATH}")

# Initialize database on startup
init_database()

def save_request_to_db(pool, project, user, layers, request_type, response_time_ms, request_id):
    """Save a request to the database"""
    try:
        debug_log(f"DEBUG [DB] Attempting to save: pool={pool}, project={project}, user={user}, type={request_type}, time={response_time_ms}ms")
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO requests (timestamp, pool, project, user, layers, request_type, response_time_ms, request_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (datetime.now(), pool, project, user, layers, request_type, response_time_ms, request_id))
        conn.commit()
        conn.close()
        debug_log(f"DEBUG [DB] ✓ Successfully saved request to database")
    except Exception as e:
        debug_log(f"DEBUG [DB] ✗ ERROR saving request to DB: {e}")
        import traceback
        traceback.print_exc()

def save_system_metrics_to_db(cpu, mem_percent, mem_used_gb, mem_avail_gb, mem_total_gb, disk_read, disk_write, net_sent, net_recv):
    """Save system metrics to the database"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO system_metrics (timestamp, cpu_percent, memory_percent, memory_used_gb, 
                                       memory_available_gb, memory_total_gb)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (datetime.now(), cpu, mem_percent, mem_used_gb, mem_avail_gb, mem_total_gb))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error saving system metrics to DB: {e}")

def cleanup_old_data():
    """Remove data older than retention period"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Delete requests older than retention period
        cursor.execute("DELETE FROM requests WHERE timestamp < datetime('now', '-' || ? || ' days')", [REQUEST_RETENTION_DAYS])

        # Delete system metrics older than retention period
        cursor.execute("DELETE FROM system_metrics WHERE timestamp < datetime('now', '-' || ? || ' days')", [SYSTEM_METRICS_RETENTION_DAYS])
        
        deleted_requests = cursor.rowcount
        conn.commit()
        conn.close()
        
        if deleted_requests > 0:
            print(f"Cleaned up {deleted_requests} old records from database")
    except Exception as e:
        print(f"Error cleaning up old data: {e}")


# Global flags
monitoring_active = False
log_monitoring_active = False
cleanup_active = False

# Response time storage - keep last 24 hours with timestamps
response_times = {pool: deque(maxlen=RESPONSE_TIMES_MAXLEN) for pool in POOL_NAMES}

# Request details tracking - stores info about ongoing requests by request_id
current_requests = {pool: {} for pool in POOL_NAMES}

# Slowest requests in last N minutes
slowest_requests = {pool: [] for pool in POOL_NAMES}

# Statistics storage
log_stats = {pool: {'requests_total': 0, 'errors': 0, 'warnings': 0} for pool in POOL_NAMES}
log_stats['php-fpm'] = {'errors': 0, 'warnings': 0}

# Recent errors/warnings
recent_issues = {pool: deque(maxlen=RECENT_ISSUES_MAXLEN) for pool in POOL_NAMES}
recent_issues['php-fpm'] = deque(maxlen=RECENT_ISSUES_MAXLEN)

def get_system_metrics():
    """Collect current system metrics"""
    
    # CPU Usage (per core and total)
    cpu_percent = psutil.cpu_percent(interval=1, percpu=True)
    cpu_total = psutil.cpu_percent(interval=0)
    
    # Memory Usage
    memory = psutil.virtual_memory()
    
    # Disk I/O
    disk_io = psutil.disk_io_counters()
    
    # Network I/O (optional, but useful)
    net_io = psutil.net_io_counters()
    
    return {
        'timestamp': datetime.now().strftime('%H:%M:%S'),
        'cpu': {
            'total': cpu_total,
            'per_core': cpu_percent,
            'core_count': len(cpu_percent)
        },
        'memory': {
            'total_gb': round(memory.total / (1024**3), 2),
            'used_gb': round(memory.used / (1024**3), 2),
            'available_gb': round(memory.available / (1024**3), 2),
            'percent': memory.percent
        },
        'disk': {
            'read_mb': round(disk_io.read_bytes / (1024**2), 2),
            'write_mb': round(disk_io.write_bytes / (1024**2), 2),
            'read_count': disk_io.read_count,
            'write_count': disk_io.write_count
        },
        'network': {
            'sent_mb': round(net_io.bytes_sent / (1024**2), 2),
            'recv_mb': round(net_io.bytes_recv / (1024**2), 2)
        }
    }

def parse_qgis_log_line(line, log_name):
    """Parse a QGIS server log line and extract request details and response times"""
    now = time.time()
    
    # Extract request ID from line (format: [1660428])
    request_id_match = re.search(r'\[(\d+)\]', line)
    request_id = request_id_match.group(1) if request_id_match else None
    
    # DEBUG: Show every line that has a request ID
    if request_id and ('MAP:' in line or 'REQUEST:' in line or 'Request finished' in line.lower()):
        debug_log(f"DEBUG [{log_name}] Processing line with ID [{request_id}]: {line[:150]}")
    
    # Check for errors/warnings (store for display)
    if 'WARNING' in line or 'WARN' in line:
        log_stats[log_name]['warnings'] += 1
        recent_issues[log_name].append({
            'timestamp': datetime.now().strftime('%H:%M:%S'),
            'level': 'WARNING',
            'message': line.strip()[:200]  # Truncate long messages
        })
    elif 'ERROR' in line or 'CRITICAL' in line:
        log_stats[log_name]['errors'] += 1
        recent_issues[log_name].append({
            'timestamp': datetime.now().strftime('%H:%M:%S'),
            'level': 'ERROR',
            'message': line.strip()[:200]
        })
    
    # If we have a request ID, track request details
    if request_id:
        # Check if this is a new request starting (has "QGIS Request accepted" or first detail line)
        # Create a unique key combining request_id and timestamp to handle ID recycling
        is_new_request = False
        
        # Detect start of new request
        if 'QGIS Request accepted' in line or ('MAP:' in line and not any(request_id in k for k in current_requests[log_name].keys())):
            is_new_request = True
            debug_log(f"DEBUG [{log_name}] NEW REQUEST detected for ID [{request_id}]")
        
        # Find or create the tracking key for this request
        tracking_key = None
        if is_new_request:
            # Create new unique key: request_id + timestamp in milliseconds
            tracking_key = f"{request_id}_{int(now * 1000)}"
            current_requests[log_name][tracking_key] = {
                'map': None,
                'user': None,
                'layers': None,
                'request_type': None,
                'start_time': now,
                'raw_request_id': request_id
            }
            debug_log(f"DEBUG [{log_name}] Created tracking key: {tracking_key}")
        else:
            # Find most recent key for this request_id
            matching_keys = [k for k in current_requests[log_name].keys() if k.startswith(f"{request_id}_")]
            if matching_keys:
                # Get the most recent one (highest timestamp)
                tracking_key = max(matching_keys, key=lambda k: int(k.split('_')[1]))
            else:
                # No existing key found, create one anyway
                tracking_key = f"{request_id}_{int(now * 1000)}"
                current_requests[log_name][tracking_key] = {
                    'map': None,
                    'user': None,
                    'layers': None,
                    'request_type': None,
                    'start_time': now,
                    'raw_request_id': request_id
                }
                debug_log(f"DEBUG [{log_name}] No existing key, created: {tracking_key}")
        
        # Extract request details from DEBUG lines
        if 'MAP:' in line:
            map_match = re.search(r'MAP:([^\s]+)', line)
            if map_match:
                # Extract just the project name from path
                map_path = map_match.group(1)
                project_name = map_path.split('/')[-1].replace('.qgs', '')
                current_requests[log_name][tracking_key]['map'] = project_name
                debug_log(f"DEBUG [{log_name}] [{tracking_key}] Set MAP: {project_name}")
        
        elif 'LIZMAP_USER:' in line:
            user_match = re.search(r'LIZMAP_USER:([^\s]+)', line)
            if user_match:
                current_requests[log_name][tracking_key]['user'] = user_match.group(1)
                debug_log(f"DEBUG [{log_name}] [{tracking_key}] Set USER: {user_match.group(1)}")
        
        elif 'LAYERS:' in line:
            layers_match = re.search(r'LAYERS:(.+?)(?:\s|$)', line)
            if layers_match:
                layers = layers_match.group(1).strip()
                current_requests[log_name][tracking_key]['layers'] = layers
                debug_log(f"DEBUG [{log_name}] [{tracking_key}] Set LAYERS: {layers[:50]}...")
        
        elif 'REQUEST:' in line:
            request_match = re.search(r'REQUEST:([^\s]+)', line)
            if request_match:
                current_requests[log_name][tracking_key]['request_type'] = request_match.group(1).upper()
                debug_log(f"DEBUG [{log_name}] [{tracking_key}] Set REQUEST: {request_match.group(1).upper()}")
    
    # Check for "Request finished" to get complete request time
    if request_id and ('Request finished' in line or 'request finished' in line):
        time_match = re.search(r'(\d+)\s*ms', line)
        if time_match:
            response_time = int(time_match.group(1))
            debug_log(f"DEBUG [{log_name}] REQUEST FINISHED for ID [{request_id}] in {response_time}ms")
            
            # Store the response time with timestamp
            if response_time > 0:
                response_times[log_name].append((now, response_time))
                log_stats[log_name]['requests_total'] += 1
                
                # Find the most recent tracking key for this request_id
                matching_keys = [k for k in current_requests[log_name].keys() if k.startswith(f"{request_id}_")]
                
                debug_log(f"DEBUG [{log_name}] Found {len(matching_keys)} matching keys for [{request_id}]")
                
                if matching_keys:
                    # Get the most recent one (closest to "finished" time)
                    tracking_key = max(matching_keys, key=lambda k: int(k.split('_')[1]))
                    details = current_requests[log_name][tracking_key]
                    
                    debug_log(f"DEBUG [{log_name}] Using key: {tracking_key}")
                    debug_log(f"DEBUG [{log_name}] Details: MAP={details.get('map')}, USER={details.get('user')}, TYPE={details.get('request_type')}")
                    
                    # Only save GETMAP requests to database (not GetLegendGraphic, GetCapabilities, etc.)
                    request_type = details.get('request_type', '')
                    
                    # Safety check: Skip if request_type is None or empty
                    if not request_type:
                        debug_log(f"DEBUG [{log_name}] ✗ SKIPPING (no request type): MAP={details.get('map')}")
                    elif request_type.upper() == 'GETMAP':
                        debug_log(f"DEBUG [{log_name}] ✓ SAVING to DB (GETMAP): {details.get('map')}")
                        socketio.start_background_task(
                            save_request_to_db,
                            log_name,
                            details.get('map', 'Unknown'),
                            details.get('user', 'Unknown'),
                            details.get('layers', 'Unknown'),
                            details.get('request_type', 'Unknown'),
                            response_time,
                            request_id
                        )
                    else:
                        debug_log(f"DEBUG [{log_name}] ✗ SKIPPING (not GETMAP): {request_type.upper()}")
                    
                    # Add to slowest requests if it's in top 5 or list is not full
                    add_to_slowest(log_name, response_time, now, request_id, details)
                    
                    # Clean up finished request from tracking
                    del current_requests[log_name][tracking_key]
                    debug_log(f"DEBUG [{log_name}] Cleaned up tracking key: {tracking_key}")
                else:
                    debug_log(f"DEBUG [{log_name}] ✗ ERROR: No matching keys found!")
                    
                return True
    
    # Clean up old requests (probably incomplete/abandoned)
    cutoff = now - REQUEST_TRACKING_TIMEOUT
    to_delete = [key for key, data in current_requests[log_name].items() 
                 if data.get('start_time', now) < cutoff]
    if to_delete:
        debug_log(f"DEBUG [{log_name}] Cleaning up {len(to_delete)} old tracking keys")
    for key in to_delete:
        del current_requests[log_name][key]
    
    return False

def add_to_slowest(log_name, response_time, timestamp, request_id, details):
    """Add request to slowest requests list if it qualifies (only GETMAP requests)"""
    # Only track GETMAP requests (not GetLegendGraphic, GetCapabilities, etc.)
    request_type = details.get('request_type', '')
    
    # Safety check: Skip if request_type is None or empty
    if not request_type:
        return
    
    if request_type.upper() != 'GETMAP':
        return
    
    # Only keep requests within the configured window
    cutoff = timestamp - SLOWEST_REQUESTS_WINDOW
    
    # Remove old entries
    slowest_requests[log_name] = [
        r for r in slowest_requests[log_name]
        if r[1] >= cutoff  # r[1] is timestamp
    ]
    
    # Add new request
    request_entry = (
        response_time,
        timestamp,
        request_id,
        {
            'map': details.get('map', 'N/A'),
            'user': details.get('user', 'N/A'),
            'layers': details.get('layers', 'N/A'),
            'request_type': details.get('request_type', 'N/A')
        }
    )
    
    slowest_requests[log_name].append(request_entry)
    
    # Sort by response time (descending) and keep top 5
    slowest_requests[log_name].sort(reverse=True, key=lambda x: x[0])
    slowest_requests[log_name] = slowest_requests[log_name][:SLOWEST_REQUESTS_COUNT]

def parse_php_log_line(line, log_name):
    """Parse a PHP-FPM log line for errors and warnings"""
    
    if 'WARNING' in line or 'WARN' in line:
        log_stats[log_name]['warnings'] += 1
        recent_issues[log_name].append({
            'timestamp': datetime.now().strftime('%H:%M:%S'),
            'level': 'WARNING',
            'message': line.strip()[:200]
        })
    elif 'ERROR' in line or 'CRITICAL' in line or 'Fatal' in line:
        log_stats[log_name]['errors'] += 1
        recent_issues[log_name].append({
            'timestamp': datetime.now().strftime('%H:%M:%S'),
            'level': 'ERROR',
            'message': line.strip()[:200]
        })

def calculate_response_stats(log_name, seconds_ago):
    """Calculate average response time for a given time window"""
    now = time.time()
    cutoff = now - seconds_ago
    
    times_in_window = [
        rt for ts, rt in response_times[log_name]
        if ts >= cutoff
    ]
    
    if not times_in_window:
        return {
            'avg': 0,
            'min': 0,
            'max': 0,
            'count': 0,
            'p95': 0
        }
    
    times_sorted = sorted(times_in_window)
    count = len(times_sorted)
    
    return {
        'avg': round(sum(times_sorted) / count, 1),
        'min': min(times_sorted),
        'max': max(times_sorted),
        'count': count,
        'p95': times_sorted[int(count * 0.95)] if count > 0 else 0
    }

def calculate_response_stats_from_db(pool, seconds_ago):
    """Calculate response stats from database for longer timeframes"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cutoff_time = datetime.now() - timedelta(seconds=seconds_ago)
        
        cursor.execute('''
            SELECT 
                AVG(response_time_ms) as avg,
                MIN(response_time_ms) as min,
                MAX(response_time_ms) as max,
                COUNT(*) as count
            FROM requests
            WHERE pool = ? AND timestamp > ? AND request_type = 'GETMAP'
        ''', (pool, cutoff_time))
        
        result = cursor.fetchone()
        conn.close()
        
        if result and result[0] is not None:
            # Calculate p95 separately if needed (requires sorting)
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
                SELECT response_time_ms 
                FROM requests
                WHERE pool = ? AND timestamp > ? AND request_type = 'GETMAP'
                ORDER BY response_time_ms
            ''', (pool, cutoff_time))
            
            times = [row[0] for row in cursor.fetchall()]
            conn.close()
            
            p95 = times[int(len(times) * 0.95)] if times else 0
            
            return {
                'avg': round(result[0], 1),
                'min': result[1],
                'max': result[2],
                'count': result[3],
                'p95': p95
            }
        else:
            return {
                'avg': 0,
                'min': 0,
                'max': 0,
                'count': 0,
                'p95': 0
            }
    except Exception as e:
        debug_log(f"ERROR calculating stats from DB: {e}")
        return {
            'avg': 0,
            'min': 0,
            'max': 0,
            'count': 0,
            'p95': 0
        }

def tail_journalctl(log_name, service_unit, parser_func):
    """Tail systemd journal for a service using journalctl"""
    global log_monitoring_active
    
    print(f"Starting journalctl monitoring for {log_name} ({service_unit})")
    
    try:
        # Use journalctl to follow the service output
        # -u = unit, -f = follow, -n 0 = don't show old entries, -o cat = output without metadata
        cmd = ['journalctl', '-u', service_unit, '-f', '-n', '0', '-o', 'cat']
        
        # Start subprocess
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1
        )
        
        print(f"journalctl process started for {log_name} (PID: {process.pid})")
        
        # Read lines as they come
        while log_monitoring_active and process.poll() is None:
            line = process.stdout.readline()
            
            if line:
                parser_func(line, log_name)
            else:
                socketio.sleep(0.1)
        
        # Clean up
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
            
    except FileNotFoundError:
        print(f"Error: journalctl not found. Falling back to log file for {log_name}")
        # Fallback to file tailing if journalctl is not available
        if log_name in LOG_FILES_FALLBACK:
            tail_log_file_fallback(log_name, LOG_FILES_FALLBACK[log_name], parser_func)
    except Exception as e:
        print(f"Error in journalctl monitoring for {log_name}: {e}")

def tail_log_file_fallback(log_name, file_path, parser_func):
    """Fallback: Tail a log file directly using file operations with rotation handling"""
    global log_monitoring_active
    
    if not os.path.exists(file_path):
        debug_log(f"ERROR: Log file not found: {file_path}")
        print(f"Warning: Log file not found: {file_path}")
        return
    
    debug_log(f"TAIL THREAD STARTED for {log_name}: {file_path}")
    print(f"Using file fallback for {log_name}: {file_path}")
    
    line_count = 0
    last_rotation_check = time.time()
    current_inode = None
    current_size = None
    f = None
    
    try:
        # Open file and get initial inode + size (with UTF-8 error handling)
        f = open(file_path, 'r', encoding='utf-8', errors='replace')
        stat_info = os.fstat(f.fileno())
        current_inode = stat_info.st_ino
        current_size = stat_info.st_size
        
        # Go to end of file
        current_pos = f.seek(0, 2)
        debug_log(f"[{log_name}] Positioned at end of file (inode: {current_inode}, size: {current_size}, pos: {current_pos})")
        
        while log_monitoring_active:
            # Check if file was rotated (every 5 seconds)
            now = time.time()
            if now - last_rotation_check >= 5:
                last_rotation_check = now
                
                try:
                    # Get current inode and size of the file path
                    new_stat = os.stat(file_path)
                    new_inode = new_stat.st_ino
                    new_size = new_stat.st_size
                    
                    # Check for rotation via inode change (create method)
                    if new_inode != current_inode:
                        debug_log(f"[{log_name}] LOG ROTATION DETECTED (create)! Old inode: {current_inode}, New inode: {new_inode}")
                        debug_log(f"[{log_name}] Reopening {file_path}...")
                        
                        # Close old file
                        f.close()
                        
                        # Open new file (with UTF-8 error handling)
                        f = open(file_path, 'r', encoding='utf-8', errors='replace')
                        stat_info = os.fstat(f.fileno())
                        current_inode = stat_info.st_ino
                        current_size = stat_info.st_size
                        line_count = 0  # Reset counter for new file
                        
                        # Start from beginning of new file
                        f.seek(0, 2)  # Go to end
                        
                        debug_log(f"[{log_name}] Successfully reopened file (new inode: {current_inode}, size: {current_size})")
                        print(f"[{log_name}] Log rotation (create) handled, reopened {file_path}")
                    
                    # Check for rotation via size decrease (copytruncate method)
                    elif new_size < current_size:
                        debug_log(f"[{log_name}] LOG TRUNCATION DETECTED (copytruncate)! Old size: {current_size}, New size: {new_size}")
                        
                        # File was truncated - reposition to end
                        current_pos = f.tell()
                        f.seek(0, 2)  # Go to new end
                        new_pos = f.tell()
                        
                        current_size = new_size
                        line_count = 0  # Reset counter
                        
                        debug_log(f"[{log_name}] Repositioned after truncation (old pos: {current_pos}, new pos: {new_pos})")
                        print(f"[{log_name}] Log rotation (copytruncate) handled, repositioned in {file_path}")
                    
                    # Update size even if no rotation (file grows normally)
                    else:
                        current_size = new_size
                        
                except OSError as e:
                    # File might not exist during rotation moment
                    debug_log(f"[{log_name}] Warning during rotation check: {e}")
                    socketio.sleep(1)
                    continue
            
            # Read next line
            line = f.readline()
            
            if line:
                line_count += 1
                if line_count % 100 == 0:
                    debug_log(f"[{log_name}] Processed {line_count} lines")
                parser_func(line, log_name)
            else:
                socketio.sleep(0.5)
                
    except Exception as e:
        debug_log(f"ERROR tailing {log_name}: {e}")
        print(f"Error tailing {log_name}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if f:
            f.close()
            debug_log(f"[{log_name}] File handle closed")

def log_monitoring_thread():
    """Start greenlets for each log file using file tailing"""
    global log_monitoring_active
    
    debug_log("=" * 80)
    debug_log("LOG MONITORING THREAD STARTING")
    debug_log("=" * 80)
    print("Starting log monitoring with file tailing...")
    
    greenlets = []
    
    # QGIS logs - use file tailing
    for pool in POOL_NAMES:
        if pool in LOG_FILES_FALLBACK and os.path.exists(LOG_FILES_FALLBACK[pool]):
            debug_log(f"Starting file tail for {pool}: {LOG_FILES_FALLBACK[pool]}")
            print(f"Starting file tail for {pool}: {LOG_FILES_FALLBACK[pool]}")
            g = socketio.start_background_task(
                tail_log_file_fallback,
                pool, LOG_FILES_FALLBACK[pool], parse_qgis_log_line
            )
            greenlets.append(g)
        else:
            debug_log(f"WARNING: Log file not found for {pool}")
            print(f"Warning: Log file not found for {pool}")
    
    # PHP-FPM log
    if 'php-fpm' in LOG_FILES_FALLBACK and os.path.exists(LOG_FILES_FALLBACK['php-fpm']):
        debug_log(f"Starting file tail for php-fpm: {LOG_FILES_FALLBACK['php-fpm']}")
        print(f"Starting file tail for php-fpm: {LOG_FILES_FALLBACK['php-fpm']}")
        g = socketio.start_background_task(
            tail_log_file_fallback,
            'php-fpm', LOG_FILES_FALLBACK['php-fpm'], parse_php_log_line
        )
        greenlets.append(g)
    
    debug_log(f"Started {len(greenlets)} log monitoring greenlets")
    debug_log("LOG MONITORING ACTIVE - Watching for new log entries...")
    print(f"Started {len(greenlets)} log monitoring greenlets using file tailing")
    
    # Periodically send aggregated stats to clients
    while log_monitoring_active:
        socketio.sleep(STATS_PUSH_INTERVAL)
        
        # Calculate stats for different time windows
        stats_update = {}
        for pool in POOL_NAMES:
            stats_update[pool] = {
                '10min': calculate_response_stats(pool, 600),    # In-memory (fast, recent)
                '30min': calculate_response_stats(pool, 1800),   # In-memory (fast, recent)
                '1hour': calculate_response_stats_from_db(pool, 3600),    # Database (accurate, survives restarts)
                '24hour': calculate_response_stats_from_db(pool, 86400),  # Database (accurate, survives restarts)
                'errors': log_stats[pool]['errors'],
                'warnings': log_stats[pool]['warnings'],
                'total_requests': log_stats[pool]['requests_total']
            }
        
        # Add PHP-FPM stats (no response times, just errors/warnings)
        stats_update['php-fpm'] = {
            'errors': log_stats['php-fpm']['errors'],
            'warnings': log_stats['php-fpm']['warnings']
        }
        
        # Emit to all connected clients
        socketio.emit('stats_update', stats_update, namespace='/monitoring')
        
        # Send slowest requests separately
        slowest_update = {}
        for pool in POOL_NAMES:
            slowest_update[pool] = [
                {
                    'response_time': r[0],
                    'timestamp': datetime.fromtimestamp(r[1]).strftime('%H:%M:%S'),
                    'request_id': r[2],
                    'map': r[3]['map'],
                    'user': r[3]['user'],
                    'layers': r[3]['layers'],
                    'request_type': r[3]['request_type']
                }
                for r in slowest_requests[pool]
            ]
        
        socketio.emit('slowest_requests', slowest_update, namespace='/monitoring')


def get_process_info():
    """Get info about specific processes we care about"""
    processes = []
    
    # Look for py-qgis-server, nginx, php-fpm processes
    target_names = ['qgisserver', 'nginx', 'php-fpm']
    
    for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'status']):
        try:
            name = proc.info['name'].lower()
            if any(target in name for target in target_names):
                processes.append({
                    'pid': proc.info['pid'],
                    'name': proc.info['name'],
                    'cpu': round(proc.info['cpu_percent'], 1),
                    'memory': round(proc.info['memory_percent'], 1),
                    'status': proc.info['status']
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    
    return processes

def monitoring_thread():
    """Background thread that continuously sends metrics"""
    global monitoring_active
    
    print("Monitoring thread started")
    
    while monitoring_active:
        try:
            # Get metrics
            metrics = get_system_metrics()
            processes = get_process_info()
            
            # Save system metrics to database (every 2 seconds)
            socketio.start_background_task(
                save_system_metrics_to_db,
                metrics['cpu']['total'],
                metrics['memory']['percent'],
                metrics['memory']['used_gb'],
                metrics['memory']['available_gb'],
                metrics['memory']['total_gb'],
                metrics['disk']['read_mb'],
                metrics['disk']['write_mb'],
                metrics['network']['sent_mb'],
                metrics['network']['recv_mb']
            )
            
            # Emit to all connected clients
            socketio.emit('metrics_update', {
                'system': metrics,
                'processes': processes
            }, namespace='/monitoring')
            
            socketio.sleep(METRICS_PUSH_INTERVAL)
            
        except Exception as e:
            print(f"Error in monitoring thread: {e}")
            socketio.sleep(5)

def cleanup_thread():
    """Background thread that cleans up old data daily"""
    print("Cleanup thread started")
    
    while True:
        try:
            socketio.sleep(CLEANUP_INTERVAL)
            
            # Clean up old data
            cleanup_old_data()
            
        except Exception as e:
            print(f"Error in cleanup thread: {e}")
            socketio.sleep(3600)  # Wait 1 hour on error

@app.route('/')
def index():
    """Serve the dashboard HTML"""
    return render_template('dashboard.html')

@app.route('/api/requests/history')
def get_requests_history():
    """Get request history with optional filters"""
    try:
        project = request.args.get('project', None)
        pool = request.args.get('pool', None)
        days = int(request.args.get('days', 7))
        
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        query = '''
            SELECT * FROM requests 
            WHERE timestamp >= datetime('now', '-' || ? || ' days')
            AND LOWER(layers) != 'overview'
        '''
        params = [days]
        
        if project and project != 'all':
            query += ' AND project = ?'
            params.append(project)
        
        if pool and pool != 'all':
            query += ' AND pool = ?'
            params.append(pool)
        
        query += ' ORDER BY timestamp DESC LIMIT 1000'
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        
        requests_list = []
        for row in rows:
            requests_list.append({
                'id': row['id'],
                'timestamp': row['timestamp'],
                'pool': row['pool'],
                'project': row['project'],
                'user': row['user'],
                'layers': row['layers'],
                'request_type': row['request_type'],
                'response_time_ms': row['response_time_ms'],
                'request_id': row['request_id']
            })
        
        conn.close()
        return jsonify(requests_list)
    except Exception as e:
        print(f"Error fetching requests history: {e}")
        return jsonify([]), 500

@app.route('/api/requests/projects')
def get_projects_list():
    """Get list of all projects"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT DISTINCT project FROM requests ORDER BY project')
        projects = [row[0] for row in cursor.fetchall()]
        conn.close()
        return jsonify(projects)
    except Exception as e:
        print(f"Error fetching projects: {e}")
        return jsonify([]), 500

@app.route('/api/requests/stats')
def get_requests_stats():
    """Get statistics about requests"""
    try:
        project_filter = request.args.get('project', None)
        days = int(request.args.get('days', 7))
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Build time filter based on days parameter
        # days=0 means "today" (since midnight), not "last 24 hours"
        if days == 0:
            time_filter = "timestamp >= datetime('now', 'start of day')"
        else:
            time_filter = f"timestamp >= datetime('now', '-{days} days')"
        
        # Requests per hour
        query_hourly = f'''
            SELECT 
                strftime('%Y-%m-%d %H:00:00', timestamp) as hour,
                COUNT(*) as count
            FROM requests
            WHERE {time_filter}
            AND user != 'admin' AND LOWER(layers) != 'overview'
        '''
        params = []
        
        if project_filter and project_filter != 'all':
            query_hourly += ' AND project = ?'
            params.append(project_filter)
        
        query_hourly += ' GROUP BY hour ORDER BY hour'
        
        cursor.execute(query_hourly, params)
        hourly_data = [{'hour': row[0], 'count': row[1]} for row in cursor.fetchall()]
        
        # User activity
        query_users = f'''
            SELECT user, COUNT(*) as count, AVG(response_time_ms) as avg_time
            FROM requests
            WHERE {time_filter}
            AND user != 'admin' AND LOWER(layers) != 'overview'
        '''
        params_users = []
        
        if project_filter and project_filter != 'all':
            query_users += ' AND project = ?'
            params_users.append(project_filter)
        
        query_users += ' GROUP BY user ORDER BY count DESC'
        
        cursor.execute(query_users, params_users)
        user_activity = [{'user': row[0], 'count': row[1], 'avg_time': round(row[2], 1)} 
                        for row in cursor.fetchall()]
        
        # Average response time per project
        query_projects = f'''
            SELECT project, AVG(response_time_ms) as avg_time, COUNT(*) as count
            FROM requests
            WHERE {time_filter}
            AND user != 'admin' AND LOWER(layers) != 'overview'
            GROUP BY project
            ORDER BY avg_time DESC
        '''
        
        cursor.execute(query_projects)
        project_stats = [{'project': row[0], 'avg_time': round(row[1], 1), 'count': row[2]} 
                        for row in cursor.fetchall()]
        
        conn.close()
        
        return jsonify({
            'hourly': hourly_data,
            'users': user_activity,
            'projects': project_stats
        })
    except Exception as e:
        print(f"Error fetching stats: {e}")
        return jsonify({'hourly': [], 'users': [], 'projects': []}), 500

@app.route('/api/analytics/performance-trends')
def get_performance_trends():
    """Get performance trends over time with flexible date range and aggregation"""
    try:
        from_date = request.args.get('from_date')
        to_date = request.args.get('to_date')
        project = request.args.get('project', 'all')
        aggregation = request.args.get('aggregation', 'day')  # hour, day, week, month
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Build aggregation format
        agg_formats = {
            'hour': '%Y-%m-%d %H:00:00',
            'day': '%Y-%m-%d',
            'week': '%Y-W%W',
            'month': '%Y-%m'
        }
        agg_format = agg_formats.get(aggregation, '%Y-%m-%d')
        
        # Build query
        query = f'''
            SELECT 
                strftime('{agg_format}', timestamp) as period,
                AVG(response_time_ms) as avg_time,
                COUNT(*) as count,
                MIN(response_time_ms) as min_time,
                MAX(response_time_ms) as max_time
            FROM requests
            WHERE timestamp >= ? AND timestamp <= ? AND request_type = 'GETMAP'
            AND user != 'admin' AND LOWER(layers) != 'overview'
        '''
        params = [from_date, to_date]
        
        if project and project != 'all':
            query += ' AND project = ?'
            params.append(project)
        
        query += f' GROUP BY strftime(\'{agg_format}\', timestamp) ORDER BY period'
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        
        # Calculate P95 for each period
        results = []
        for row in rows:
            period = row[0]
            # Get P95 for this period
            p95_query = f'''
                SELECT response_time_ms 
                FROM requests
                WHERE strftime('{agg_format}', timestamp) = ? AND request_type = 'GETMAP'
                AND user != 'admin' AND LOWER(layers) != 'overview'
            '''
            p95_params = [period]
            if project and project != 'all':
                p95_query += ' AND project = ?'
                p95_params.append(project)
            p95_query += ' ORDER BY response_time_ms'
            
            cursor.execute(p95_query, p95_params)
            times = [r[0] for r in cursor.fetchall()]
            p95 = times[int(len(times) * 0.95)] if times else 0
            
            results.append({
                'period': period,
                'avg_time': round(row[1], 1),
                'count': row[2],
                'min_time': row[3],
                'max_time': row[4],
                'p95': round(p95, 1)
            })
        
        conn.close()
        return jsonify(results)
    except Exception as e:
        print(f"Error fetching performance trends: {e}")
        return jsonify([]), 500

@app.route('/api/analytics/response-distribution')
def get_response_distribution():
    """Get response time distribution (histogram buckets)"""
    try:
        from_date = request.args.get('from_date')
        to_date = request.args.get('to_date')
        project = request.args.get('project', 'all')
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        query = '''
            SELECT 
                CASE 
                    WHEN response_time_ms < 500 THEN '0-500ms'
                    WHEN response_time_ms < 1000 THEN '500ms-1s'
                    WHEN response_time_ms < 2000 THEN '1-2s'
                    WHEN response_time_ms < 5000 THEN '2-5s'
                    ELSE '>5s'
                END as bucket,
                COUNT(*) as count
            FROM requests
            WHERE timestamp >= ? AND timestamp <= ? AND request_type = 'GETMAP'
            AND user != 'admin' AND LOWER(layers) != 'overview'
        '''
        params = [from_date, to_date]
        
        if project and project != 'all':
            query += ' AND project = ?'
            params.append(project)
        
        query += ' GROUP BY bucket ORDER BY MIN(response_time_ms)'
        
        cursor.execute(query, params)
        results = [{'bucket': row[0], 'count': row[1]} for row in cursor.fetchall()]
        
        conn.close()
        return jsonify(results)
    except Exception as e:
        print(f"Error fetching response distribution: {e}")
        return jsonify([]), 500

@app.route('/api/analytics/peak-hours')
def get_peak_hours():
    """Get average response time by hour of day"""
    try:
        from_date = request.args.get('from_date')
        to_date = request.args.get('to_date')
        project = request.args.get('project', 'all')
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        query = '''
            SELECT 
                CAST(strftime('%H', timestamp) AS INTEGER) as hour,
                AVG(response_time_ms) as avg_time,
                COUNT(*) as count
            FROM requests
            WHERE timestamp >= ? AND timestamp <= ? AND request_type = 'GETMAP'
            AND user != 'admin' AND LOWER(layers) != 'overview'
        '''
        params = [from_date, to_date]
        
        if project and project != 'all':
            query += ' AND project = ?'
            params.append(project)
        
        query += ' GROUP BY hour ORDER BY hour'
        
        cursor.execute(query, params)
        results = [{'hour': row[0], 'avg_time': round(row[1], 1), 'count': row[2]} for row in cursor.fetchall()]
        
        conn.close()
        return jsonify(results)
    except Exception as e:
        print(f"Error fetching peak hours: {e}")
        return jsonify([]), 500

@app.route('/api/analytics/project-rankings')
def get_project_rankings():
    """Get projects ranked by average response time"""
    try:
        from_date = request.args.get('from_date')
        to_date = request.args.get('to_date')
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT 
                project,
                AVG(response_time_ms) as avg_time,
                COUNT(*) as count
            FROM requests
            WHERE timestamp >= ? AND timestamp <= ? AND request_type = 'GETMAP'
            AND user != 'admin' AND LOWER(layers) != 'overview'
            GROUP BY project
            ORDER BY avg_time DESC
            LIMIT 10
        ''', [from_date, to_date])
        
        results = [{'project': row[0], 'avg_time': round(row[1], 1), 'count': row[2]} for row in cursor.fetchall()]
        
        conn.close()
        return jsonify(results)
    except Exception as e:
        print(f"Error fetching project rankings: {e}")
        return jsonify([]), 500

@app.route('/api/analytics/pool-comparison')
def get_pool_comparison():
    """Get pool performance comparison"""
    try:
        from_date = request.args.get('from_date')
        to_date = request.args.get('to_date')
        project = request.args.get('project', 'all')
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        query = '''
            SELECT 
                pool,
                AVG(response_time_ms) as avg_time,
                COUNT(*) as count,
                MIN(response_time_ms) as min_time,
                MAX(response_time_ms) as max_time
            FROM requests
            WHERE timestamp >= ? AND timestamp <= ? AND request_type = 'GETMAP'
            AND user != 'admin' AND LOWER(layers) != 'overview'
        '''
        params = [from_date, to_date]
        
        if project and project != 'all':
            query += ' AND project = ?'
            params.append(project)
        
        query += ' GROUP BY pool ORDER BY pool'
        
        cursor.execute(query, params)
        results = [{'pool': row[0], 'avg_time': round(row[1], 1), 'count': row[2], 
                   'min_time': row[3], 'max_time': row[4]} for row in cursor.fetchall()]
        
        conn.close()
        return jsonify(results)
    except Exception as e:
        print(f"Error fetching pool comparison: {e}")
        return jsonify([]), 500

@app.route('/api/analytics/volume-performance')
def get_volume_performance():
    """Get correlation between request volume and performance"""
    try:
        from_date = request.args.get('from_date')
        to_date = request.args.get('to_date')
        project = request.args.get('project', 'all')
        aggregation = request.args.get('aggregation', 'hour')  # hour or day
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        agg_format = '%Y-%m-%d %H:00:00' if aggregation == 'hour' else '%Y-%m-%d'
        
        query = f'''
            SELECT 
                strftime('{agg_format}', timestamp) as period,
                COUNT(*) as volume,
                AVG(response_time_ms) as avg_time
            FROM requests
            WHERE timestamp >= ? AND timestamp <= ? AND request_type = 'GETMAP'
            AND user != 'admin' AND LOWER(layers) != 'overview'
        '''
        params = [from_date, to_date]
        
        if project and project != 'all':
            query += ' AND project = ?'
            params.append(project)
        
        query += f' GROUP BY strftime(\'{agg_format}\', timestamp) ORDER BY period'
        
        cursor.execute(query, params)
        results = [{'period': row[0], 'volume': row[1], 'avg_time': round(row[2], 1)} for row in cursor.fetchall()]
        
        conn.close()
        return jsonify(results)
    except Exception as e:
        print(f"Error fetching volume performance: {e}")
        return jsonify([]), 500

@app.route('/api/analytics/day-of-week')
def get_day_of_week_performance():
    """Get performance by day of week"""
    try:
        from_date = request.args.get('from_date')
        to_date = request.args.get('to_date')
        project = request.args.get('project', 'all')
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        query = '''
            SELECT 
                CASE CAST(strftime('%w', timestamp) AS INTEGER)
                    WHEN 0 THEN 'Sonntag'
                    WHEN 1 THEN 'Montag'
                    WHEN 2 THEN 'Dienstag'
                    WHEN 3 THEN 'Mittwoch'
                    WHEN 4 THEN 'Donnerstag'
                    WHEN 5 THEN 'Freitag'
                    WHEN 6 THEN 'Samstag'
                END as day_name,
                CAST(strftime('%w', timestamp) AS INTEGER) as day_num,
                AVG(response_time_ms) as avg_time,
                COUNT(*) as count
            FROM requests
            WHERE timestamp >= ? AND timestamp <= ? AND request_type = 'GETMAP'
            AND user != 'admin' AND LOWER(layers) != 'overview'
        '''
        params = [from_date, to_date]
        
        if project and project != 'all':
            query += ' AND project = ?'
            params.append(project)
        
        query += ' GROUP BY day_num ORDER BY day_num'
        
        cursor.execute(query, params)
        results = [{'day': row[0], 'avg_time': round(row[2], 1), 'count': row[3]} for row in cursor.fetchall()]
        
        conn.close()
        return jsonify(results)
    except Exception as e:
        print(f"Error fetching day of week performance: {e}")
        return jsonify([]), 500

@app.route('/api/system/history')
def get_system_history():
    """Get system metrics history with optional hourly aggregation for longer timeframes"""
    try:
        hours = int(request.args.get('hours', 24))
        
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # For timeframes > 24 hours, aggregate by hour to reduce data points
        if hours > 24:
            # Aggregate by hour
            cursor.execute('''
                SELECT 
                    strftime('%Y-%m-%d %H:00:00', timestamp) as timestamp,
                    AVG(cpu_percent) as cpu_percent,
                    AVG(memory_percent) as memory_percent,
                    AVG(memory_used_gb) as memory_used_gb,
                    AVG(memory_available_gb) as memory_available_gb,
                    MAX(memory_total_gb) as memory_total_gb
                FROM system_metrics
                WHERE timestamp >= datetime('now', '-' || ? || ' hours')
                GROUP BY strftime('%Y-%m-%d %H:00:00', timestamp)
                ORDER BY timestamp ASC
            ''', [hours])
        else:
            # Return all data points for short timeframes
            cursor.execute('''
                SELECT 
                    timestamp,
                    cpu_percent,
                    memory_percent,
                    memory_used_gb,
                    memory_available_gb,
                    memory_total_gb
                FROM system_metrics
                WHERE timestamp >= datetime('now', '-' || ? || ' hours')
                ORDER BY timestamp ASC
            ''', [hours])
        
        rows = cursor.fetchall()
        
        metrics_list = []
        for row in rows:
            metrics_list.append({
                'timestamp': row['timestamp'],
                'cpu_percent': round(row['cpu_percent'], 1),
                'memory_percent': round(row['memory_percent'], 1),
                'memory_used_gb': round(row['memory_used_gb'], 2),
                'memory_available_gb': round(row['memory_available_gb'], 2),
                'memory_total_gb': round(row['memory_total_gb'], 2)
            })
        
        conn.close()
        return jsonify(metrics_list)
    except Exception as e:
        print(f"Error fetching system history: {e}")
        return jsonify([]), 500

@socketio.on('connect', namespace='/monitoring')
def handle_connect():
    """Handle client connection"""
    global monitoring_active, log_monitoring_active, cleanup_active
    
    print(f"Client connected: {datetime.now()}")
    
    # Start monitoring thread if not already running
    if not monitoring_active:
        monitoring_active = True
        socketio.start_background_task(monitoring_thread)
    
    # Start log monitoring if not already running
    if not log_monitoring_active:
        log_monitoring_active = True
        socketio.start_background_task(log_monitoring_thread)
    
    # Start cleanup thread if not already running
    if not cleanup_active:
        cleanup_active = True
        socketio.start_background_task(cleanup_thread)
    
    # Send initial data immediately
    metrics = get_system_metrics()
    processes = get_process_info()
    emit('metrics_update', {
        'system': metrics,
        'processes': processes
    })
    
    # Send initial response time stats
    initial_stats = {}
    for pool in ['qgis-pool1', 'qgis-pool2', 'qgis-pool3']:
        initial_stats[pool] = {
            '10min': calculate_response_stats(pool, 600),    # In-memory (fast, recent)
            '30min': calculate_response_stats(pool, 1800),   # In-memory (fast, recent)
            '1hour': calculate_response_stats_from_db(pool, 3600),    # Database (accurate)
            '24hour': calculate_response_stats_from_db(pool, 86400),  # Database (accurate)
            'errors': log_stats[pool]['errors'],
            'warnings': log_stats[pool]['warnings'],
            'total_requests': log_stats[pool]['requests_total']
        }
    
    # Add PHP-FPM stats
    initial_stats['php-fpm'] = {
        'errors': log_stats['php-fpm']['errors'],
        'warnings': log_stats['php-fpm']['warnings']
    }
    
    emit('stats_update', initial_stats)
    
    # Send initial slowest requests
    slowest_update = {}
    for pool in ['qgis-pool1', 'qgis-pool2', 'qgis-pool3']:
        slowest_update[pool] = [
            {
                'response_time': r[0],
                'timestamp': datetime.fromtimestamp(r[1]).strftime('%H:%M:%S'),
                'request_id': r[2],
                'map': r[3]['map'],
                'user': r[3]['user'],
                'layers': r[3]['layers'],
                'request_type': r[3]['request_type']
            }
            for r in slowest_requests[pool]
        ]
    
    emit('slowest_requests', slowest_update)
    
    # Send recent issues
    emit('recent_issues', {
        name: list(issues) for name, issues in recent_issues.items()
    })

@socketio.on('disconnect', namespace='/monitoring')
def handle_disconnect():
    """Handle client disconnection"""
    print(f"Client disconnected: {datetime.now()}")

if __name__ == '__main__':
    print("=" * 60)
    print("QGIS Server Monitoring Dashboard")
    print("=" * 60)
    print(f"Starting server on http://{HOST}:{PORT}")
    print(f"Pools: {', '.join(POOL_NAMES)}")
    print(f"Database: {DB_PATH}")
    print("Press Ctrl+C to stop")
    print("=" * 60)

    socketio.run(app, host=HOST, port=PORT, debug=False)
