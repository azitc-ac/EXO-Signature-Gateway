"""
Backend D: SwissSign Managed PKI — Registration Authority (RA) REST API

Programmatic S/MIME certificate issuance via SwissSign's public RA REST API.
The private key is generated locally and never leaves the gateway; only a CSR
is sent to SwissSign.

Public API surface (SwissSign RA REST API v3.5.0 — public OpenAPI spec at
https://github.com/SwissSign-AG/RaApi/blob/main/api.yaml):
  Base:   https://api.ra.swisssign.ch      (production)
          https://api.ra.pre.swisssign.ch  (pre-production / sandbox)
  Auth:   POST {base}/v2/jwt/{username}
            body (x-www-form-urlencoded): userSecret=<API key>
            returns: JWT (text/plain) -> "Authorization: Bearer <jwt>" on every further call
  Issue:  POST {base}/v2/issue
            body:    {productReference, csr, synchrone: true, includeCertificateChain: true,
                      acceptTandC: true}
            returns: CertificateOrder {uuid, status, certificate: {certificate: <base64 DER>},
                      certificateChain: [<base64 DER>, ...]}
  Status: POST {base}/v2/order/{orderReference}/status  -> CertificateOrderStatus enum
            (NEW | KEY_VALIDATION | PRE_VALIDATION | GENERATE_TBS | PENDING_AUTH |
             PRE_ISSUE | ISSUE | POST_VALIDATION | FINALIZE_ISSUANCE | ISSUED |
             REVOKED | FAILED | REJECTED | PENDING_CSR_RENEWAL | UNKNOWN)

Domain pre-validation (one-time per domain, DNS TXT record — same "cabdns"
pattern already used for the Hub's Sectigo domain verification):
  POST {base}/v2/client/domain/{clientReference}/register  [domain, ...]
       -> ClientDNS {uuid ("cld-..."), randomValue}  — add randomValue as a DNS TXT record
  POST {base}/v2/client/domain/{prevalidatedDomainReference}/validate

Docs: https://github.com/SwissSign-AG/RaApi (public OpenAPI spec)
      https://support.swisspki.com

IMPORTANT — this is a scaffold built from SwissSign's PUBLIC OpenAPI spec. It
has NOT been validated against a live RA account. Account-specific values that
MUST be filled in once an account exists:
  - SWISSSIGN_USERNAME / SWISSSIGN_API_KEY   : RA login credentials
  - SWISSSIGN_PRODUCT_REFERENCE ("pma-...")  : the S/MIME certificate product UUID
  - SWISSSIGN_CLIENT_REFERENCE  ("cli-...")  : the RA client UUID, needed for domain
                                                pre-validation (setup_wizard-side, not here)
Some SwissSign S/MIME products additionally require the mailbox owner to click
a validation link sent by email (product flag "isEmailBoxValidationRequired")
even once the domain is pre-validated — unconfirmed whether the RA's assigned
product has this requirement. If it does, `synchrone: true` will not return a
finished certificate immediately; the order stays at PENDING_AUTH and must be
polled via the status endpoint instead (same "issued asynchronously" pattern
the caller already tolerates for the reseller path).
"""
import asyncio
import base64
import logging

import httpx

import settings_store
from .base import CABackend

log = logging.getLogger(__name__)

_DEFAULT_API_BASE = "https://api.ra.swisssign.ch"
_PORTAL = "https://www.swisssign.com/en/support/"
_DOCS = "https://github.com/SwissSign-AG/RaApi"


def _api_base() -> str:
    return (settings_store.get("SWISSSIGN_API_BASE") or _DEFAULT_API_BASE).strip().rstrip("/")


async def _jwt(base: str, username: str, api_key: str) -> str:
    """Exchange username + API key for a short-lived JWT. Fetched fresh per
    issuance call rather than cached — renewals are infrequent, so the extra
    round trip is cheap and avoids any expiry-tracking complexity."""
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            f"{base}/v2/jwt/{username}",
            data={"userSecret": api_key},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if r.status_code != 200:
        raise RuntimeError(f"SwissSign JWT-Anfrage fehlgeschlagen: HTTP {r.status_code} {r.text[:300]}")
    return r.text.strip()


