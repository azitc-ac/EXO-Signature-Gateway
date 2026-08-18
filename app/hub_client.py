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
    later pull the issued API key (after the operator confirms via email).
    Consent receipts for hub_connect documents are bundled in the payload so the
    hub can persist them as the authoritative acceptance record."""
    import legal_consent
    base = _base()
    email = (settings_store.get("HUB_CUSTOMER_EMAIL") or "").strip().lower()
    name = (settings_store.get("HUB_CUSTOMER_NAME") or "").strip()
    if not base:
        return {"ok": False, "error": "Hub-Adresse (HUB_BASE_URL) nicht gesetzt."}
    if "@" not in email:
        return {"ok": False, "error": "Gültige Kunden-E-Mail erforderlich."}
    claim = secrets.token_urlsafe(32)
    settings_store.update({"HUB_CLAIM_TOKEN": claim})
    receipts = legal_consent.get_consent_receipts_for_hub()
    for r in receipts:
        # Die Mandanten-Domain wird bewusst NICHT mitgesendet: Die Lizenz ist an
        # die Tenant-ID gebunden, nicht an die Domain (license.tenant_error()),
        # und der Zustimmungsbeleg ist ueber die Pruefsumme des Dokumententexts
        # eindeutig. Die Domain diente allein der lesbaren Anzeige — das ist
        # kein Erforderlichkeitsgrund (Art. 5 Abs. 1 lit. c DSGVO).
        r["gateway_version"] = _gateway_version()
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{base}/api/register",
                             json={"email": email, "name": name, "want": "support",
                                   "claim_token": claim, "consent_receipts": receipts})
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("ok"):
            return {"ok": True, "status": data.get("status"),
                    "email_sent": data.get("email_sent"), "message": data.get("message", "")}
        return {"ok": False, "error": data.get("detail") or f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": f"Verbindungsfehler: {exc}"}


def _gateway_version() -> str:
    try:
        from pathlib import Path
        return Path("/app/VERSION").read_text().strip()
    except Exception:
        return ""


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


async def cert_request_invoice(billing: dict | None = None) -> dict:
    """Apply to switch from prepaid to invoice billing (admin approval required).
    billing: {billing_company, billing_address, billing_vat, billing_contact} —
    wird mit dem Antrag übermittelt und im Hub eingetragen."""
    base = _base()
    if not (base and _key()):
        return {"ok": False, "error": "Nicht registriert (Anbindung fehlt)."}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{base}/api/cert/request-invoice",
                             headers=_gateway_headers(), json=billing or {})
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("ok"):
            return data
        return {"ok": False, "error": data.get("detail") or f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": f"Verbindungsfehler: {exc}"}


async def cert_submit_billing(company: str, address: str, vat: str, contact: str,
                              website: str = "") -> dict:
    """Submit/update this account's billing info (self-service)."""
    base = _base()
    if not (base and _key()):
        return {"ok": False, "error": "Nicht registriert (Anbindung fehlt)."}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{base}/api/cert/billing", headers=_gateway_headers(),
                             json={"billing_company": company, "billing_address": address,
                                   "billing_vat": vat, "billing_contact": contact,
                                   "billing_website": website})
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
                "message": data.get("message") or data.get("detail") or data.get("error", ""),
                "digicert": data.get("digicert")}
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


