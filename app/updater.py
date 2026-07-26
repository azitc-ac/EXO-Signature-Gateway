"""Selbst-Update des Gateways — Adapter auf den gemeinsamen Kern.

Die Logik steckt in `update_core.py`, das mit dem Hub inhaltsgleich ist
(`tools/driftcheck.py` vergleicht die SHA-256). Hier stehen nur die
anwendungsspezifischen Werte und die bisherige Modul-API, damit die
Aufrufstellen unverändert bleiben.

Host-Seite: `exo-gateway-updater.service` liest `data/.update-trigger`, führt
`git pull` (bzw. `git checkout <tag>` im Release-Kanal) plus
`docker compose up -d --build` aus und schreibt `data/.update-status`.
"""

from update_core import (              # noqa: F401  (Teil der Modul-API)
    HEARTBEAT_MAX_AGE_S,
    WATCHER_TIMEOUT_S,
    Updater,
)

GITHUB_REPO = "azitc-ac/EXO-signature-service"

_updater = Updater(repo=GITHUB_REPO, user_agent="EXO-Gateway/1")

# Pfade — nur zur Diagnose von außen; die Schreibvorgänge laufen über die
# Methoden unten.
TRIGGER = _updater.trigger
STATUS = _updater.status_file
HEARTBEAT = _updater.heartbeat
RESTART_TRIGGER = _updater.restart_trigger

# Bisherige Funktionsnamen unverändert weiterreichen.
check_update = _updater.check_update
list_release_tags = _updater.list_release_tags
get_status = _updater.get_status
clear_status = _updater.clear_status
request_update = _updater.request_update
request_container_restart = _updater.request_container_restart
watcher_ok = _updater.watcher_ok
