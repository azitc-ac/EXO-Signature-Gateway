"""Fair-Use-Lizenz — Offline-Verifizierung, Ed25519, Tenant-gebunden.

Ab FAIR_USE_LIMIT aktivierten Postfächern (Signatur oder S/MIME) zeigt das
Gateway einen Fair-Use-Hinweis. Eine vom Hub ausgestellte Lizenz lässt ihn
verschwinden — die Prüfung läuft KOMPLETT OFFLINE:

  Echtheit:      Ed25519-Signatur; der private Schlüssel liegt nur im Hub,
                 hier ist ausschließlich der öffentliche eingebettet.
  Kopierschutz:  Die Payload enthält die Microsoft-Tenant-ID; das Gateway
                 akzeptiert nur Lizenzen für den eigenen Tenant. Mehrere
                 Gateways im selben Tenant (gleicher Kunde) sind legitim.

Key-Format: EXOSIG1.<b64url(payload_json)>.<b64url(signatur)>
Payload:    {customer, issued, lic_id, mailboxes (0 = unbegrenzt),
             tenant_domain, tenant_id[, expires]}
"""
import base64
import json
import logging
from datetime import date

import settings_store

log = logging.getLogger(__name__)

# Zugehöriger privater Schlüssel: nur im EXO Signature HUB (data/license_signing.key)
PUBLIC_KEY_HEX = "9d6782ed004fb515dd3f1eec1c35f3d520da05af5a2c9f01bfec612e6c0506b9"

FAIR_USE_LIMIT = 100


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify(key_str: str, check_expiry: bool = True) -> tuple[dict | None, str]:
    """Signatur + Format prüfen. Rückgabe (payload, "") oder (None, fehler).
    Die Tenant-Bindung wird separat geprüft (tenant_error).
    check_expiry=False: Payload trotz Ablauf liefern (z.B. für die
    Ablauf-Erinnerung, die Kundendaten der abgelaufenen Lizenz braucht)."""
    key_str = (key_str or "").strip()
    parts = key_str.split(".")
    if len(parts) != 3 or parts[0] != "EXOSIG1":
        return None, "Ungültiges Format — erwartet EXOSIG1.<payload>.<signatur>"
    try:
        payload_bytes = _b64url_decode(parts[1])
        sig = _b64url_decode(parts[2])
    except Exception:
        return None, "Ungültige Kodierung"
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(PUBLIC_KEY_HEX))
        pub.verify(sig, payload_bytes)
    except Exception:
        return None, "Signatur ungültig — kein echter Lizenzschlüssel"
    try:
        payload = json.loads(payload_bytes)
    except Exception:
        return None, "Payload nicht lesbar"
    expires = payload.get("expires")
    if expires and check_expiry:
        try:
            if date.fromisoformat(expires) < date.today():
                return None, f"Lizenz am {expires} abgelaufen"
        except ValueError:
            return None, "Ungültiges Ablaufdatum in der Lizenz"
    return payload, ""


def tenant_error(payload: dict) -> str:
    """'' wenn die Lizenz zum eigenen Tenant passt, sonst Fehlertext."""
    own = (settings_store.get("TENANT_ID") or "").strip().lower()
    lic = (payload.get("tenant_id") or "").strip().lower()
    if not own:
        return "Eigene Tenant-ID nicht konfiguriert (Einrichtung unvollständig)"
    if lic != own:
        dom = payload.get("tenant_domain") or lic
        return (f"Lizenz gehört zu einem anderen Tenant ({dom}) — "
                f"Lizenzen sind Tenant-gebunden und nicht übertragbar")
    return ""


def status() -> dict:
    """Aktueller Lizenzstatus des Gateways."""
    key = (settings_store.get("LICENSE_KEY") or "").strip()
    if not key:
        return {"licensed": False, "reason": ""}
    payload, err = verify(key)
    if err:
        return {"licensed": False, "reason": err}
    terr = tenant_error(payload)
    if terr:
        return {"licensed": False, "reason": terr}
    return {
        "licensed": True,
        "customer": payload.get("customer", ""),
        "lic_id": payload.get("lic_id", ""),
        "mailboxes": int(payload.get("mailboxes") or 0),  # 0 = unbegrenzt
        "issued": payload.get("issued", ""),
        "expires": payload.get("expires", ""),
        "tenant_domain": payload.get("tenant_domain", ""),
    }


def enabled_mailbox_count() -> int:
    """Für Signatur und/oder S/MIME aktivierte Postfächer.
    Leere MAILBOX_CONFIG bedeutet 'alle Postfächer verarbeiten' → EXO-Zahl."""
    mc = settings_store.get("MAILBOX_CONFIG") or {}
    if mc:
        return sum(1 for cfg in mc.values()
                   if isinstance(cfg, dict) and (cfg.get("sig") or cfg.get("smime")))
    try:
        import exo_mailboxes
        return len(exo_mailboxes.list_mailboxes())
    except Exception:
        return 0


def fair_use_state() -> dict:
    """Zustand für die UI-Hinweise (Dashboard + Postfächer)."""
    st = status()
    count = enabled_mailbox_count()
    if st["licensed"]:
        limit = st["mailboxes"]          # 0 = unbegrenzt
        exceeded = limit > 0 and count > limit
    else:
        limit = FAIR_USE_LIMIT
        exceeded = count > FAIR_USE_LIMIT
    return {**st, "count": count, "limit": limit, "exceeded": exceeded}