async def account_email_change(neue: str) -> dict:
    """Wechsel der Kontoadresse beim Hub anfordern.

    Ändert hier nichts: Der Hub schickt einen Bestätigungslink an die NEUE
    Adresse, und erst der Klick darin zieht das Konto um. Der API-Schlüssel
    bleibt dabei derselbe — an dieser Anbindung ist danach nichts zu tun.

    Warum die Bestätigung nicht hier abgekürzt wird: Der Schlüssel liegt in
    diesem Gateway. Genügte er, könnte jeder mit Zugang hierher das Konto samt
    Guthaben und Zahlungsbeziehung auf eine fremde Adresse ziehen.
    """
    base = _base()
    if not (base and _key()):
        return {"ok": False, "error": "Nicht registriert (Anbindung fehlt)."}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{base}/api/account/email/change",
                             headers=_gateway_headers(), json={"email": neue})
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("ok"):
            return {"ok": True, "message": data.get("message", ""),
                    "ziel": data.get("ziel", ""), "email_sent": data.get("email_sent")}
        return {"ok": False, "error": data.get("detail") or f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": f"Verbindungsfehler: {exc}"}


async def account_billing_email(neue: str) -> dict:
    """Abweichende Rechnungsadresse anfordern — oder mit "" wieder entfernen.

    Setzen braucht eine Bestaetigung im Zielpostfach (Rechnungen tragen
    Firmenanschrift, USt-IdNr. und Betraege); bis dahin gehen sie weiter an die
    Kontoadresse. Entfernen wirkt sofort, weil der Rueckfall die ohnehin schon
    bestaetigte Kontoadresse ist.
    """
    base = _base()
    if not (base and _key()):
        return {"ok": False, "error": "Nicht registriert (Anbindung fehlt)."}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{base}/api/account/billing-email",
                             headers=_gateway_headers(), json={"email": neue})
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("ok"):
            return {"ok": True, "message": data.get("message", ""),
                    "ziel": data.get("ziel", ""), "email_sent": data.get("email_sent")}
        return {"ok": False, "error": data.get("detail") or f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": f"Verbindungsfehler: {exc}"}


async def account_email_change_cancel() -> dict:
    """Angeforderten Wechsel zurücknehmen."""
    base = _base()
    if not (base and _key()):
        return {"ok": False, "error": "Nicht registriert (Anbindung fehlt)."}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{base}/api/account/email/change/cancel",
                             headers=_gateway_headers())
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        return {"ok": r.status_code == 200 and bool(data.get("ok")),
                "abgebrochen": data.get("abgebrochen")}
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
            d = r.json()
            # Massgeblich ist die Adresse beim Hub, nicht die lokal gespeicherte:
            # nach einem Kontowechsel laeuft das Konto unter einer anderen, und
            # diese Seite zeigte sonst dauerhaft die alte an. Der API-Schluessel
            # bleibt beim Wechsel gleich, die Anbindung merkt sonst nichts davon.
            gemeldet = (d.get("email") or "").strip().lower()
            lokal = (settings_store.get("HUB_CUSTOMER_EMAIL") or "").strip().lower()
            if gemeldet and lokal and gemeldet != lokal:
                settings_store.update({"HUB_CUSTOMER_EMAIL": gemeldet})
                log.info("Kontoadresse lokal nachgezogen: %s -> %s", lokal, gemeldet)
            return {"ok": True, **d}
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


class GuthabenReichtNicht(RuntimeError):
    """Bestellung scheiterte am Guthaben — mit Betrag, nicht nur mit Text.

    Eigene Klasse, weil der Weg vom Hub bis zur Oberfläche durch
    `initiate_renewal()` führt und dort bisher alles zu `RuntimeError(text)`
    einschmolz. Wer den Fehlbetrag aus einem Fliesstext zurücklesen muss,
    verliert ihn beim ersten Umformulieren.
    """

    def __init__(self, text: str, fehlbetrag_cents: int = 0,
                 benoetigt_cents: int = 0, guthaben_cents: int = 0):
        super().__init__(text)
        self.fehlbetrag_cents = int(fehlbetrag_cents or 0)
        self.benoetigt_cents = int(benoetigt_cents or 0)
        self.guthaben_cents = int(guthaben_cents or 0)


