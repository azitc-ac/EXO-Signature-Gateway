"""
Client for the sig-provider hub (operator's service-provider backend).

Registers this gateway with the hub, stores the issued API key, and routes
support/diagnostic bundles to the hub's upload endpoint. Replaces the former
direct-to-Azure-Blob support upload.

Hub endpoints used:
  POST {base}/api/register          {email,name,want} → status pending
  GET  {base}/api/support/me        (X-API-Key)       → quota/enabled status
  POST {base}/api/support/upload    (X-API-Key, multipart file=…)
"""
import asyncio
import logging

import httpx

import settings_store
import support_upload  # reuse build_bundle (bundle contents unchanged)

log = logging.getLogger(__name__)


def _base() -> str:
    return (settings_store.get("HUB_BASE_URL") or "").strip().rstrip("/")


def _key() -> str:
    return (settings_store.get("HUB_API_KEY") or "").strip()


def is_configured() -> bool:
    return bool(_base())


def is_registered() -> bool:
    return bool(_base() and _key())


# ── Cert capability — SAME account/key as support (unified registration) ──────
# Cert is a paid capability on the one hub account; it reuses HUB_BASE_URL /
# HUB_API_KEY. Enabling + billing + terms are handled per-account at the hub.

def _cert_base() -> str:
    return _base()


def _cert_key() -> str:
    return _key()


def cert_is_configured() -> bool:
    return is_configured()


def cert_is_registered() -> bool:
    return is_registered()


async def register() -> dict:
    """Register this gateway with the hub. Email is the username."""
    base = _base()
    email = (settings_store.get("HUB_CUSTOMER_EMAIL") or "").strip().lower()
    name = (settings_store.get("HUB_CUSTOMER_NAME") or "").strip()
    if not base:
        return {"ok": False, "error": "Hub-Adresse (HUB_BASE_URL) nicht gesetzt."}
    if "@" not in email:
        return {"ok": False, "error": "Gültige Kunden-E-Mail erforderlich."}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{base}/api/register",
                             json={"email": email, "name": name, "want": "support"})
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("ok"):
            return {"ok": True, "status": data.get("status"), "message": data.get("message", "")}
        return {"ok": False, "error": data.get("detail") or f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": f"Verbindungsfehler: {exc}"}


async def status() -> dict:
    """Query this gateway's account status/quota at the hub (needs API key)."""
    base = _base()
    if not (base and _key()):
        return {"ok": False, "error": "Hub-Adresse oder API-Key fehlt."}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{base}/api/support/me", headers={"X-API-Key": _key()})
        if r.status_code == 200:
            return {"ok": True, **r.json()}
        if r.status_code in (401, 403):
            return {"ok": False, "error": f"Nicht freigegeben/ungültiger Key (HTTP {r.status_code})."}
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": f"Verbindungsfehler: {exc}"}


async def upload_bundle(runtime_log_lines: list[str]) -> dict:
    """Build the diagnostic bundle and upload it to the hub."""
    base = _base()
    if not base:
        return {"ok": False, "error": "Hub-Adresse (HUB_BASE_URL) nicht gesetzt."}
    if not _key():
        return {"ok": False, "error": "Noch nicht registriert/freigegeben — kein API-Key."}
    try:
        zip_bytes, name = await asyncio.get_event_loop().run_in_executor(
            None, support_upload.build_bundle, runtime_log_lines
        )
    except Exception as exc:
        log.error("hub upload: bundle build failed: %s", exc)
        return {"ok": False, "error": f"Bundle-Erstellung fehlgeschlagen: {exc}"}

    size_kb = round(len(zip_bytes) / 1024, 1)
    try:
        async with httpx.AsyncClient(timeout=180) as c:
            r = await c.post(
                f"{base}/api/support/upload",
                headers={"X-API-Key": _key()},
                files={"file": (name, zip_bytes, "application/zip")},
            )
        if r.status_code == 200:
            d = r.json()
            log.info("Support bundle uploaded to hub: %s (%s KB)", d.get("stored_as", name), size_kb)
            return {"ok": True, "ticket_id": d.get("stored_as", name), "size_kb": size_kb,
                    "analysis": d.get("analysis", {})}
        if r.status_code == 413:
            return {"ok": False, "error": f"Vom Hub abgelehnt (Kontingent/Größe): {r.text[:200]}"}
        if r.status_code in (401, 403):
            return {"ok": False, "error": f"Nicht freigegeben/ungültiger Key (HTTP {r.status_code})."}
        return {"ok": False, "error": f"Hub HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        log.error("hub upload error: %s", exc)
        return {"ok": False, "error": f"Netzwerkfehler: {exc}"}


async def cert_register() -> dict:
    """Flag the cert capability on the ONE hub account (want=cert, same email)."""
    base = _base()
    email = (settings_store.get("HUB_CUSTOMER_EMAIL") or "").strip().lower()
    name = (settings_store.get("HUB_CUSTOMER_NAME") or "").strip()
    if not base:
        return {"ok": False, "error": "Hub-Adresse (HUB_BASE_URL) nicht gesetzt."}
    if "@" not in email:
        return {"ok": False, "error": "Gültige Kunden-E-Mail erforderlich (Anbindung)."}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{base}/api/register",
                             json={"email": email, "name": name, "want": "cert"})
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("ok"):
            return {"ok": True, "status": data.get("status"), "message": data.get("message", "")}
        return {"ok": False, "error": data.get("detail") or f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": f"Verbindungsfehler: {exc}"}


async def cert_accept_terms(version: str = "1") -> dict:
    """Accept the paid terms for the cert capability (uses the one account key)."""
    base = _base()
    if not (base and _key()):
        return {"ok": False, "error": "Nicht registriert (Anbindung fehlt)."}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{base}/api/cert/accept-terms",
                             headers={"X-API-Key": _key()}, json={"version": version})
        if r.status_code == 200:
            return {"ok": True, **r.json()}
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": f"Verbindungsfehler: {exc}"}


async def cert_eligibility() -> dict:
    """Ask the hub whether this account may currently order certs (and why not)."""
    base = _base()
    if not (base and _key()):
        return {"ok": False, "error": "Nicht registriert (Anbindung fehlt)."}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{base}/api/cert/eligibility", headers={"X-API-Key": _key()})
        if r.status_code == 200:
            return r.json()
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": f"Verbindungsfehler: {exc}"}


async def cert_order(target_email: str, csr_pem: str, extra: dict | None = None,
                     provider: str = "sectigo") -> dict:
    """Submit an S/MIME cert order via the ONE hub account (operator holds CA creds)."""
    base = _base()
    if not base:
        return {"ok": False, "error": "Hub-Adresse (HUB_BASE_URL) nicht gesetzt."}
    if not _key():
        return {"ok": False, "error": "Nicht registriert/freigegeben — kein API-Key."}
    body = {"provider": provider or "sectigo", "email": target_email, "csr": csr_pem, "extra": extra or {}}
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{base}/api/cert/order",
                             headers={"X-API-Key": _cert_key()}, json=body)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("ok"):
            log.info("Cert order submitted to reseller hub for %s → %s", target_email, data.get("status"))
            return {"ok": True, "status": data.get("status"), "ref": data.get("ref"),
                    "message": data.get("message", ""), "cert_pem": data.get("cert_pem")}
        if r.status_code in (401, 403):
            return {"ok": False, "error": f"Nicht freigegeben/ungültiger Key (HTTP {r.status_code})."}
        return {"ok": False, "error": data.get("message") or f"Hub HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        log.error("hub cert order error: %s", exc)
        return {"ok": False, "error": f"Netzwerkfehler: {exc}"}
