"""
Backend C: Sectigo Certificate Manager (SCM) — S/MIME REST API

Programmatic S/MIME certificate issuance via Sectigo's SCM REST API.
The private key is generated locally and never leaves the gateway; only a CSR
is sent to Sectigo.

Public API surface (Sectigo SCM REST API, "smime/v1"):
  Base:    https://cert-manager.com/api  (EU/other regions may differ — configurable)
  Enroll:  POST  {base}/smime/v1/enroll
             headers: login, password, customerUri, Content-Type: application/json
             body:    { orgId, certType, term, email, firstName, lastName,
                        csr, ... }   (fields are account/version-specific)
             returns: { orderNumber, backendCertId }
  Collect: GET   {base}/smime/v1/collect/{orderNumber}?format=x509
             200 + cert body when issued; 4xx while still pending
  Status:  GET   {base}/smime/v1/{backendCertId}   → { status, ... }

Docs: https://scm.devx.sectigo.com/  ·  https://docs.sectigo.com/

IMPORTANT — this is a scaffold built from Sectigo's PUBLIC API documentation. It
has NOT been validated against a live SCM account. Several values are strictly
account-specific and MUST be filled in once a Sectigo account exists:
  - SECTIGO_ORG_ID    : the Organization ID the S/MIME profile belongs to
  - SECTIGO_CERT_TYPE : the certificate profile / type ID for S/MIME
  - SECTIGO_TERM      : validity term in days/years accepted by that profile
  - the exact enroll body field names may vary by SCM version/profile
Additionally, Sectigo S/MIME issuance normally requires the recipient's
Organization/Person and email to be pre-validated in SCM; fully unattended
auto-renewal only works when the account is provisioned for it.
"""
import asyncio
import logging

import httpx

import settings_store
from .base import CABackend

log = logging.getLogger(__name__)

_DEFAULT_API_BASE = "https://cert-manager.com/api"
_PORTAL = "https://cert-manager.com/"
_DOCS = "https://scm.devx.sectigo.com/"


def _api_base() -> str:
    return (settings_store.get("SECTIGO_API_BASE") or _DEFAULT_API_BASE).strip().rstrip("/")


def _auth_headers() -> dict:
    return {
        "login": (settings_store.get("SECTIGO_LOGIN") or "").strip(),
        "password": settings_store.get("SECTIGO_PASSWORD") or "",
        "customerUri": (settings_store.get("SECTIGO_CUSTOMER_URI") or "").strip(),
        "Content-Type": "application/json;charset=utf-8",
    }


def _generate_key_and_csr_pem(email: str) -> tuple[bytes, bytes]:
    """Generate an RSA-2048 key + PEM CSR (CN + rfc822 SAN = email).

    RSA is the safest default for S/MIME across CAs; make key type configurable
    later if a profile requires EC. Returns (key_pem, csr_pem).
    """
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


class SectigoBackend(CABackend):

    def get_name(self) -> str:
        return "sectigo"

    def get_label(self) -> str:
        return "Sectigo Certificate Manager (S/MIME REST API — automatisch)"

    def can_auto_renew(self) -> bool:
        # Requires SCM credentials + org/profile provisioning. can_auto_renew stays
        # True so the scheduler will attempt it; initiate_renewal raises a clear
        # error if the account config is incomplete (then manual notification kicks in).
        return True

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
   über Sectigo Certificate Manager.</p>
<p>Kein Handlungsbedarf — der Ablauf ist vollautomatisch:</p>
<ol style="line-height:2">
  <li>Das Gateway erzeugt lokal ein neues Schlüsselpaar und einen CSR
      (der private Schlüssel verlässt das Gateway nie).</li>
  <li>Der CSR wird über die Sectigo-SCM-API eingereicht.</li>
  <li>Nach Ausstellung lädt das Gateway das Zertifikat automatisch herunter und
      importiert es.</li>
  <li>Ihr Administrator erhält eine Bestätigung.</li>
