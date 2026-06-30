"""Maintenance-mode mail queue.

When MAINTENANCE_MODE is enabled, outbound mails are held here instead of
being delivered.  Each held mail is persisted to disk so it survives a
container restart.
"""
import base64
import email as _email_mod
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

import mail_processor

log = logging.getLogger(__name__)

_HELD_DIR = Path("/app/data/held_mails")
_MAX_HELD = 100


def _ensure_dir() -> None:
    _HELD_DIR.mkdir(parents=True, exist_ok=True)


def hold(sender: str, recipients: list[str], raw_bytes: bytes,
         processed_msg: "_email_mod.message.Message | None" = None) -> str:
    """Store a mail in the held queue. Returns the new mail ID."""
    _ensure_dir()

    # Enforce cap — drop oldest first.
    existing = sorted(_HELD_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    while len(existing) >= _MAX_HELD:
        try:
            existing.pop(0).unlink(missing_ok=True)
        except Exception:
            break

    mail_id = uuid.uuid4().hex

    # Extract HTML preview from the pre-S/MIME processed message.
    html_preview: str = ""
    src = processed_msg or _email_mod.message_from_bytes(raw_bytes)
    try:
        html_preview = mail_processor.extract_html(src) or ""
    except Exception as exc:
        log.debug("held_mails: html extract failed: %s", exc)

    subject = (src.get("Subject") or "").strip()
    try:
        from email.header import decode_header as _dh
        parts = _dh(subject)
        subject = "".join(
            (b.decode(enc or "utf-8") if isinstance(b, bytes) else b)
            for b, enc in parts
        )
    except Exception:
        pass

    entry = {
        "id": mail_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "from_addr": sender,
        "to_addrs": list(recipients),
        "subject": subject,
        "html_preview": html_preview,
        "raw_mime_b64": base64.b64encode(raw_bytes).decode(),
    }
    (_HELD_DIR / f"{mail_id}.json").write_text(
        json.dumps(entry, ensure_ascii=False), encoding="utf-8"
    )
    log.info("held_mails: stored %s (from=%s, to=%s, subject=%r)",
             mail_id, sender, recipients, subject)
    return mail_id


def list_all() -> list[dict]:
    """Return metadata for all held mails, newest first."""
    _ensure_dir()
    result = []
    for p in sorted(_HELD_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            result.append({
                "id":         d["id"],
                "timestamp":  d["timestamp"],
                "from_addr":  d["from_addr"],
                "to_addrs":   d["to_addrs"],
                "subject":    d["subject"],
                "has_preview": bool(d.get("html_preview")),
            })
        except Exception as exc:
            log.warning("held_mails: could not read %s: %s", p.name, exc)
    return result


def get_preview_html(mail_id: str) -> str | None:
    """Return the HTML preview for a held mail, or None if not found."""
    p = _HELD_DIR / f"{mail_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("html_preview") or ""
    except Exception:
        return None


def get_raw(mail_id: str) -> tuple[str, list[str], bytes] | None:
    """Return (from_addr, to_addrs, raw_bytes) for release, or None."""
    p = _HELD_DIR / f"{mail_id}.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d["from_addr"], d["to_addrs"], base64.b64decode(d["raw_mime_b64"])
    except Exception:
        return None


def delete(mail_id: str) -> bool:
    """Delete a held mail. Returns True if it existed."""
    p = _HELD_DIR / f"{mail_id}.json"
    if p.exists():
        p.unlink(missing_ok=True)
        log.info("held_mails: deleted %s", mail_id)
        return True
    return False


def count() -> int:
    _ensure_dir()
    return len(list(_HELD_DIR.glob("*.json")))
