#!/usr/bin/env bash
# Richtet den Update-Watcher als systemd-Dienst ein — auf JEDEM Host mit systemd
# (Raspberry Pi, on-prem, reines `docker compose`), nicht nur auf Azure.
#
# ANLASS: Bislang installierte nur `azure-vm-setup.ps1` den
# Watcher automatisch. Der Dienst wird aber für UI-gesteuerte Updates
# (Erweitert → Gateway aktualisieren) auf ALLEN Installationswegen gebraucht.
# Dieses Skript setzt dieselbe systemd-Unit auf, unabhängig von Azure.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WATCHER="$DIR/update-watcher.sh"

if [ ! -f "$WATCHER" ]; then
  echo "FEHLER: update-watcher.sh nicht neben diesem Skript gefunden ($DIR)." >&2
  exit 1
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "Bitte mit sudo ausführen: sudo bash $0" >&2
  exit 1
fi

chmod 755 "$WATCHER"

cat > /etc/systemd/system/exo-gateway-updater.service <<UNITEOF
[Unit]
Description=EXO Gateway Update Watcher
After=docker.service
Requires=docker.service

[Service]
ExecStart=/bin/bash $WATCHER
Restart=always
RestartSec=10
User=root

[Install]
WantedBy=multi-user.target
UNITEOF

systemctl daemon-reload
systemctl enable --now exo-gateway-updater
systemctl status exo-gateway-updater --no-pager || true
echo "Update-Watcher eingerichtet (ExecStart=$WATCHER)."
