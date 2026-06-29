"""
Trigger-file-based self-update helper.

The container writes data/.update-trigger to request an update.
The host-side exo-gateway-updater.service picks it up, runs
git pull (or git checkout <latest-tag> for release channel)
+ docker compose up -d --build, and writes the result to data/.update-status.
"""

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_DATA     = Path("/app/data")
TRIGGER   = _DATA / ".update-trigger"
STATUS    = _DATA / ".update-status"
HEARTBEAT = _DATA / ".update-heartbeat"

GITHUB_REPO = "azitc-ac/EXO-signature-service"

# How long (seconds) the UI polls before declaring the watcher absent
WATCHER_TIMEOUT_S = 60
# Heartbeat older than this → watcher considered dead
HEARTBEAT_MAX_AGE_S = 120


def _version_tuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.lstrip("v").split("."))
    except Exception:
        return (0,)


def check_update(channel: str, current_version: str) -> dict:
    """
    Check GitHub for a newer version.
    channel "main"    → compare to latest commit's VERSION file on main branch
    channel "release" → compare to latest GitHub Release tag
    """
    try:
        if channel == "release":
            url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
            req = urllib.request.Request(url, headers={"User-Agent": "EXO-Gateway/1"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            if "tag_name" not in data:
                return {
                    "ok": True, "channel": channel, "current": current_version,
                    "latest": None, "available": False,
                    "note": "Noch kein Release veröffentlicht",
                }
            latest = data["tag_name"].lstrip("v")
            release_url = data.get("html_url", "")
        else:
            url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/VERSION"
            req = urllib.request.Request(url, headers={"User-Agent": "EXO-Gateway/1"})
            with urllib.request.urlopen(req, timeout=10) as r:
                latest = r.read().decode().strip()
            release_url = ""

        available = _version_tuple(latest) > _version_tuple(current_version)
        return {
            "ok": True,
            "channel": channel,
            "current": current_version,
            "latest": latest,
            "available": available,
            "url": release_url,
        }
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"GitHub nicht erreichbar: {e.reason}", "channel": channel}
    except Exception as e:
        return {"ok": False, "error": str(e), "channel": channel}


def request_update(requested_by: str, current_version: str, channel: str = "main") -> dict:
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
        "channel": channel,
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
