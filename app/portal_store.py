"""Secure Message Portal — encrypted message storage."""
import base64
import hashlib
import json
import logging
import os
import secrets
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
        # OTP-Spalten (v1.5.113) — ADD COLUMN ist idempotent via Exception
        for col, decl in (("otp_hash", "TEXT"), ("otp_expires", "TEXT"),
                          ("otp_attempts", "INTEGER DEFAULT 0"), ("otp_sent_at", "TEXT"),
                          ("access_token", "TEXT"), ("access_expires", "TEXT")):
            try:
                con.execute(f"ALTER TABLE portal_messages ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass  # Spalte existiert schon
    # DB enthält Metadaten (Adressen, Betreffs) — restriktive Rechte erzwingen
    try:
        os.chmod(_BLOB_DIR, 0o700)
        os.chmod(_DB_PATH, 0o600)
    except OSError:
        pass


def _retention_days() -> int:
    return int(settings_store.get("SECURE_PORTAL_RETENTION_DAYS") or 14)


def base_url() -> str:
    """Öffentliche Basis-URL des Gateways für Portal-Links und Logo-Referenzen.
    Extern lauscht 443 (docker-compose mappt 443→8080) — daher ohne Port."""
    url = (settings_store.get("SECURE_PORTAL_BASE_URL") or "").rstrip("/")
    if url:
        return url
    url = (settings_store.get("ADDIN_BASE_URL") or "").rstrip("/")
    if url:
        return url
    hostname = (settings_store.get("PUBLIC_HOSTNAME") or "").strip().split(":")[0]
    if hostname:
        return f"https://{hostname}"
    return "https://localhost"


# ── Branding (Logo für Portal-Seite + Benachrichtigungsmails) ────────────────

_LOGO_PATH = Path("/app/data/portal_logo.img")
_LOGO_TYPE_PATH = Path("/app/data/portal_logo.type")

LOGO_ALLOWED_TYPES = {"image/png", "image/jpeg", "image/gif"}
LOGO_MAX_BYTES = 512 * 1024


def save_logo(data: bytes, content_type: str) -> None:
    _LOGO_PATH.write_bytes(data)
    _LOGO_TYPE_PATH.write_text(content_type)
    os.chmod(_LOGO_PATH, 0o644)


def get_logo() -> tuple[bytes, str] | None:
    """(bytes, content_type) oder None, wenn kein Logo hochgeladen."""
    if not _LOGO_PATH.exists():
        return None
    ctype = "image/png"
    if _LOGO_TYPE_PATH.exists():
        ctype = _LOGO_TYPE_PATH.read_text().strip() or ctype
    return _LOGO_PATH.read_bytes(), ctype


def has_logo() -> bool:
    return _LOGO_PATH.exists()


def delete_logo() -> None:
    _LOGO_PATH.unlink(missing_ok=True)
    _LOGO_TYPE_PATH.unlink(missing_ok=True)


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
            "INSERT INTO portal_messages "
            "(token, sender_email, sender_name, recipient_email, subject, "
            " created_at, expires_at, read_at, replied_at, deleted) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
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
        first = cur.rowcount > 0
    return first


def mark_replied(token: str) -> None:
    _init()
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute(
            "UPDATE portal_messages SET replied_at=? WHERE token=? AND deleted=0",
            (now, token),
        )


# ── OTP (Zugangscode) ────────────────────────────────────────────────────────
# Bindet das Lesen an AKTUELLEN Postfachzugriff des Empfängers statt an
# einmaligen Link-Besitz (weitergeleitete Links / Browser-Historie nutzlos).
# Gleiches Modell wie Microsoft Purview OME.

OTP_VALIDITY_MIN   = 15    # Code-Gültigkeit
OTP_MAX_ATTEMPTS   = 5     # Fehlversuche pro Code
OTP_SEND_COOLDOWN  = 60    # Sekunden zwischen zwei Code-Anforderungen
ACCESS_VALIDITY_H  = 24    # Gültigkeit der Freischaltung nach erfolgreichem OTP


def generate_otp(token: str) -> str | None:
    """Neuen 6-stelligen Code erzeugen. None = Cooldown aktiv."""
    _init()
    msg = get_message(token)
    if not msg:
        return None
    now = datetime.now(timezone.utc)
    sent_at = msg.get("otp_sent_at")
    if sent_at:
        try:
            prev = datetime.fromisoformat(sent_at)
            if prev.tzinfo is None:
                prev = prev.replace(tzinfo=timezone.utc)
            if (now - prev).total_seconds() < OTP_SEND_COOLDOWN:
                return None
        except ValueError:
            pass
    code = f"{secrets.randbelow(1000000):06d}"
    expires = now + timedelta(minutes=OTP_VALIDITY_MIN)
    with _conn() as con:
        con.execute(
            "UPDATE portal_messages SET otp_hash=?, otp_expires=?, otp_attempts=0, "
            "otp_sent_at=? WHERE token=? AND deleted=0",
            (hashlib.sha256(code.encode()).hexdigest(), expires.isoformat(),
             now.isoformat(), token),
        )
    return code


def verify_otp(token: str, code: str) -> str | None:
    """Code prüfen. Bei Erfolg: Access-Token (24h gültig), sonst None.
    Der Code ist single-use — nach Erfolg wird otp_hash gelöscht."""
    _init()
    msg = get_message(token)
    if not msg or not msg.get("otp_hash"):
        return None
    now = datetime.now(timezone.utc)
    try:
        exp = datetime.fromisoformat(msg["otp_expires"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    if now > exp or int(msg.get("otp_attempts") or 0) >= OTP_MAX_ATTEMPTS:
        return None
    given = hashlib.sha256((code or "").strip().encode()).hexdigest()
    if not secrets.compare_digest(given, msg["otp_hash"]):
        with _conn() as con:
            con.execute(
                "UPDATE portal_messages SET otp_attempts=otp_attempts+1 "
                "WHERE token=? AND deleted=0", (token,),
            )
        return None
    access = secrets.token_hex(16)
    access_exp = now + timedelta(hours=ACCESS_VALIDITY_H)
    with _conn() as con:
        con.execute(
            "UPDATE portal_messages SET access_token=?, access_expires=?, otp_hash=NULL "
            "WHERE token=? AND deleted=0",
            (access, access_exp.isoformat(), token),
        )
    return access


def check_access(token: str, access: str) -> bool:
    """True, wenn das Access-Token gültig und nicht abgelaufen ist."""
    _init()
    msg = get_message(token)
    if not msg or not msg.get("access_token") or not access:
        return False
    if not secrets.compare_digest(access, msg["access_token"]):
        return False
    try:
        exp = datetime.fromisoformat(msg["access_expires"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    return datetime.now(timezone.utc) <= exp


def list_messages(include_expired: bool = False) -> list[dict]:
    """Alle nicht gelöschten Portal-Nachrichten, neueste zuerst."""
    _init()
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM portal_messages WHERE deleted=0 ORDER BY created_at DESC"
        ).fetchall()
    msgs = [dict(r) for r in rows]
    if not include_expired:
        msgs = [m for m in msgs if not is_expired(m)]
    return msgs


def delete_message(token: str) -> bool:
    """Nachricht widerrufen: Blob löschen, Eintrag als gelöscht markieren."""
    _init()
    with _conn() as con:
        cur = con.execute(
            "UPDATE portal_messages SET deleted=1 WHERE token=? AND deleted=0", (token,)
        )
        found = cur.rowcount > 0
    (_BLOB_DIR / f"{token}.enc").unlink(missing_ok=True)
    if found:
        log.info("Portal message revoked: token=%s", token)
    return found


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
