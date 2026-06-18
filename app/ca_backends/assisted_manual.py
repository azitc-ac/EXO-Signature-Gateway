"""
Backend A: Assisted Manual Renewal

For CAs where no API access is available (Actalis Free S/MIME, similar).
- Detects expiry, notifies the user with step-by-step renewal instructions
- User obtains new PKCS#12 from the CA portal manually
- User uploads via the Gateway self-service link
- Gateway auto-imports and notifies admin of success/failure
"""
from .base import CABackend

_ACTALIS_DEFAULT_URL = "https://extrassl.actalis.it/portal/uapub/freeemailcert"


class AssistedManualBackend(CABackend):

    def get_name(self) -> str:
        return "assisted_manual"

    def get_label(self) -> str:
        return "Assistiert manuell (z.B. Actalis Free S/MIME)"

    def can_auto_renew(self) -> bool:
        return False

    def get_portal_url(self, email: str, user_config: dict) -> str:
        return (user_config.get("portal_url") or "").strip() or _ACTALIS_DEFAULT_URL

    def get_instructions_html(
        self,
        email: str,
        days_left: int,
        expiry_str: str,
        upload_url: str,
        user_config: dict,
    ) -> str:
        portal_url = self.get_portal_url(email, user_config)
        return f"""
<ol style="line-height:2.2;margin:0 0 0 18px;padding:0">
  <li>Öffnen Sie das Zertifikatsportal Ihrer CA:<br>
      <a href="{portal_url}" style="color:#0066cc">{portal_url}</a></li>
  <li>Geben Sie Ihre E-Mail-Adresse <strong>{email}</strong> ein und bestätigen Sie
      den Verifizierungscode, der an Ihre Adresse gesendet wird.</li>
  <li>Laden Sie die PKCS12-Datei (<code>.p12</code> / <code>.pfx</code>) herunter,
      die das Portal für Sie erstellt hat.</li>
  <li>Laden Sie die Datei über folgenden sicheren Link direkt in das Gateway hoch:<br>
      <a href="{upload_url}" style="color:#0066cc;font-weight:700;word-break:break-all">{upload_url}</a></li>
</ol>
<p style="color:#888;font-size:12px;margin-top:12px">
  Der Upload-Link ist 30 Tage gültig. Falls er abgelaufen ist, wenden Sie sich
  an Ihren Gateway-Administrator.
</p>
"""