def _generate_key_and_csr_pem(email: str) -> tuple[bytes, bytes]:
    """Generate an RSA-2048 key + PEM CSR (CN + rfc822 SAN = email).
    Returns (key_pem, csr_pem)."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, email)]))
        .add_extension(x509.SubjectAlternativeName([x509.RFC822Name(email)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    csr_pem = csr.public_bytes(serialization.Encoding.PEM)
    return key_pem, csr_pem


def _der_b64_to_pem(der_b64: str) -> bytes:
    """SwissSign returns certificates as base64-encoded DER, not PEM."""
    der = base64.b64decode(der_b64)
    b64 = base64.encodebytes(der).decode()  # 64-char wrapped, matches PEM convention
    return f"-----BEGIN CERTIFICATE-----\n{b64}-----END CERTIFICATE-----\n".encode()


class SwissSignBackend(CABackend):

    def get_name(self) -> str:
        return "swisssign"

    def get_label(self) -> str:
        return "SwissSign Managed PKI (RA REST API — automatisch)"

    def can_auto_renew(self) -> bool:
        # Same reasoning as Sectigo: stays True so the scheduler attempts it;
        # initiate_renewal raises a clear error on misconfiguration, which
        # falls back to manual notification.
        return True

    def is_ready(self) -> bool:
        mode = (settings_store.get("SWISSSIGN_MODE") or "reseller").strip().lower()
        if mode == "direct":
            return all(settings_store.get(k) for k in
                       ("SWISSSIGN_USERNAME", "SWISSSIGN_API_KEY", "SWISSSIGN_PRODUCT_REFERENCE"))
        import hub_client
        return hub_client.cert_is_registered()

    def not_ready_reason(self) -> str:
        mode = (settings_store.get("SWISSSIGN_MODE") or "reseller").strip().lower()
        if mode == "direct":
            return "Direktkauf: SwissSign-RA-Zugangsdaten unvollständig (Erweitert-Tab)."
        return "Reseller: Cert-Provider-Hub noch nicht registriert/freigegeben (Erweitert-Tab)."

    def get_portal_url(self, email: str, user_config: dict) -> str:
        return (user_config.get("portal_url") or "").strip() or _PORTAL

    def get_instructions_html(
        self,
        email: str,
        days_left: int,
        expiry_str: str,
        upload_url: str,
        user_config: dict,
    ) -> str:
        return f"""
<p>Das Gateway erneuert das S/MIME-Zertifikat für <strong>{email}</strong> automatisch
   über SwissSign Managed PKI.</p>
<p>Kein Handlungsbedarf — der Ablauf ist vollautomatisch:</p>
<ol style="line-height:2">
  <li>Das Gateway erzeugt lokal ein neues Schlüsselpaar und einen CSR
      (der private Schlüssel verlässt das Gateway nie).</li>
  <li>Der CSR wird über die SwissSign-RA-API eingereicht.</li>
  <li>Nach Ausstellung lädt das Gateway das Zertifikat automatisch herunter und
      importiert es.</li>
  <li>Ihr Administrator erhält eine Bestätigung.</li>
</ol>
<p style="color:#888;font-size:12px">
  Sollte die automatische Ausstellung fehlschlagen (z. B. weil das Postfach noch per
  Link bestätigt werden muss), informiert das Gateway den Administrator.
  Manueller Upload als Fallback:
  <a href="{upload_url}" style="word-break:break-all">{upload_url}</a>