</ol>
<p style="color:#888;font-size:12px">
  Sollte die automatische Ausstellung fehlschlagen (z. B. weil die Organisation/Person
  in Sectigo noch nicht freigegeben ist), informiert das Gateway den Administrator.
  Manueller Upload als Fallback:
  <a href="{upload_url}" style="word-break:break-all">{upload_url}</a>
</p>
"""

    # ── Automated issuance ────────────────────────────────────────────────────

    async def initiate_renewal(self, email: str, user_config: dict) -> bool:
        """Enroll a new S/MIME cert via SCM, poll until issued, import locally.

        Raises on misconfiguration so the caller can fall back to manual
        notification (mirrors how the ACME backend surfaces failures).
        """
        import smime_store

        login = (settings_store.get("SECTIGO_LOGIN") or "").strip()
        password = settings_store.get("SECTIGO_PASSWORD") or ""
        customer = (settings_store.get("SECTIGO_CUSTOMER_URI") or "").strip()
        org_id = settings_store.get("SECTIGO_ORG_ID")
        cert_type = settings_store.get("SECTIGO_CERT_TYPE")
        term = settings_store.get("SECTIGO_TERM")
        if not (login and password and customer):
            raise RuntimeError(
                "Sectigo-Zugangsdaten unvollständig (SECTIGO_LOGIN / SECTIGO_PASSWORD / "
                "SECTIGO_CUSTOMER_URI im Erweitert-Tab hinterlegen)."
            )
        if not (org_id and cert_type):
            raise RuntimeError(
                "Sectigo-Profil unvollständig (SECTIGO_ORG_ID und SECTIGO_CERT_TYPE müssen "
                "zum SCM-Account passen — im Erweitert-Tab hinterlegen)."
            )

        key_pem, csr_pem = _generate_key_and_csr_pem(email)

        # Person fields: Sectigo requires at least a name. Derive from local dir /
        # user_config; fall back to the local part of the email address.
        first = (user_config.get("first_name") or "").strip()
        last = (user_config.get("last_name") or "").strip()
        if not (first or last):
            last = email.split("@", 1)[0]

        body: dict = {
            "orgId": org_id,
            "certType": cert_type,
            "term": term,
            "email": email,
            "firstName": first,
            "lastName": last,
            "csr": csr_pem.decode(),
        }
        # Optional passthrough of account-specific extras (eppn, customFields, phone…)
        for k in ("phone", "eppn", "commonName", "secondaryEmails", "customFields"):
            if user_config.get(k) is not None:
                body[k] = user_config[k]

        base = _api_base()
        headers = _auth_headers()

        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{base}/smime/v1/enroll", json=body, headers=headers)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Sectigo enroll fehlgeschlagen: HTTP {r.status_code} {r.text[:300]}")
        data = r.json()
        order_number = data.get("orderNumber") or data.get("sslId") or data.get("id")
        if not order_number:
            raise RuntimeError(f"Sectigo enroll ohne orderNumber: {str(data)[:200]}")
        log.info("Sectigo: enrolled S/MIME for %s → orderNumber=%s", email, order_number)

        # Poll collect until the cert is issued (SCM issuance is usually seconds to
        # a few minutes for pre-validated profiles). Up to ~15 min.
        cert_pem: bytes | None = None
        for attempt in range(90):
            await asyncio.sleep(10)
            async with httpx.AsyncClient(timeout=30) as c:
                cr = await c.get(
                    f"{base}/smime/v1/collect/{order_number}",
                    params={"format": "x509CO"},  # x509CO = cert + chain PEM; adjust per account
                    headers=headers,
                )
            if cr.status_code == 200 and b"BEGIN CERTIFICATE" in cr.content:
                cert_pem = cr.content
                break
            log.debug("Sectigo: collect %s pending (HTTP %s, attempt %d)", order_number, cr.status_code, attempt + 1)
        if not cert_pem:
            raise RuntimeError(f"Sectigo: Zertifikat für {email} nach Timeout nicht abrufbar (order {order_number})")

        info = smime_store.store_pem_slot(email, cert_pem, key_pem)
        log.info("Sectigo: cert imported for %s — expiry=%s slot=%s",
                 email, info.get("expiry"), info.get("slot_id"))
        return True
