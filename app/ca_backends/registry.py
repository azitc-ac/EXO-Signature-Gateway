"""Backend-Registry — statische lokale Backends + dynamische Hub-Anbieter.

Lokal bleiben nur Backends, die zwingend Postfach-/Gateway-Zugriff brauchen:
  - castle_acme (ACME email-reply-00 pollt die Mailbox — geht nicht im Hub)
  - assisted_manual (Anleitung + manueller Upload)

Alle kommerziellen CA-Anbieter (Sectigo, SwissSign, künftige) kommen dynamisch
aus dem Hub-Katalog (hub_catalog) als "hub:<id>"-Backends. Anbieter-Wegfall,
Preisänderung oder neue Anbieter sind damit reine Hub-Konfiguration.
"""
from .assisted_manual import AssistedManualBackend
from .castle_acme import CastleAcmeBackend
from .hub_provider import HubProviderBackend
from .base import CABackend

_STATIC: dict[str, CABackend] = {
    "assisted_manual": AssistedManualBackend(),
    "castle_acme": CastleAcmeBackend(),
}

# Historische Backend-Namen (Direktanbindung im Gateway, nie produktiv) →
# gleicher Anbieter über den Hub. Bestehende CA_USER_CONFIG-Einträge
# funktionieren dadurch weiter.
_LEGACY_ALIAS = {
    "sectigo": "hub:sectigo",
    "swisssign": "hub:swisssign",
}


def _hub_backend(provider_id: str) -> HubProviderBackend:
    import hub_catalog
    prov = hub_catalog.get(provider_id)
    if prov is None:
        # Katalog (noch) nicht geladen oder Anbieter entfallen — Stub, der im
        # UI als "nicht bereit" erscheint und bei initiate_renewal sauber wirft
        prov = {"id": provider_id, "label": provider_id, "available": False}
    return HubProviderBackend(prov)


def get_backend(name: str) -> CABackend:
    name = _LEGACY_ALIAS.get(name, name or "")
    if name.startswith("hub:"):
        return _hub_backend(name[4:])
    return _STATIC.get(name) or _STATIC["assisted_manual"]


def list_backends() -> list[dict]:
    import hub_catalog
    out = [
        {"name": b.get_name(), "label": b.get_label(), "auto": b.can_auto_renew(),
         "ready": b.is_ready(), "not_ready_reason": b.not_ready_reason(),
         "hub": False}
        for b in _STATIC.values()
    ]
    for p in hub_catalog.enabled():   # lokal abgewählte Anbieter erscheinen nicht pro Postfach
        b = HubProviderBackend(p)
        out.append({
            "name": b.get_name(), "label": b.get_label(), "auto": True,
            "ready": b.is_ready(), "not_ready_reason": b.not_ready_reason(),
            "hub": True,
            "description": p.get("description", ""),
            "price_cents": p.get("price_cents"),
            "currency": hub_catalog.currency(),
            "validity_months": p.get("validity_months"),
        })
    return out
