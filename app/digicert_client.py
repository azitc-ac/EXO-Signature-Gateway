"""DigiCert CertCentral Direktanbindung — Kunde nutzt sein EIGENES Konto.

Bewusste Ausnahme zur Hub-only-Regel (Entscheidung 2026-07-15): Wer sich
selbst kümmern will, hinterlegt hier seinen eigenen CertCentral-API-Key;
wer es einfach haben will, geht über den Hub (hub:digicert). Beide Wege
existieren transparent nebeneinander.

API (dev.digicert.com, verifiziert 2026-07-14/15 — identisch zur
Hub-Implementierung in sig-provider/certrelay.py):
  Auth: Header X-DC-DEVKEY
  POST /order/certificate/secure_email_mailbox  → {id}
  GET  /order/certificate/{order_id}            → status, certificate.id
  GET  /certificate/{cert_id}/download/format/pem_noroot
  POST /domain  bzw. /domain/{id}/validation    → DCV-Token (TXT am Apex)
  PUT  /domain/{id}/dcv/validate-token          → DCV-Prüfung anstoßen
"""
import logging
import re

import httpx

import settings_store

log = logging.getLogger(__name__)

# Endzustände laut Order-Status-Glossar; alles andere = noch in Arbeit
_FINAL_BAD = {"rejected", "revoked", "canceled", "expired"}


def _s(key: str) -> str:
    return str(settings_store.get(key) or "").strip()


def _base() -> str:
    return (_s("DIGICERT_API_BASE") or "https://www.digicert.com/services/v2").rstrip("/")


def _headers() -> dict:
    return {"X-DC-DEVKEY": settings_store.get("DIGICERT_API_KEY") or "",
            "Content-Type": "application/json"}