async def cert_order(target_email: str, csr_pem: str, extra: dict | None = None,
                     provider: str = "sectigo",
                     ca_terms_accepted_at: str = "") -> dict:
    """Submit an S/MIME cert order via the ONE hub account (operator holds CA creds).
    ca_terms_accepted_at: ISO UTC timestamp when the customer accepted the CA's subscriber
    agreement (required by Hub when the provider has a terms_url)."""
    base = _base()
    if not base:
        return {"ok": False, "error": "Hub-Adresse (HUB_BASE_URL) nicht gesetzt."}
    if not _key():
        return {"ok": False, "error": "Nicht registriert/freigegeben — kein API-Key."}
    body = {"provider": provider or "sectigo", "email": target_email, "csr": csr_pem, "extra": extra or {},
            "ca_terms_accepted_at": ca_terms_accepted_at}
    # Ziffer 13.4: bei ausstehender Zustimmung dürfen keine Zertifikate bestellt
    # werden. Der Hub hat die Dokumente nicht und kann die Aktualität eines
    # Belegs nur anhand der hier geltenden Fassungen beurteilen.
    try:
        import legal_consent
        body["doc_versions"] = legal_consent.current_versions()
    except Exception:                       # Dokumente fehlen → Hub prüft nur Existenz
        pass
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
        if r.status_code == 403 and data.get("grund") == "guthaben":
            # ⚠️ NICHT unter "ungültiger Key" mitverarbeiten. Der Hub schickt
            # Guthabenmangel als 403 mit strukturiertem Körper; die pauschale
            # Meldung darunter schickte den Betreiber zur Anbindung, während in
            # Wahrheit nur Geld fehlte — und der Sammellauf, der auf
            # fehlbetrag_cents wartet, sah nie einen Betrag.
            return {"ok": False, "grund": "guthaben",
                    "benoetigt_cents": int(data.get("benoetigt_cents") or 0),
                    "guthaben_cents": int(data.get("guthaben_cents") or 0),
                    "fehlbetrag_cents": int(data.get("fehlbetrag_cents") or 0),
                    "error": data.get("message") or "Guthaben reicht nicht."}
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
            # DURCHREICHEN, nicht Feld für Feld abschreiben.
            #
            # Hier stand bis v1.7.75 eine Aufzählung. Jedes neue Feld musste
            # dann an vier Stellen erinnert werden — Hub-Endpunkt, hier,
            # Gateway-Endpunkt, Oberfläche — und genau das ist am 27.07.2026
            # dreimal an einem Tag schiefgegangen: die Zahlungsweise kam beim
            # Kauf nicht an, danach fehlte sie in der Verlängerungsansicht,
            # danach ihr Umschaltwunsch. Der Hub liefert bereits eine kuratierte
            # Sicht; sie zu wiederholen bringt nichts als Gelegenheit zum
            # Vergessen.
            return {**data, "ok": True}
        if r.status_code == 404:
            return {"ok": False, "error": "Für dieses Konto ist beim Hub keine Lizenz hinterlegt."}
        return {"ok": False, "error": data.get("detail") or data.get("message")
                or f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": f"Verbindungsfehler: {exc}"}


async def submit_consent_receipts() -> dict:
    """Aktuelle Zustimmungsbelege an den Hub nachreichen.

    Nötig nach einer erneuten Zustimmung: die Belege gingen bisher nur bei der
    Registrierung mit, sodass der Hub dauerhaft die alte Fassung auswies.
    Scheitert der Aufruf, bleibt die lokale Zustimmung trotzdem gültig — der
    Beleg wird beim nächsten Anlauf nachgereicht.
    """
    base = _base()
    if not (base and _key()):
        return {"ok": False, "error": "Nicht registriert (Anbindung fehlt)."}
    import legal_consent
    belege = legal_consent.get_consent_receipts_for_hub()
    if not belege:
        return {"ok": False, "error": "Keine vollständigen Belege vorhanden."}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{base}/api/consent-receipts", headers=_gateway_headers(),
                             json={"consent_receipts": belege})
        if r.status_code == 200:
            return {"ok": True, **r.json()}
        return {"ok": False, "error": f"HTTP {r.status_code}"}
    except Exception as exc:
        return {"ok": False, "error": f"Verbindungsfehler: {exc}"}










async def _license_json(pfad: str, nutzlast: dict) -> dict:
    """Gemeinsamer POST an /api/license/<pfad>. Reicht die Antwort durch."""
    base = _base()
    if not (base and _key()):
        return {"ok": False, "error": "Nicht registriert (Anbindung fehlt)."}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{base}/api/license/{pfad}",
                             headers=_gateway_headers(), json=nutzlast)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("ok"):
            return {**data, "ok": True}
        return {"ok": False, "error": data.get("detail") or f"HTTP {r.status_code}"}
    except Exception as exc:
        return {"ok": False, "error": f"Verbindungsfehler: {exc}"}






