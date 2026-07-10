"""Secure Message Portal — encrypted message storage."""
import base64
import json
import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import settings_store

log = logging.getLogger(__name__)

_DB_PATH  = Path("/app/data/portal.db")
_BLOB_DIR = Path("/app/data/portal")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS portal_messages (
    token           TEXT PRIMARY KEY,
    sender_email    TEXT NOT NULL,
    sender_name     TEXT NOT NULL,
    recipient_email TEXT NOT NULL,
    subject         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    read_at         TEXT,
    replied_at      TEXT,
    deleted         INTEGER DEFAULT 0
)
"""


@contextmanager
def _conn():
    con = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _init():
    _BLOB_DIR.mkdir(parents=True, exist_ok=True)
    with _conn() as con:
        con.execute(_SCHEMA)


def _retention_days() -> int:
    return int(settings_store.get("SECURE_PORTAL_RETENTION_DAYS") or 14)


def encrypt_payload(payload: dict) -> tuple[bytes, bytes]:
    """AES-256-GCM encrypt. Returns (nonce+ciphertext, raw_key_bytes)."""
    key   = os.urandom(32)
    nonce = os.urandom(12)
    ct    = AESGCM(key).encrypt(nonce, json.dumps(payload).encode(), None)
    return nonce + ct, key


def create_message(
    sender_email: str,
    sender_name: str,
    recipient_email: str,
    subject: str,
    payload: dict,
) -> tuple[str, str]:
    """Store portal message. Returns (token, key_b64url)."""
    _init()
    token   = uuid.uuid4().hex
    now     = datetime.now(timezone.utc)
    expires = now + timedelta(days=_retention_days())
    blob, key_bytes = encrypt_payload(payload)
    (_BLOB_DIR / f"{token}.enc").write_bytes(blob)
    with _conn() as con:
        con.execute(
            "INSERT INTO portal_messages VALUES (?,?,?,?,?,?,?,?,?,?)",
            (token, sender_email, sender_name, recipient_email, subject,
             now.isoformat(), expires.isoformat(), None, None, 0),
        )
    key_b64url = base64.urlsafe_b64encode(key_bytes).rstrip(b"=").decode()
    log.info("Portal message created: token=%s recipient=%s expires=%s",
             token, recipient_email, expires.date())
    return token, key_b64url


def get_message(token: str) -> dict | None:
    _init()
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM portal_messages WHERE token=? AND deleted=0", (token,)
        ).fetchone()
    return dict(row) if row else None


def get_blob(token: str) -> bytes | None:
    p = _BLOB_DIR / f"{token}.enc"
    return p.read_bytes() if p.exists() else None


def mark_read(token: str) -> bool:
    """Mark as read. Returns True only on FIRST read."""
    _init()
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        cur = con.execute(
            "UPDATE portal_messages SET read_at=? WHERE token=? AND read_at IS NULL AND deleted=0",
            (now, token),
        )
    return cur.rowcount > 0


def mark_replied(token: str) -> None:
    _init()
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute(
            "UPDATE portal_messages SET replied_at=? WHERE token=? AND deleted=0",
            (now, token),
        )


def cleanup_expired() -> int:
    _init()
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        tokens = [r[0] for r in con.execute(
            "SELECT token FROM portal_messages WHERE expires_at < ? AND deleted=0", (now,)
        ).fetchall()]
        for t in tokens:
            (_BLOB_DIR / f"{t}.enc").unlink(missing_ok=True)
        if tokens:
            ph = ",".join("?" * len(tokens))
            con.execute(f"UPDATE portal_messages SET deleted=1 WHERE token IN ({ph})", tokens)
    if tokens:
        log.info("Portal cleanup: %d expired messages removed", len(tokens))
    return len(tokens)


def is_expired(msg: dict) -> bool:
    exp = datetime.fromisoformat(msg["expires_at"])
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) > exp
