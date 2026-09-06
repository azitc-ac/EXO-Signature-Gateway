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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mail_processor
import config

log = logging.getLogger(__name__)

_HELD_DIR = Path(config.DATA_DIR) / "held_mails"
_MAX_HELD = 100

# ── Auto-Retry-Kadenz für „delivery_failed"-Mails ─────────────────────────────
# An Exchanges Queue-Retry angelehnt (Transport service auf Mailbox-Servern,
# Standardwerte, learn.microsoft.com „Message retry, resubmit, and expiration
# intervals"):
#   Glitch:    4 Versuche im Abstand von  1 min  (QueueGlitchRetryCount/-Interval)
#   Transient: 6 Versuche im Abstand von  5 min  (TransientFailureRetryCount/-Interval)
#   danach:    alle 15 min                        (MessageRetryInterval)
#   Aufgabe:   nach 2 Tagen                       (MessageExpirationTimeout)
# Unterschied zu Exchange: bei „Aufgabe" wird NICHT zugestellt und NICHT gelöscht
# (das ist der Sinn des Fallnetzes) — es werden nur die automatischen Versuche
# eingestellt und die Mail bleibt zur manuellen Zustellung sichtbar liegen.
_GLITCH_ANZAHL = 4
_GLITCH_ABSTAND_S = 60
_TRANSIENT_ANZAHL = 6
_TRANSIENT_ABSTAND_S = 5 * 60
_STEADY_ABSTAND_S = 15 * 60
_AUFGABE_NACH_S = 2 * 24 * 60 * 60


def _abstand_fuer(versuche: int) -> int:
    """Sekunden bis zum nächsten Versuch nach `versuche` bereits erfolgten Retries."""
    if versuche < _GLITCH_ANZAHL:
        return _GLITCH_ABSTAND_S
    if versuche < _GLITCH_ANZAHL + _TRANSIENT_ANZAHL:
        return _TRANSIENT_ABSTAND_S
    return _STEADY_ABSTAND_S


def _ensure_dir() -> None:
    _HELD_DIR.mkdir(parents=True, exist_ok=True)


def hold(sender: str, recipients: list[str], raw_bytes: bytes,
         processed_msg: "_email_mod.message.Message | None" = None,
         reason: str = "maintenance") -> str:
    """Store a mail in the held queue. Returns the new mail ID.

    reason: "maintenance" (Wartungsmodus) oder "delivery_failed" (Zustellung nach
    Verarbeitung/Entschlüsselung endgültig gescheitert — Fallnetz gegen Verlust).
    """
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

    now = datetime.now(timezone.utc)
    entry = {
        "id": mail_id,
        "timestamp": now.isoformat(),
        "reason": reason,
        "from_addr": sender,
        "to_addrs": list(recipients),
        "subject": subject,
        "html_preview": html_preview,
        "raw_mime_b64": base64.b64encode(raw_bytes).decode(),
    }
    # Nur der Zustellfehler-Grund bekommt einen Auto-Retry-Zeitplan; Wartungsmodus-
    # Mails werden bewusst manuell freigegeben.
    if reason == "delivery_failed":
        entry["attempts"] = 0
        entry["first_failed"] = now.isoformat()
        entry["next_retry"] = (now + timedelta(seconds=_abstand_fuer(0))).isoformat()
        entry["retry_exhausted"] = False
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
                "reason":     d.get("reason", "maintenance"),
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


def due_for_retry(now: "datetime | None" = None) -> list[str]:
    """IDs der „delivery_failed"-Mails, die für einen erneuten Zustellversuch fällig
    sind: nicht aufgegeben und deren geplanter Zeitpunkt erreicht ist."""
    _ensure_dir()
    now = now or datetime.now(timezone.utc)
    faellig: list[str] = []
    for p in _HELD_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("reason") != "delivery_failed" or d.get("retry_exhausted"):
            continue
        nr = d.get("next_retry")
        if not nr:
            faellig.append(d["id"])
            continue
        try:
            if datetime.fromisoformat(nr) <= now:
                faellig.append(d["id"])
        except Exception:
            faellig.append(d["id"])
    return faellig


def mark_retry_failed(mail_id: str, now: "datetime | None" = None) -> dict:
    """Nach einem gescheiterten Auto-Versuch: Zähler hoch, nächsten Versuch planen.
    Nach Ablauf der Frist (2 Tage) wird aufgegeben — NICHT gelöscht, die Mail bleibt
    zur manuellen Zustellung liegen. Gibt {attempts, retry_exhausted} zurück."""
    p = _HELD_DIR / f"{mail_id}.json"
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    now = now or datetime.now(timezone.utc)
    versuche = int(d.get("attempts", 0)) + 1
    d["attempts"] = versuche
    first = d.get("first_failed") or d.get("timestamp")
    try:
        verstrichen = (now - datetime.fromisoformat(first)).total_seconds()
    except Exception:
        verstrichen = 0.0
    if verstrichen >= _AUFGABE_NACH_S:
        d["retry_exhausted"] = True
        d.pop("next_retry", None)
    else:
        d["next_retry"] = (now + timedelta(seconds=_abstand_fuer(versuche))).isoformat()
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return {"attempts": versuche, "retry_exhausted": bool(d.get("retry_exhausted"))}


def count(reason: str | None = None) -> int:
    """Anzahl in der Warteschlange. Ohne reason: alle; mit reason: nur dieser Grund."""
    _ensure_dir()
    if reason is None:
        return len(list(_HELD_DIR.glob("*.json")))
    n = 0
    for p in _HELD_DIR.glob("*.json"):
        try:
            if json.loads(p.read_text(encoding="utf-8")).get("reason", "maintenance") == reason:
                n += 1
        except Exception:
            pass
    return n
