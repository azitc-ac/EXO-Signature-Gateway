"""Generisches Hub-Backend — ein Katalog-Eintrag des Hubs = ein Backend.

Die komplette CA-Kommunikation (Sectigo, SwissSign, …) liegt beim Hub;
das Gateway erzeugt nur lokal Schlüssel+CSR, bestellt über den Hub und
holt das fertige Zertifikat per Polling ab (hub_orders). Anbieter, Label,
Beschreibung und Preis kommen dynamisch aus hub_catalog — neue Anbieter
oder Preisänderungen brauchen kein Gateway-Release.
"""
import logging

from .base import CABackend
from .csr import generate_key_and_csr_pem as _generate_key_and_csr_pem

log = logging.getLogger(__name__)


class HubProviderBackend(CABackend):

    def __init__(self, provider: dict):
        self._p = provider or {}

    def get_name(self) -> str:
        return f"hub:{self._p.get('id', '')}"

    def get_label(self) -> str:
        import hub_catalog
        label = self._p.get("label") or self._p.get("id", "?")
        price = hub_catalog.format_price(self._p.get("price_cents"))
        months = self._p.get("validity_months")
        parts = [label]
        if price:
            parts.append(f"{price}/Zertifikat")
        if months:
            parts.append(f"{months} Monate")
        return " — ".join(parts) + " (über Hub)"

    def can_auto_renew(self) -> bool:
        return True

    def is_ready(self) -> bool:
        import hub_client
        return bool(self._p.get("available")) and hub_client.cert_is_registered()

    def not_ready_reason(self) -> str:
        import hub_client
        if not hub_client.cert_is_registered():
            return "Cert-Provider-Hub nicht registriert/freigegeben (Erweitert-Tab)."
        if not self._p.get("available"):
            return "Anbieter im Hub derzeit nicht verfügbar."
        return ""

    def get_portal_url(self, email: str, user_config: dict) -> str:
        # Kein CA-Portal für den Nutzer — die Erfüllung läuft über den Hub.
        return ""

    def get_instructions_html(self, email: str, days_left: int, expiry_str: str,
                              upload_url: str, user_config: dict) -> str:
        label = self._p.get("label") or self._p.get("id", "")
        return f"""
<p>Ihr S/MIME-Zertifikat für <strong>{email}</strong> läuft in {days_left} Tagen ab
({expiry_str}).</p>
<p>Die Erneuerung über <strong>{label}</strong> wurde automatisch beim
Zertifikatsdienst beauftragt — Sie müssen nichts weiter tun. Das neue Zertifikat
wird nach Ausstellung automatisch eingespielt.</p>
<p style="color:#888;font-size:12px">Falls die automatische Ausstellung
fehlschlägt, informiert das Gateway den Administrator. Manueller Upload als
Fallback: <a href="{upload_url}" style="word-break:break-all">{upload_url}</a></p>
"""

    async def initiate_renewal(self, email: str, user_config: dict) -> bool:
        import hub_client
        import hub_orders
        if not hub_client.cert_is_registered():
            raise RuntimeError(
                "Cert-Provider-Hub nicht registriert/freigegeben (Erweitert-Tab).")
        provider_id = self._p.get("id", "")
        key_pem, csr_pem = _generate_key_and_csr_pem(email)
        result = await hub_client.cert_order(email, csr_pem.decode(), {}, provider=provider_id)
        if not result.get("ok"):
            raise RuntimeError(f"Hub-Bestellung fehlgeschlagen: {result.get('error')}")

        # Synchron ausgestellt (API-Erfüllung ohne Wartezeit) → direkt importieren
        cert_pem = result.get("cert_pem")
        if cert_pem:
            import smime_store
            info = smime_store.store_pem_slot(email, cert_pem.encode(), key_pem)
            log.info("Hub-Backend %s: Zertifikat für %s direkt importiert (Slot %s)",
                     provider_id, email, info.get("slot_id"))
            return True

        # Asynchron (z.B. manuelle Erfüllung im Hub) → Schlüssel MUSS persistiert
        # werden, sonst kann das spätere Zertifikat nie gepaart werden
        order_id = result.get("order_id") or result.get("ref")
        if not order_id:
            raise RuntimeError(
                "Hub lieferte weder Zertifikat noch Order-ID — Bestellung abgebrochen, "
                "damit kein verwaister Schlüssel entsteht.")
        hub_orders.save_pending(str(order_id), email, provider_id, key_pem,
                                int(result.get("price_cents") or 0))
        log.info("Hub-Backend %s: Order %s für %s eingereicht — Zertifikat wird "
                 "nach Ausstellung automatisch abgeholt", provider_id, order_id, email)
        return True
