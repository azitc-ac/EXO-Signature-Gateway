"""
Backend B: CASTLE Platform — ACME RFC 8823 (Email S/MIME Challenge)

Fully automated renewal flow:
1. initiate_renewal() calls acme_state.initiate_acme_order()
   → places ACME order, saves pending state
2. CASTLE sends challenge email to the user's mailbox
3. handler.py intercepts the email (Subject: ACME: <token>)
   → calls acme_state.handle_challenge_email()
4. Gateway computes key authorization + sends reply email via Graph API
5. Background task polls order → submits CSR → downloads cert → imports
6. Admin notified on success/failure

Private key is generated locally; it never leaves the gateway.
CASTLE Platform: https://acme.castle.cloud/
RFC 8823: https://www.rfc-editor.org/rfc/rfc8823
"""
import logging

from .base import CABackend

log = logging.getLogger(__name__)

_CASTLE_PORTAL = "https://acme.castle.cloud/"
_CASTLE_DOCS   = "https://acme.castle.cloud/documentation/how-it-works/"


class CastleAcmeBackend(CABackend):

    def get_name(self) -> str:
        return "castle_acme"

    def get_label(self) -> str:
        return "CASTLE Platform (ACME RFC 8823 — vollautomatisch)"

    def can_auto_renew(self) -> bool:
        return True

    def get_portal_url(self, email: str, user_config: dict) -> str:
        return _CASTLE_PORTAL

    def get_instructions_html(
        self,
        email: str,
        days_left: int,
        expiry_str: str,
        upload_url: str,
        user_config: dict,
    ) -> str:
        return f"""
<p>Das Gateway hat automatisch ein neues Zertifikat bei der CASTLE Platform beantragt.</p>
<p>Kein weiterer Schritt erforderlich. Die Erneuerung läuft vollautomatisch ab:</p>
<ol style="line-height:2">
  <li>Das Gateway hat einen ACME-Auftrag bei
      <a href="{_CASTLE_PORTAL}">{_CASTLE_PORTAL}</a> platziert.</li>
  <li>CASTLE sendet eine Challenge-E-Mail an <strong>{email}</strong>
      (wird automatisch verarbeitet — kein Handlungsbedarf).</li>
  <li>Das Gateway antwortet auf die Challenge und lädt das neue Zertifikat herunter.</li>
  <li>Ihr Administrator erhält eine Bestätigung sobald das Zertifikat importiert ist.</li>
</ol>
<p style="color:#888;font-size:12px">
  Falls die automatische Erneuerung fehlschlägt, wenden Sie sich an Ihren Administrator.
  Manueller Upload als Fallback:
  <a href="{upload_url}" style="word-break:break-all">{upload_url}</a>
</p>
"""

    async def initiate_renewal(self, email: str, user_config: dict) -> bool:
        """
        Initiates the ACME RFC 8823 order with CASTLE Platform.
        The challenge email interception + response happens in handler.py.
        """
        import acme_state
        staging = bool(user_config.get("staging", False))
        await acme_state.initiate_acme_order(email, user_config, staging=staging)
        log.info("CASTLE ACME: order initiated for %s (staging=%s)", email, staging)
        return True
