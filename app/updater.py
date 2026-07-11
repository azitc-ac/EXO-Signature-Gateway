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

_DATA           = Path("/app/data")
TRIGGER         = _DATA / ".update-trigger"
STATUS          = _DATA / ".update-status"
HEARTBEAT       = _DATA / ".update-heartbeat"
RESTART_TRIGGER = _DATA / ".restart-trigger"

GITHUB_REPO = "azitc-ac/EXO-signature-service"

# How long (seconds) the UI polls before declaring the watcher absent
WATCHER_TIMEOUT_S = 60
# Heartbeat older than this → watcher considered dead
# 300s covers a full docker compose build cycle
HEARTBEAT_MAX_AGE_S = 300


def _version_tuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.lstrip("v").split("."))
    except Exception:
        return (0,)


def _fetch_changelog_entries(from_version: str, to_version: str) -> list:
    import re
    try:
        url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/CHANGELOG.md"
        req = urllib.request.Request(url, headers={"User-Agent": "EXO-Gateway/1"})
        with urllib.request.urlopen(req, timeout=10) as r:
            text = r.read().decode()
        def _vnum(v: str) -> int:
            parts = (list(int(x) for x in v.lstrip("v").split(".")) + [0, 0, 0])[:3]
            return parts[0] * 1_000_000 + parts[1] * 1_000 + parts[2]

        # Alle Einträge in Reihenfolge (neueste zuerst)
        all_entries, cur_header, cur_body = [], "", []
        for line in text.splitlines():
            if line.startswith("## v"):
                if cur_header:
                    all_entries.append({"header": cur_header, "body": "\n".join(cur_body).strip()})
                cur_header, cur_body = line, []
            elif cur_header:
                cur_body.append(line)
        if cur_header:
            all_entries.append({"header": cur_header, "body": "\n".join(cur_body).strip()})

        # DRIFT-IMMUN (s. app.py api_update_whats_new): oberste K Einträge,
        # K = Versions-Distanz. Changelog-Nummern driften von der VERSION-Datei.
        steps = max(1, _vnum(to_version) - _vnum(from_version))
        return all_entries[:min(steps, len(all_entries), 25)]
    except Exception:
        return []


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
        changelog_entries = []
        if available:
            changelog_entries = _fetch_changelog_entries(current_version, latest)
        return {
            "ok": True,
            "channel": channel,
            "current": current_version,
            "latest": latest,
            "available": available,
            "url": release_url,
            "changelog_entries": changelog_entries,
        }
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"GitHub nicht erreichbar: {e.reason}", "channel": channel}
    except Exception as e:
        return {"ok": False, "error": str(e), "channel": channel}


def request_update(requested_by: str, current_version: str, channel: str = "main",
                    target_version: str | None = None) -> dict:
    """
    Write the trigger file to start an update.
    target_version: specific release tag to check out (release channel only).
    None means "latest" (existing behaviour) — used for both upgrades and,
    when set to an older version than current, rollbacks.
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
    if target_version:
        payload["target_version"] = target_version
    try:
        TRIGGER.write_text(json.dumps(payload))
        TRIGGER.chmod(0o644)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


def list_release_tags(limit: int = 30) -> list:
    """
    Fetch published GitHub Releases (tag + date + URL), newest first.
    Used by the UI to offer a specific version to roll back / forward to.
    """
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page={limit}"
        req = urllib.request.Request(url, headers={"User-Agent": "EXO-Gateway/1"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        return [
            {
                "version": rel["tag_name"].lstrip("v"),
                "published_at": rel.get("published_at", ""),
                "url": rel.get("html_url", ""),
                "name": rel.get("name") or rel["tag_name"],
            }
            for rel in data
            if rel.get("tag_name") and not rel.get("draft")
        ]
    except Exception:
        return []


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


def request_container_restart(requested_by: str) -> dict:
    """Write restart trigger → watcher runs docker compose restart."""
    if get_status().get("state") == "running":
        return {"ok": False, "error": "Update läuft bereits — bitte warten"}
    try:
        RESTART_TRIGGER.write_text(json.dumps({
            "requested_by": requested_by,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }))
        RESTART_TRIGGER.chmod(0o644)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


def watcher_ok() -> bool:
    """True if the host-side watcher wrote a heartbeat within the last 2 minutes."""
    try:
        age = datetime.now(timezone.utc).timestamp() - HEARTBEAT.stat().st_mtime
        return age < HEARTBEAT_MAX_AGE_S
    except Exception:
        return False
