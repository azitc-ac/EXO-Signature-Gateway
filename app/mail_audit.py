"""
Per-message audit log stored in SQLite.

Each processed SMTP transaction writes one row. The log is queried by the
dashboard to show details behind the aggregate stats numbers.

Storage: /app/data/mail_audit.db
Retention: configurable via LOG_RETENTION_DAYS (default 90), pruned on startup.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

DB_PATH = Path("/app/data/mail_audit.db")
_lock = threading.Lock()
_initialised = False


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    global _initialised
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock, _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mail_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT    NOT NULL,
                sender        TEXT,
                recipients    TEXT,
                subject       TEXT,
                message_id    TEXT,
                action        TEXT,
                size_bytes    INTEGER,
                processing_ms INTEGER,
                error         TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ts     ON mail_log(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_action ON mail_log(action)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sender ON mail_log(sender)")
    _initialised = True
    log.info("mail_audit: DB ready at %s", DB_PATH)


def log_event(
    *,
    sender: str,
    recipients: list[str],
    subject: str,
    message_id: str,
    action: str,
    size_bytes: int = 0,
    processing_ms: int = 0,
    error: str | None = None,
) -> None:
    if not _initialised:
        return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with _lock, _conn() as conn:
            conn.execute(
                "INSERT INTO mail_log "
                "(ts, sender, recipients, subject, message_id, action, size_bytes, processing_ms, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts,
                    sender,
                    json.dumps(recipients, ensure_ascii=False),
                    subject,
                    message_id,
                    action,
                    size_bytes,
                    processing_ms,
                    error,
                ),
            )
    except Exception as exc:
        log.warning("mail_audit: write failed: %s", exc)


def query_events(
    *,
    date: str | None = None,
    action: str | None = None,
    sender: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    """Return events as a list of dicts, newest first."""
    if not _initialised:
        return []
    conditions: list[str] = []
    params: list = []
    if date:
        conditions.append("ts >= ? AND ts <= ?")
        params += [f"{date}T00:00:00Z", f"{date}T23:59:59Z"]
    if action:
        conditions.append("action = ?")
        params.append(action)
    if sender:
        conditions.append("sender = ?")
        params.append(sender)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params += [limit, offset]
    try:
        with _conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM mail_log {where} ORDER BY ts DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.warning("mail_audit: query failed: %s", exc)
        return []


def count_events(
    *,
    date: str | None = None,
    action: str | None = None,
    sender: str | None = None,
) -> int:
    if not _initialised:
        return 0
    conditions: list[str] = []
    params: list = []
    if date:
        conditions.append("ts >= ? AND ts <= ?")
        params += [f"{date}T00:00:00Z", f"{date}T23:59:59Z"]
    if action:
        conditions.append("action = ?")
        params.append(action)
    if sender:
        conditions.append("sender = ?")
        params.append(sender)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    try:
        with _conn() as conn:
            return conn.execute(
                f"SELECT COUNT(*) FROM mail_log {where}", params
            ).fetchone()[0]
    except Exception as exc:
        log.warning("mail_audit: count failed: %s", exc)
        return 0


def avg_processing_ms(since_iso: str) -> float | None:
    """Return average processing_ms for rows with ts >= since_iso, or None if no rows."""
    if not _initialised:
        return None
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT AVG(processing_ms) FROM mail_log WHERE ts >= ? AND processing_ms > 0",
                (since_iso,),
            ).fetchone()
        val = row[0] if row else None
        return round(val, 1) if val is not None else None
    except Exception as exc:
        log.warning("mail_audit: avg_processing_ms failed: %s", exc)
        return None


def peak_hour(date: str) -> tuple[str, int] | None:
    """Return (hour_str, count) for the busiest hour on *date* (YYYY-MM-DD), or None."""
    if not _initialised:
        return None
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT substr(ts,12,2) AS h, COUNT(*) AS c "
                "FROM mail_log WHERE ts >= ? AND ts <= ? "
                "GROUP BY h ORDER BY c DESC LIMIT 1",
                (f"{date}T00:00:00Z", f"{date}T23:59:59Z"),
            ).fetchone()
        if row and row[0]:
            return (row[0], row[1])
        return None
    except Exception as exc:
        log.warning("mail_audit: peak_hour failed: %s", exc)
        return None


def prune_old_events(retention_days: int = 90) -> int:
    """Delete events older than *retention_days*. Returns the number of deleted rows."""
    if not _initialised:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    try:
        with _lock, _conn() as conn:
            deleted = conn.execute(
                "DELETE FROM mail_log WHERE ts < ?", (cutoff,)
            ).rowcount
        if deleted:
            log.info("mail_audit: pruned %d events older than %d days", deleted, retention_days)
        return deleted
    except Exception as exc:
        log.warning("mail_audit: prune failed: %s", exc)
        return 0
