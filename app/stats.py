"""In-memory stats with full persistence across container restarts.

stats.json layout:
  snapshot   — totals at last daily report (used to compute daily delta)
  total      — running totals at last save (restored into _stats on startup)
  snapshot_at / total_saved_at — ISO timestamps for debugging
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

log = logging.getLogger(__name__)
_STATS_FILE = Path("/app/data/stats.json")
_lock = RLock()

KEYS = (
    "processed", "fallback", "errors",
    "smime_signed", "smime_encrypted", "smime_decrypted", "certs_harvested",
)
_stats: dict = {k: 0 for k in KEYS}
_snapshot: dict = {k: 0 for k in KEYS}
_session_start: str = datetime.now(timezone.utc).isoformat()


def _load() -> None:
    global _snapshot, _stats
    try:
        if _STATS_FILE.exists():
            data = json.loads(_STATS_FILE.read_text())
            _snapshot = {k: int(data.get("snapshot", {}).get(k, 0)) for k in KEYS}
            # Restore running total so counts survive container restarts.
            # Falls back to snapshot if no separate total is stored yet.
            raw_total = data.get("total") or data.get("snapshot") or {}
            _stats = {k: int(raw_total.get(k, 0)) for k in KEYS}
    except Exception as exc:
        log.warning("stats: could not load: %s", exc)


def _persist_total() -> None:
    """Write current running total to disk (called after every increment)."""
    try:
        _STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if _STATS_FILE.exists():
            try:
                data = json.loads(_STATS_FILE.read_text())
            except Exception:
                pass
        data["total"] = dict(_stats)
        data["total_saved_at"] = datetime.now(timezone.utc).isoformat()
        _STATS_FILE.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        log.warning("stats: could not persist total: %s", exc)


def get() -> dict:
    with _lock:
        d = dict(_stats)
    d["session_start"] = _session_start
    return d


def get_daily() -> dict:
    """Stats since last daily snapshot."""
    with _lock:
        return {k: max(0, _stats[k] - _snapshot.get(k, 0)) for k in KEYS}


def increment(key: str) -> None:
    with _lock:
        _stats[key] = _stats.get(key, 0) + 1
        _persist_total()


def take_daily_snapshot() -> dict:
    """Snapshot current totals (for daily report). Returns daily delta."""
    with _lock:
        daily = {k: max(0, _stats[k] - _snapshot.get(k, 0)) for k in KEYS}
        new_snap = dict(_stats)
    _save_snapshot(new_snap)
    return daily


def _save_snapshot(snapshot: dict) -> None:
    global _snapshot
    _snapshot = snapshot
    try:
        _STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if _STATS_FILE.exists():
            try:
                data = json.loads(_STATS_FILE.read_text())
            except Exception:
                pass
        data["snapshot"] = snapshot
        data["snapshot_at"] = datetime.now(timezone.utc).isoformat()
        data["total"] = snapshot
        data["total_saved_at"] = datetime.now(timezone.utc).isoformat()
        _STATS_FILE.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        log.warning("stats: could not persist snapshot: %s", exc)


_load()
