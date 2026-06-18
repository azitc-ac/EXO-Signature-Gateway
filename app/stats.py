"""In-memory stats with daily snapshot persistence."""
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


def _load_snapshot() -> None:
    global _snapshot
    try:
        if _STATS_FILE.exists():
            data = json.loads(_STATS_FILE.read_text())
            _snapshot = {k: int(data.get("snapshot", {}).get(k, 0)) for k in KEYS}
    except Exception as exc:
        log.warning("stats: could not load snapshot: %s", exc)


def get() -> dict:
    with _lock:
        d = dict(_stats)
    d["session_start"] = _session_start
    return d


def get_daily() -> dict:
    """Stats since last daily snapshot (reset at report time)."""
    with _lock:
        return {k: max(0, _stats[k] - _snapshot.get(k, 0)) for k in KEYS}


def increment(key: str) -> None:
    with _lock:
        _stats[key] = _stats.get(key, 0) + 1


def take_daily_snapshot() -> dict:
    """Snapshot current totals (for daily report). Returns daily delta."""
    with _lock:
        daily = {k: max(0, _stats[k] - _snapshot.get(k, 0)) for k in KEYS}
        new_snap = dict(_stats)
    _save(new_snap)
    return daily


def _save(snapshot: dict) -> None:
    global _snapshot
    _snapshot = snapshot
    try:
        _STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATS_FILE.write_text(json.dumps(
            {"snapshot": snapshot, "snapshot_at": datetime.now(timezone.utc).isoformat()},
            indent=2,
        ))
    except Exception as exc:
        log.warning("stats: could not persist snapshot: %s", exc)


_load_snapshot()
