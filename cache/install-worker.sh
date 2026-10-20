#!/bin/bash
#
# One-time setup of the Lizmap tile cache worker (run as root, idempotent).
#
# Creates the shared group and database directory, a boundaries-only config
# file (if none exists), installs and starts the systemd service. Everything
# else (instances, pools, time windows, load limits) is then configured in the
# dashboard: tab "Cache-Verwaltung" -> "Einstellungen".
#
# Usage: sudo ./cache/install-worker.sh [options]
#   --php-user USER         Lizmap's PHP user, the worker runs as it   (default: www-data)
#   --base DIR              allowed directory with Lizmap installs, repeatable
#                           (default: /var/www; only used when the config file is created)
#   --allow-remote-pools    allow QGIS pools on other hosts than localhost
#                           (only used when the config file is created)
#   --dashboard-unit NAME   systemd unit of the dashboard              (default: monitoring-dashboard)
#
# Copyright (c) 2025 meyerlor - MIT License (see LICENSE)
set -euo pipefail

PHP_USER=www-data
GROUP=lizcache
DB_DIR=/var/lib/lizmap-cache
CONF=/etc/lizmap-cache-worker.json
UNIT_NAME=lizmap-cache-worker
UNIT=/etc/systemd/system/${UNIT_NAME}.service
DASH_UNIT=monitoring-dashboard
BASES=()
ALLOW_REMOTE=false

while [ $# -gt 0 ]; do
    case "$1" in
        --php-user) PHP_USER="$2"; shift 2 ;;
        --base) BASES+=("${2%/}"); shift 2 ;;
        --allow-remote-pools) ALLOW_REMOTE=true; shift ;;
        --dashboard-unit) DASH_UNIT="$2"; shift 2 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
[ ${#BASES[@]} -eq 0 ] && BASES=(/var/www)

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok() { printf '  ✓ %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "Bitte als root ausführen: sudo $0" >&2; exit 1; }

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
WORKER="$HERE/cache_worker.py"
TEMPLATE="$HERE/lizmap-cache-worker.service"
[ -f "$WORKER" ] && [ -f "$HERE/lizmap_cache_helper.php" ] && [ -f "$TEMPLATE" ] \
    || { echo "cache_worker.py / lizmap_cache_helper.php / lizmap-cache-worker.service nicht neben diesem Skript gefunden ($HERE)" >&2; exit 1; }

say "1/5 Voraussetzungen"
PYTHON="$(command -v python3 || true)"
PHP="$(command -v php || true)"
[ -n "$PYTHON" ] || { echo "python3 nicht gefunden" >&2; exit 1; }
[ -n "$PHP" ] || { echo "php (CLI) nicht gefunden" >&2; exit 1; }
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
    || { echo "python3 >= 3.8 erforderlich" >&2; exit 1; }
id "$PHP_USER" >/dev/null 2>&1 || { echo "Benutzer $PHP_USER existiert nicht (--php-user?)" >&2; exit 1; }
ok "python3: $PYTHON ($("$PYTHON" -V 2>&1))"
ok "php:     $PHP ($("$PHP" -r 'echo PHP_VERSION;'))"
ok "Worker läuft als: $PHP_USER"

say "2/5 Gruppe $GROUP (Worker und Dashboard teilen sich die Cache-Datenbank)"
getent group "$GROUP" >/dev/null || groupadd --system "$GROUP"
usermod -aG "$GROUP" "$PHP_USER"
ok "$PHP_USER ist in $GROUP"
DASH_USER="$(systemctl show -p User --value "$DASH_UNIT" 2>/dev/null || true)"
DASH_USER="${DASH_USER:-root}"
DASH_RESTART=false
if [ "$DASH_USER" != "root" ] && id "$DASH_USER" >/dev/null 2>&1; then
    if ! id -nG "$DASH_USER" | tr ' ' '\n' | grep -qx "$GROUP"; then
        usermod -aG "$GROUP" "$DASH_USER"
        DASH_RESTART=true
    fi
    ok "Dashboard-Benutzer $DASH_USER ist in $GROUP"
else
    ok "Dashboard läuft als root – keine Gruppe nötig"
fi

say "3/5 Datenbank-Verzeichnis $DB_DIR"
install -d -o "$PHP_USER" -g "$GROUP" -m 2770 "$DB_DIR"
chown "$PHP_USER:$GROUP" "$DB_DIR"
chmod 2770 "$DB_DIR"
shopt -s nullglob
for f in "$DB_DIR"/cache.db*; do chgrp "$GROUP" "$f"; chmod g+rw "$f"; done
shopt -u nullglob
ok "$DB_DIR ($PHP_USER:$GROUP, 2770)"

say "4/5 Konfigurationsdatei $CONF (nur Pfade und Sicherheitsgrenzen)"
if [ -f "$CONF" ]; then
    ok "vorhanden, wird nicht verändert"
else
    BASES_JSON="$(printf '"%s", ' "${BASES[@]}")"
    cat > "$CONF" <<EOF
{
  "db_path": "$DB_DIR/cache.db",
  "php": "$PHP",
  "instances_base": [${BASES_JSON%, }],
  "allow_remote_pools": $ALLOW_REMOTE
}
EOF
    chown root:root "$CONF"
    chmod 644 "$CONF"
    ok "angelegt: Instanzen nur unter ${BASES[*]}, Pools $([ "$ALLOW_REMOTE" = true ] && echo 'auch entfernt' || echo 'nur localhost')"
fi

say "5/5 systemd-Dienst $UNIT_NAME"
sed -e "s#^ExecStart=.*#ExecStart=$PYTHON $WORKER --config $CONF#" \
    -e "s#^User=.*#User=$PHP_USER#" \
    -e "s#^Group=.*#Group=$(id -gn "$PHP_USER")#" \
    -e "s#^SupplementaryGroups=.*#SupplementaryGroups=$GROUP#" \
    "$TEMPLATE" > "$UNIT"
systemctl daemon-reload
systemctl enable "$UNIT_NAME" >/dev/null 2>&1
systemctl restart "$UNIT_NAME"
sleep 3
if systemctl is-active --quiet "$UNIT_NAME"; then
    ok "läuft"
else
    echo "  ✗ Dienst läuft nicht – Log:" >&2
    journalctl -u "$UNIT_NAME" -n 30 --no-pager >&2
    exit 1
fi

say "Prüfung"
sudo -u "$PHP_USER" "$PYTHON" "$WORKER" --config "$CONF" check || true

say "Fertig. Nächste Schritte:"
if [ "$DASH_RESTART" = true ]; then
    echo "  • Dashboard neu starten (neue Gruppenmitgliedschaft): systemctl restart $DASH_UNIT"
fi
echo "  • Dashboard öffnen → Tab „Cache-Verwaltung“ → „Einstellungen“ → „Instanzen suchen“,"
echo "    Instanzen anhaken, optional einen Seeding-Pool eintragen, speichern."
echo "  • Log des Workers: journalctl -u $UNIT_NAME -f"
