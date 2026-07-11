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
import secrets

import httpx

import settings_store
import support_upload  # reuse build_bundle (bundle contents unchanged)

log = logging.getLogger(__name__)


def _base() -> str:
    return (settings_store.get("HUB_BASE_URL") or "").strip().rstrip("/")


def _key() -> str:
    return (settings_store.get("HUB_API_KEY") or "").strip()


def _gateway_headers(key: str | None = None) -> dict:
    """Auth + identify this gateway to the hub (X-Gateway-*), so the hub can show
    which gateway(s) sit behind a customer. Multiple gateways per customer are OK."""
    gid = (settings_store.get("GATEWAY_ID") or "").strip()
    if not gid:
        import uuid
        gid = uuid.uuid4().hex
        settings_store.update({"GATEWAY_ID": gid})
    import socket
    host = (settings_store.get("PUBLIC_HOSTNAME") or "").strip() or socket.gethostname()
    ver = ""
    try:
        import config as _cfg
        ver = str(getattr(_cfg, "VERSION", "") or "")
    except Exception:
        ver = ""
    if not ver:
        try:
            from pathlib import Path
            ver = Path("/app/VERSION").read_text().strip()
        except Exception:
            ver = ""
    return {"X-API-Key": key or _key(), "X-Gateway-Id": gid,
            "X-Gateway-Host": host, "X-Gateway-Version": ver}


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
    """Start the self-service connection: send a claim token so the gateway can
    later pull the issued API key (after the operator confirms via email)."""
    base = _base()
    email = (settings_store.get("HUB_CUSTOMER_EMAIL") or "").strip().lower()
    name = (settings_store.get("HUB_CUSTOMER_NAME") or "").strip()
    if not base:
        return {"ok": False, "error": "Hub-Adresse (HUB_BASE_URL) nicht gesetzt."}
    if "@" not in email:
        return {"ok": False, "error": "Gültige Kunden-E-Mail erforderlich."}
    claim = secrets.token_urlsafe(32)
    settings_store.update({"HUB_CLAIM_TOKEN": claim})
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{base}/api/register",
                             json={"email": email, "name": name, "want": "support", "claim_token": claim})
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("ok"):
            return {"ok": True, "status": data.get("status"),
                    "email_sent": data.get("email_sent"), "message": data.get("message", "")}
        return {"ok": False, "error": data.get("detail") or f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": f"Verbindungsfehler: {exc}"}


async def poll_claim() -> dict:
    """After the operator confirmed via email, pull the API key once and store it.
    Returns {ok, connected, status}."""
    base = _base()
    claim = (settings_store.get("HUB_CLAIM_TOKEN") or "").strip()
    if not base:
        return {"ok": False, "error": "Hub-Adresse (HUB_BASE_URL) nicht gesetzt."}
    if not claim:
        return {"ok": False, "error": "Kein Claim-Token — erst „Verbinden“ auslösen."}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{base}/api/register/claim", headers={"X-Claim-Token": claim})
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("ok") and data.get("api_key"):
            settings_store.update({"HUB_API_KEY": data["api_key"], "HUB_CLAIM_TOKEN": ""})
            log.info("Hub API key received via claim-token relay — connected.")
            return {"ok": True, "connected": True}
        return {"ok": True, "connected": False, "status": data.get("status", "pending_confirmation")}
    except Exception as exc:
        return {"ok": False, "error": f"Verbindungsfehler: {exc}"}


async def cert_request_invoice() -> dict:
    """Apply to switch from prepaid to invoice billing (admin approval required)."""
    base = _base()
    if not (base and _key()):
        return {"ok": False, "error": "Nicht registriert (Anbindung fehlt)."}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{base}/api/cert/request-invoice", headers=_gateway_headers())
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("ok"):
            return data
        return {"ok": False, "error": data.get("detail") or f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": f"Verbindungsfehler: {exc}"}


async def cert_submit_billing(company: str, address: str, vat: str, contact: str) -> dict:
    """Submit/update this account's billing info (self-service)."""
    base = _base()
    if not (base and _key()):
        return {"ok": False, "error": "Nicht registriert (Anbindung fehlt)."}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{base}/api/cert/billing", headers=_gateway_headers(),
                             json={"billing_company": company, "billing_address": address,
                                   "billing_vat": vat, "billing_contact": contact})
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("ok"):
            return {"ok": True}
        return {"ok": False, "error": data.get("detail") or f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": f"Verbindungsfehler: {exc}"}


async def cert_domain_request(domain: str) -> dict:
    """Start DNS-TXT domain-control verification for a domain."""
    base = _base()
    if not (base and _key()):
        return {"ok": False, "error": "Nicht registriert (Anbindung fehlt)."}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{base}/api/cert/domain/request",
                             headers=_gateway_headers(), json={"domain": domain})
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("ok"):
            return data
        return {"ok": False, "error": data.get("detail") or f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": f"Verbindungsfehler: {exc}"}


async def cert_domain_verify(domain: str) -> dict:
    """Trigger the DNS TXT check for a pending domain verification."""
    base = _base()
    if not (base and _key()):
        return {"ok": False, "error": "Nicht registriert (Anbindung fehlt)."}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{base}/api/cert/domain/verify",
                             headers=_gateway_headers(), json={"domain": domain})
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        return {"ok": r.status_code == 200 and bool(data.get("ok")),
                "message": data.get("message") or data.get("detail") or data.get("error", "")}
    except Exception as exc:
        return {"ok": False, "error": f"Verbindungsfehler: {exc}"}


async def cert_opt_out() -> dict:
    """Ask the hub to disable the (paid) cert capability for this account."""
    base = _base()
    if not (base and _key()):
        return {"ok": False, "error": "Nicht registriert (Anbindung fehlt)."}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{base}/api/cert/opt-out", headers=_gateway_headers())
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        return {"ok": r.status_code == 200 and data.get("ok"),
                "cert_issuing_enabled": data.get("cert_issuing_enabled")}
    except Exception as exc:
        return {"ok": False, "error": f"Verbindungsfehler: {exc}"}


async def disconnect(close_remote: bool = False) -> dict:
    """Remove the local hub binding AND tell the hub to deactivate THIS gateway
    (the customer account stays). Optionally also close the whole account."""
    base = _base()
    if base and _key():
        # Deactivate this gateway at the hub (best-effort; needs the key → do it first).
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                await c.post(f"{base}/api/gateway/deactivate", headers=_gateway_headers())
        except Exception as exc:
            log.warning("hub gateway-deactivate failed: %s", exc)
        if close_remote:
            try:
                async with httpx.AsyncClient(timeout=20) as c:
                    await c.post(f"{base}/api/account/disconnect", headers=_gateway_headers())
            except Exception as exc:
                log.warning("hub account-disconnect failed: %s", exc)
    settings_store.update({"HUB_API_KEY": "", "HUB_CLAIM_TOKEN": ""})
    log.info("Hub-Anbindung lokal entfernt (Gateway beim Hub deaktiviert).")
    return {"ok": True}


async def status() -> dict:
    """Query this gateway's account status/quota at the hub (needs API key)."""
    base = _base()
    if not (base and _key()):
        return {"ok": False, "error": "Hub-Adresse oder API-Key fehlt."}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{base}/api/support/me", headers=_gateway_headers())
        if r.status_code == 200:
            return {"ok": True, **r.json()}
        if r.status_code in (401, 403):
            return {"ok": False, "error": f"Nicht freigegeben/ungültiger Key (HTTP {r.status_code})."}
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": f"Verbindungsfehler: {exc}"}


async def upload_bundle(runtime_log_lines: list[str], note: str = "") -> dict:
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
                headers=_gateway_headers(),
                files={"file": (name, zip_bytes, "application/zip")},
                data={"note": note or ""},
            )
        if r.status_code == 200:
            d = r.json()
            log.info("Support bundle uploaded to hub: %s (%s KB)", d.get("stored_as", name), size_kb)
            return {"ok": True, "ticket_id": d.get("stored_as", name), "size_kb": size_kb,
                    "analysis": d.get("analysis", {})}
        if r.status_code == 413:
            return {"ok": False, "error": f"Vom Hub abgelehnt (Kontingent/Größe): {r.text[:200]}"}
        if r.status_code == 429:
            return {"ok": False, "error": "Höchstens 1 Upload pro Minute — kurz warten und erneut versuchen."}
        if r.status_code in (401, 403):
            return {"ok": False, "error": f"Nicht freigegeben/ungültiger Key (HTTP {r.status_code})."}
        return {"ok": False, "error": f"Hub HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        log.error("hub upload error: %s", exc)
        return {"ok": False, "error": f"Netzwerkfehler: {exc}"}


async def cert_terms() -> dict:
    """Fetch the current terms text from the hub (public, no key needed) so the
    gateway can show it before the customer accepts."""
    base = _base()
    if not base:
        return {"ok": False, "error": "Hub-Adresse (HUB_BASE_URL) nicht gesetzt."}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{base}/api/cert/terms")
        if r.status_code == 200:
            return {"ok": True, **r.json()}
        return {"ok": False, "error": f"HTTP {r.status_code}"}
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
                             headers=_gateway_headers(), json={"version": version})
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
            r = await c.get(f"{base}/api/cert/eligibility", headers=_gateway_headers())
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
                             headers=_gateway_headers(_cert_key()), json=body)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("ok"):
            log.info("Cert order submitted to reseller hub for %s → %s", target_email, data.get("status"))
            return {"ok": True, "status": data.get("status"), "ref": data.get("ref"),
                    "order_id": data.get("order_id"),
                    "price_cents": data.get("price_cents"),
                    "message": data.get("message", ""), "cert_pem": data.get("cert_pem")}
        if r.status_code in (401, 403):
            return {"ok": False, "error": f"Nicht freigegeben/ungültiger Key (HTTP {r.status_code})."}
        return {"ok": False, "error": data.get("message") or f"Hub HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        log.error("hub cert order error: %s", exc)
        return {"ok": False, "error": f"Netzwerkfehler: {exc}"}


async def get_license() -> dict:
    """Für dieses Konto hinterlegte Fair-Use-Lizenz vom Hub abrufen."""
    base = _base()
    if not (base and _key()):
        return {"ok": False, "error": "Nicht registriert (Anbindung fehlt)."}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{base}/api/license", headers=_gateway_headers())
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("ok"):
            return {"ok": True, "license": data.get("license") or ""}
        if r.status_code == 404:
            return {"ok": False, "error": "Für dieses Konto ist beim Hub keine Lizenz hinterlegt."}
        return {"ok": False, "error": data.get("detail") or data.get("message")
                or f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": f"Verbindungsfehler: {exc}"}


async def cert_get_catalog() -> dict:
    """Anbieter-Katalog des Hubs (Label, Beschreibung, Preis, Verfügbarkeit)."""
    base = _base()
    if not (base and _cert_key()):
        return {"ok": False, "error": "Nicht registriert (Anbindung fehlt)."}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{base}/api/cert/providers",
                            headers=_gateway_headers(_cert_key()))
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("ok"):
            return {"ok": True, "providers": data.get("providers") or [],
                    "currency": data.get("currency") or "EUR"}
        return {"ok": False, "error": data.get("detail") or f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": f"Verbindungsfehler: {exc}"}


async def cert_get_order(order_id: str) -> dict:
    """Status einer Zertifikatsbestellung abfragen (issued → enthält cert_pem)."""
    base = _base()
    if not (base and _cert_key()):
        return {"ok": False, "error": "Nicht registriert (Anbindung fehlt)."}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{base}/api/cert/order/{order_id}",
                            headers=_gateway_headers(_cert_key()))
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("ok"):
            return data
        return {"ok": False, "error": data.get("detail") or f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": f"Verbindungsfehler: {exc}"}


async def cert_topup(amount_cents: int) -> dict:
    """Ask the hub to create a Stripe Checkout session for a prepaid top-up.
    Returns {"ok": True, "checkout_url": ...} — the browser opens that URL."""
    base = _base()
    if not (base and _key()):
        return {"ok": False, "error": "Nicht registriert (Anbindung fehlt)."}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{base}/api/billing/topup",
                             headers=_gateway_headers(),
                             json={"amount_cents": int(amount_cents)})
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("ok"):
            return {"ok": True, "checkout_url": data.get("checkout_url")}
        return {"ok": False, "error": data.get("message") or f"Hub HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": f"Netzwerkfehler: {exc}"}
