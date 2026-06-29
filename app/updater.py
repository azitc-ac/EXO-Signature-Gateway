"""
Trigger-file-based self-update helper.

The container writes data/.update-trigger to request an update.
The host-side exo-gateway-updater.service picks it up, runs
git pull + docker compose up -d --build, and writes the result
to data/.update-status.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

_DATA     = Path("/app/data")
TRIGGER   = _DATA / ".update-trigger"
STATUS    = _DATA / ".update-status"
HEARTBEAT = _DATA / ".update-heartbeat"

# How long (seconds) the UI polls before declaring the watcher absent
WATCHER_TIMEOUT_S = 60
# Heartbeat older than this → watcher considered dead
HEARTBEAT_MAX_AGE_S = 120


def request_update(requested_by: str, current_version: str) -> dict:
    """
    Write the trigger file to start an update.
    Returns {"ok": True} or {"ok": False, "error": "..."}.
    """
    status = get_status()
    if status.get("state") == "running":
        return {"ok": False, "error": "Update läuft bereits"}
    payload = {
        "requested_by": requested_by,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "current_version": current_version,
    }
    try:
        TRIGGER.write_text(json.dumps(payload))
        TRIGGER.chmod(0o644)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


def get_status() -> dict:
    """Read current update status. Returns {"state": "idle"} if no status file."""
    try:
        return json.loads(STATUS.read_text())
    except Exception:
        return {"state": "idle"}


def clear_status() -> None:
    try:
        STATUS.unlink(missing_ok=True)
    except Exception:
        pass


def watcher_ok() -> bool:
    """True if the host-side watcher wrote a heartbeat within the last 2 minutes."""
    try:
        age = datetime.now(timezone.utc).timestamp() - HEARTBEAT.stat().st_mtime
        return age < HEARTBEAT_MAX_AGE_S
    except Exception:
        return False