def _int_or(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def is_configured() -> bool:
    return bool(_headers()["X-DC-DEVKEY"])


async def test() -> dict:
    if not is_configured():
        return {"ok": False, "message": "API-Key fehlt (X-DC-DEVKEY)."}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{_base()}/user/me", headers=_headers())
        if r.status_code == 200:
            user = r.json()
            who = user.get("email") or user.get("username") or "?"
            return {"ok": True, "message": f"Verbindung OK (Konto: {who})."}
        return {"ok": False, "message": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "message": f"Verbindungsfehler: {exc}"}


async def order(target_email: str, csr_pem: str) -> dict:
    """Secure-Email-Mailbox-Zertifikat bestellen.
    Returns {ok, status: submitted|error|unconfigured, ref?, message}."""
    if not is_configured():
        return {"ok": False, "status": "unconfigured",
                "message": "DigiCert-API-Key nicht konfiguriert (Anbindung-Tab)."}
    days = _int_or(_s("DIGICERT_VALIDITY_DAYS"), 365)
    payment = _s("DIGICERT_PAYMENT_METHOD") or "profile"
    if payment not in ("profile", "balance"):
        payment = "profile"
    body = {
        "certificate": {
            "profile_type": "multipurpose",
            "csr": csr_pem,
            "emails": [target_email],
            "common_name_indicator": "email_address",
            "signature_hash": "sha256",
            "usage_designation": {"primary_usage": "dual_use"},
        },
        "order_validity": {"days": days},
        "payment_method": payment,
        "skip_approval": True,
    }
    org_id = _int_or(_s("DIGICERT_ORG_ID"))
    if org_id is not None:
        body["organization"] = {"id": org_id}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{_base()}/order/certificate/secure_email_mailbox",
                             json=body, headers=_headers())
        if r.status_code in (200, 201):
            data = r.json()
            ref = str(data.get("id") or "")
            if not ref:
                return {"ok": False, "status": "error",
                        "message": f"DigiCert-Antwort ohne Order-ID: {r.text[:200]}"}
            return {"ok": True, "status": "submitted", "ref": ref,
                    "message": f"Order eingereicht (order_id={ref})."}
        return {"ok": False, "status": "error",
                "message": f"DigiCert HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "status": "error", "message": f"Verbindungsfehler: {exc}"}


async def collect(order_id: str) -> dict:
    """Order-Status prüfen, bei issued Zertifikat laden.
    Returns {ok, status: issued|pending|rejected, cert_pem?, note?}.
    ok=False nur bei echten Fehlern (Netz/Auth) — pending ist ok=True,
    damit der Poller nicht bei jedem Durchlauf warnt."""
    if not is_configured():
        return {"ok": False, "status": "pending",
                "error": "DigiCert-API-Key nicht konfiguriert."}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(f"{_base()}/order/certificate/{order_id}", headers=_headers())
            if r.status_code != 200:
                return {"ok": False, "status": "pending",
                        "error": f"DigiCert HTTP {r.status_code}: {r.text[:200]}"}
            data = r.json()
            status = (data.get("status") or "").lower()
            if status in _FINAL_BAD:
                return {"ok": True, "status": "rejected",
                        "note": f"DigiCert-Order-Status: {status}"}
            if status != "issued":
                return {"ok": True, "status": "pending",
                        "note": f"DigiCert-Order-Status: {status or '?'}"}
            cert_id = (data.get("certificate") or {}).get("id")
            if not cert_id:
                return {"ok": True, "status": "pending",
                        "note": "issued, aber certificate.id fehlt noch"}
            r2 = await c.get(f"{_base()}/certificate/{cert_id}/download/format/pem_noroot",
                             headers=_headers())
            if r2.status_code == 200 and "BEGIN CERTIFICATE" in r2.text:
                return {"ok": True, "status": "issued", "cert_pem": r2.text}
            return {"ok": True, "status": "pending",
                    "note": f"Download noch nicht möglich (HTTP {r2.status_code})"}
    except Exception as exc:
        return {"ok": False, "status": "pending", "error": f"Verbindungsfehler: {exc}"}


async def domain_setup(domain: str) -> dict:
    """Domain im eigenen CertCentral-Konto anlegen, DNS-TXT-DCV-Token holen.
    TXT gehört an den APEX (Name leer/@); Token 30 Tage, Validierung 398."""
    if not is_configured():
        return {"ok": False, "message": "DigiCert-API-Key nicht konfiguriert."}
    org_id = _int_or(_s("DIGICERT_ORG_ID"))
    if org_id is None:
        return {"ok": False, "message": "Org-ID fehlt (für das Anlegen von Domains Pflicht)."}
    domain = domain.strip().lower().lstrip("@")
    if not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", domain):
        return {"ok": False, "message": f"Ungültige Domain: {domain!r}"}
    body = {"name": domain, "organization": {"id": org_id},
            "validations": [{"type": "ov"}], "dcv_method": "dns-txt-token"}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{_base()}/domain", json=body, headers=_headers())
            if r.status_code in (200, 201):
                data = r.json()
                tok = data.get("dcv_token") or {}
                return {"ok": True, "domain_id": data.get("id"), "token": tok.get("token"),
                        "expires": tok.get("expiration_date"),
                        "message": "Domain angelegt, DCV-Token erzeugt."}
            if r.status_code == 400 and "exist" in r.text.lower():
                rl = await c.get(f"{_base()}/domain", headers=_headers())
                dom_id = None
                if rl.status_code == 200:
                    for d in (rl.json().get("domains") or []):
                        if (d.get("name") or "").lower() == domain:
                            dom_id = d.get("id")
                            break
                if dom_id:
                    r2 = await c.post(f"{_base()}/domain/{dom_id}/validation",
                                      json={"validations": [{"type": "ov"}],
                                            "dcv_method": "dns-txt-token"},
                                      headers=_headers())
                    if r2.status_code in (200, 201):
                        tok = (r2.json() or {}).get("dcv_token") or {}
                        return {"ok": True, "domain_id": dom_id, "token": tok.get("token"),
                                "expires": tok.get("expiration_date"),
                                "message": "Domain existierte bereits, neuer DCV-Token erzeugt."}
                    return {"ok": False, "message": f"DigiCert HTTP {r2.status_code}: {r2.text[:200]}"}
            return {"ok": False, "message": f"DigiCert HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "message": f"Verbindungsfehler: {exc}"}


async def domain_check(domain_id) -> dict:
    """DCV-Prüfung anstoßen (Pendant zum 'Check TXT'-Button)."""
    if not is_configured():
        return {"ok": False, "message": "DigiCert-API-Key nicht konfiguriert."}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.put(f"{_base()}/domain/{domain_id}/dcv/validate-token",
                            headers=_headers())
        if r.status_code in (200, 204):
            return {"ok": True, "message": "DigiCert-DCV bestätigt."}
        return {"ok": False,
                "message": f"DCV noch offen (HTTP {r.status_code}: {r.text[:150]})."}
    except Exception as exc:
        return {"ok": False, "message": f"Verbindungsfehler: {exc}"}
