"""DigiCert-Direktanbindung — der Kunde nutzt sein EIGENES CertCentral-Konto.

Transparente Alternative zum Hub-Weg (hub:digicert): wer sich selbst kümmern
will, hinterlegt seinen API-Key im Anbindung-Tab; Bestellung, Abholung und
Import laufen trotzdem vollautomatisch über das Gateway. Schlüssel entsteht
lokal und verlässt das Gateway nie; offene Orders werden über den bestehenden
hub_orders-Poller (source=digicert_direct) abgeholt.
"""
import logging

from .base import CABackend
from .csr import generate_key_and_csr_pem as _generate_key_and_csr_pem

log = logging.getLogger(__name__)


class DigiCertDirectBackend(CABackend):

    def get_name(self) -> str:
        return "digicert_direct"

    def get_label(self) -> str:
        return "DigiCert S/MIME — eigenes CertCentral-Konto (Direktanbindung)"

    def can_auto_renew(self) -> bool:
        return True

    def is_ready(self) -> bool:
        import digicert_client
        return digicert_client.is_configured()

    def not_ready_reason(self) -> str:
        return "DigiCert-API-Key nicht hinterlegt (Anbindung-Tab → DigiCert-Direktanbindung)."

    def get_portal_url(self, email: str, user_config: dict) -> str:
        return "https://www.digicert.com/account/"

    def get_instructions_html(self, email: str, days_left: int, expiry_str: str,
                              upload_url: str, user_config: dict) -> str:
        return f"""
<p>Ihr S/MIME-Zertifikat für <strong>{email}</strong> läuft in {days_left} Tagen ab
({expiry_str}).</p>
<p>Die Erneuerung wurde automatisch über Ihr <strong>DigiCert-CertCentral-Konto</strong>
beauftragt — Sie müssen nichts weiter tun. Das neue Zertifikat wird nach
Ausstellung automatisch eingespielt.</p>
<p style="color:#888;font-size:12px">Falls die automatische Ausstellung
fehlschlägt (z.B. Domain-Validierung abgelaufen), informiert das Gateway den
Administrator. Manueller Upload als Fallback:
<a href="{upload_url}" style="word-break:break-all">{upload_url}</a></p>
"""

    async def initiate_renewal(self, email: str, user_config: dict) -> bool:
        import digicert_client
        import hub_orders
        if not digicert_client.is_configured():
            raise RuntimeError(self.not_ready_reason())
        key_pem, csr_pem = _generate_key_and_csr_pem(email)
        result = await digicert_client.order(email, csr_pem.decode())
        if not result.get("ok"):
            raise RuntimeError(f"DigiCert-Bestellung fehlgeschlagen: {result.get('message')}")
        order_id = result.get("ref")
        if not order_id:
            raise RuntimeError(
                "DigiCert lieferte keine Order-ID — Bestellung abgebrochen, "
                "damit kein verwaister Schlüssel entsteht.")
        # Schlüssel persistieren; Abholung übernimmt der Order-Poller
        hub_orders.save_pending(str(order_id), email, "digicert",
                                key_pem, source="digicert_direct")
        log.info("DigiCert-Direkt: Order %s für %s eingereicht — Zertifikat wird "
                 "nach Ausstellung automatisch abgeholt", order_id, email)
        return True
