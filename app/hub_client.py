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


# ── Cert deployment track (separate registration + key) ───────────────────────

def _cert_base() -> str:
    return (settings_store.get("HUB_CERT_BASE_URL") or "").strip().rstrip("/")


def _cert_key() -> str:
    return (settings_store.get("HUB_CERT_API_KEY") or "").strip()


def cert_is_configured() -> bool:
    return bool(_cert_base())


def cert_is_registered() -> bool:
    return bool(_cert_base() and _cert_key())


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
    """Register this gateway with the cert-deployment hub (want=cert, separate key)."""
    base = _cert_base()
    email = (settings_store.get("HUB_CERT_EMAIL") or "").strip().lower()
    name = (settings_store.get("HUB_CERT_NAME") or "").strip()
    if not base:
        return {"ok": False, "error": "Cert-Hub-Adresse (HUB_CERT_BASE_URL) nicht gesetzt."}
    if "@" not in email:
        return {"ok": False, "error": "Gültige Cert-Kunden-E-Mail erforderlich."}
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


async def cert_order(target_email: str, csr_pem: str, extra: dict | None = None,
                     provider: str = "sectigo") -> dict:
    """Submit an S/MIME cert order to the reseller hub (operator holds the CA creds)."""
    base = _cert_base()
    if not base:
        return {"ok": False, "error": "Cert-Hub-Adresse (HUB_CERT_BASE_URL) nicht gesetzt."}
    if not _cert_key():
        return {"ok": False, "error": "Cert-Track nicht registriert/freigegeben — kein API-Key."}
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