</p>
"""

    # ── Automated issuance ────────────────────────────────────────────────────

    async def initiate_renewal(self, email: str, user_config: dict) -> bool:
        """Enroll a new S/MIME cert. Two modes (setting SWISSSIGN_MODE):

          "reseller" (default): route the order through the operator's provider hub
             (certdeploy) — the operator holds the SwissSign RA credentials, this
             gateway only sends a locally-generated CSR. No RA creds needed here.
          "direct": use this gateway's own SwissSign RA credentials (SWISSSIGN_*).

        Raises on misconfiguration so the caller can fall back to manual notification.
        """
        mode = (settings_store.get("SWISSSIGN_MODE") or "reseller").strip().lower()
        if mode == "direct":
            return await self._direct_order(email, user_config)
        return await self._reseller_order(email, user_config)

    async def _reseller_order(self, email: str, user_config: dict) -> bool:
        """Submit a CSR to the operator's provider hub (reseller relay)."""
        import hub_client
        if not hub_client.cert_is_registered():
            raise RuntimeError(
                "Reseller-Modus: Cert-Provider-Hub nicht registriert/freigegeben "
                "(Erweitert-Tab → Cert-Provider-Hub)."
            )
        _key_pem, csr_pem = _generate_key_and_csr_pem(email)
        extra = {k: user_config[k] for k in ("firstName", "lastName") if user_config.get(k)}
        result = await hub_client.cert_order(email, csr_pem.decode(), extra, provider="swisssign")
        if not result.get("ok"):
            raise RuntimeError(f"Reseller-Order fehlgeschlagen: {result.get('error')}")
        cert_pem = result.get("cert_pem")
        if cert_pem:
            import smime_store
            info = smime_store.store_pem_slot(email, cert_pem.encode(), _key_pem)
            log.info("SwissSign (reseller): cert imported for %s slot=%s", email, info.get("slot_id"))
            return True
        log.info("SwissSign (reseller): order submitted for %s (ref=%s, status=%s) — "
                 "cert wird nach Ausstellung nachgeliefert",
                 email, result.get("ref"), result.get("status"))
        return True

    async def _direct_order(self, email: str, user_config: dict) -> bool:
        import smime_store

        username = (settings_store.get("SWISSSIGN_USERNAME") or "").strip()
        api_key = settings_store.get("SWISSSIGN_API_KEY") or ""
        product_ref = (settings_store.get("SWISSSIGN_PRODUCT_REFERENCE") or "").strip()
        if not (username and api_key and product_ref):
            raise RuntimeError(
                "SwissSign-Zugangsdaten unvollständig (SWISSSIGN_USERNAME / SWISSSIGN_API_KEY / "
                "SWISSSIGN_PRODUCT_REFERENCE im Erweitert-Tab hinterlegen)."
            )

        base = _api_base()
        jwt = await _jwt(base, username, api_key)
        headers = {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}

        key_pem, csr_pem = _generate_key_and_csr_pem(email)
        body = {
            "productReference": product_ref,
            "csr": csr_pem.decode(),
            "synchrone": True,
            "includeCertificateChain": True,
            "acceptTandC": True,
        }

        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{base}/v2/issue", json=body, headers=headers)
        if r.status_code != 200:
            raise RuntimeError(f"SwissSign enroll fehlgeschlagen: HTTP {r.status_code} {r.text[:300]}")
        order = r.json()
        order_ref = order.get("uuid")
        status = order.get("status")

        # synchrone=True should return the finished cert directly when no extra
        # mailbox-link validation is required for this product. If the product
        # DOES require it (isEmailBoxValidationRequired), status stays at
        # PENDING_AUTH and there's nothing more this call can do — surface that
        # clearly rather than silently polling forever for a click that may
        # never come from an unattended process.
        if status == "PENDING_AUTH":
            raise RuntimeError(
                f"SwissSign: Order {order_ref} wartet auf Postfach-Bestätigung per Link "
                f"(isEmailBoxValidationRequired) — kein automatischer Abschluss möglich."
            )
        if status not in ("ISSUED",):
            # Not immediately finished but not blocked on a human click either —
            # poll the status endpoint for a while (covers PRE_VALIDATION/ISSUE/
            # FINALIZE_ISSUANCE transient states).
            order = await self._poll_order(base, headers, order_ref)
            status = order.get("status")
        if status != "ISSUED":
            raise RuntimeError(f"SwissSign: Order {order_ref} nicht ausgestellt (status={status})")

        cert_b64 = (order.get("certificate") or {}).get("certificate")
        if not cert_b64:
            raise RuntimeError(f"SwissSign: Order {order_ref} als ISSUED markiert, aber kein Zertifikat enthalten")
        cert_pem = _der_b64_to_pem(cert_b64)

        info = smime_store.store_pem_slot(email, cert_pem, key_pem)
        log.info("SwissSign: cert imported for %s — expiry=%s slot=%s",
                 email, info.get("expiry"), info.get("slot_id"))
        return True

    async def _poll_order(self, base: str, headers: dict, order_ref: str) -> dict:
        """Poll the order until it reaches a terminal status. Up to ~15 min."""
        for attempt in range(90):
            await asyncio.sleep(10)
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(f"{base}/v2/order/{order_ref}", headers=headers)
            if r.status_code != 200:
                log.debug("SwissSign: order %s poll HTTP %s (attempt %d)", order_ref, r.status_code, attempt + 1)
                continue
            order = r.json()
            status = order.get("status")
            if status in ("ISSUED", "FAILED", "REJECTED", "REVOKED"):
                return order
            log.debug("SwissSign: order %s status=%s (attempt %d)", order_ref, status, attempt + 1)
        raise RuntimeError(f"SwissSign: Order {order_ref} nach Timeout weiterhin nicht abgeschlossen")
