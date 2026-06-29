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

_DATA    = Path("/app/data")
TRIGGER  = _DATA / ".update-trigger"
STATUS   = _DATA / ".update-status"

# How long (seconds) the UI polls before declaring the watcher absent
WATCHER_TIMEOUT_S = 60


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