async def _license_action(aktion: str) -> dict:
    base = _base()
    if not (base and _key()):
        return {"ok": False, "error": "Nicht registriert (Anbindung fehlt)."}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{base}/api/license/{aktion}", headers=_gateway_headers())
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("ok"):
            return {"ok": True, **data}
        return {"ok": False, "error": data.get("detail") or data.get("message")
                or f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": f"Verbindungsfehler: {exc}"}


async def purchase_license(tenant_id: str, mailboxes: int,
                           zahlungsweise: str = "") -> dict:
    """Fair-Use-Lizenz kaufen — Abrechnung über das Hub-Konto (Guthaben/Rechnung)."""
    base = _base()
    if not (base and _key()):
        return {"ok": False, "error": "Nicht registriert (Anbindung fehlt)."}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{base}/api/license/purchase", headers=_gateway_headers(),
                             json={"tenant_id": tenant_id,
                                   "mailboxes": int(mailboxes),
                                   **({"zahlungsweise": zahlungsweise} if zahlungsweise else {})})
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("ok"):
            return data
        return {"ok": False, "status_code": r.status_code,
                "missing_cents": data.get("missing_cents"),
                "price_cents": data.get("price_cents"),
                "error": data.get("message") or data.get("detail")
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


# ── Automatische Guthabenaufladung (hinterlegtes Zahlungsmittel) ──────────────

async def _billing_auto(method: str, path: str, payload: dict | None = None) -> dict:
    """Gemeinsamer Aufruf für die Automatik-Endpunkte des Hub."""
    base = _base()
    if not (base and _key()):
        return {"ok": False, "error": "Nicht registriert (Anbindung fehlt)."}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            if method == "GET":
                r = await c.get(f"{base}{path}", headers=_gateway_headers())
            else:
                r = await c.post(f"{base}{path}", headers=_gateway_headers(),
                                 json=payload or {})
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("ok"):
            return data
        return {"ok": False, "error": data.get("message") or data.get("detail")
                or f"Hub HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": f"Netzwerkfehler: {exc}"}


async def billing_me() -> dict:
    """Kontostand samt Verlauf.

    Bewusst getrennt von `cert_eligibility()`: das Guthaben trägt Lizenzen UND
    Zertifikate (Ziffer 10.1), die Zertifikatsberechtigung nur letztere. Wer
    keine Zertifikate bezieht, soll seinen Kontostand trotzdem sehen.
    """
    return await _billing_auto("GET", "/api/billing/me")


async def billing_topup(amount_cents: int, doc_versions: dict | None = None) -> dict:
    """Bezahlseite zum Aufladen. Kein Zertifikats-Gate — das Guthaben ist die
    Grundlage beider Leistungen.

    `doc_versions` trägt die im Gateway GELTENDEN Fassungen zum Hub. Der Hub hat
    die Dokumente nicht und könnte sonst nicht erkennen, dass ein vorhandener
    Beleg eine überholte Fassung betrifft — genau daran lief das Gate am
    28.07.2026 vorbei.
    """
    return await _billing_auto("POST", "/api/billing/topup",
                               {"amount_cents": int(amount_cents),
                                "doc_versions": doc_versions or {}})


async def billing_auto_status() -> dict:
    return await _billing_auto("GET", "/api/billing/auto")


async def billing_auto_setup(amount_cents: int = 0,
                             doc_versions: dict | None = None) -> dict:
    """Checkout-URL für die Einrichtung: lädt den Startbetrag auf UND hinterlegt
    das Zahlungsmittel für spätere Nachladungen."""
    return await _billing_auto("POST", "/api/billing/auto/setup",
                               {"amount_cents": int(amount_cents),
                                "doc_versions": doc_versions or {}})


async def billing_auto_amount(amount_cents: int,
                              doc_versions: dict | None = None) -> dict:
    return await _billing_auto("POST", "/api/billing/auto/amount",
                               {"amount_cents": int(amount_cents),
                                "doc_versions": doc_versions or {}})


async def billing_auto_disable() -> dict:
    return await _billing_auto("POST", "/api/billing/auto/disable")


async def billing_refund_preview() -> dict:
    """Wie sich die Auszahlung aufteilen würde. Fragt nur, verändert nichts.

    Grundlage der Entscheidung, ob nach einer Bankverbindung gefragt wird —
    ohne nicht zuordenbaren Anteil wird sie gar nicht erst erhoben.
    """
    return await _billing_auto("GET", "/api/billing/refund/preview")


async def billing_refund(bank_account: dict | None = None) -> dict:
    """Nicht verbrauchtes Guthaben auszahlen lassen — ohne Kündigung.

    Kein `doc_versions`: an sein eigenes Geld zu kommen hängt nicht an einer
    Zustimmung (siehe Gegenstück auf der Betreiber-Seite).

    `bank_account` ({"iban", "holder"}) ist nur nötig, wenn ein nicht
    zuordenbarer Anteil bleibt — die Gegenseite lehnt sonst mit 400 ab.
    """
    return await _billing_auto("POST", "/api/billing/refund",
                               {"bank_account": bank_account or {}})
