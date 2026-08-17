"""Backend-Registry — statische lokale Backends + dynamische Hub-Anbieter.

Lokal: Backends, die zwingend Postfach-/Gateway-Zugriff brauchen
(castle_acme pollt die Mailbox, assisted_manual = Anleitung+Upload) — sowie
seit 2026-07-15 die DigiCert-DIREKTANBINDUNG als bewusste Ausnahme zur
Hub-only-Regel: Kunden mit eigenem CertCentral-Konto sollen die Wahl haben
(transparent neben dem Hub-Angebot; wer es einfach will, nimmt hub:digicert).

Kommerzielle CA-Anbieter über den Betreiber (Sectigo, SwissSign, DigiCert-
managed) kommen dynamisch aus dem Hub-Katalog als "hub:<id>"-Backends.
"""
from .assisted_manual import AssistedManualBackend
from .castle_acme import CastleAcmeBackend, CastleAcmeStagingBackend
from .digicert_direct import DigiCertDirectBackend
from .hub_provider import HubProviderBackend
from .base import CABackend

_STATIC: dict[str, CABackend] = {
    "assisted_manual": AssistedManualBackend(),
    "castle_acme": CastleAcmeBackend(),
    "castle_acme_staging": CastleAcmeStagingBackend(),
    "digicert_direct": DigiCertDirectBackend(),
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
            "terms_url": p.get("terms_url", ""),
            # Weiterführende Unterlagen der Zertifizierungsstelle. Getrennt von
            # `terms_url`, weil der Bezieher genau EINEM Dokument zustimmt —
            # alles andere ist Beleg und Hintergrund.
            "docs": p.get("docs") or [],
        })
    return out


def migriere_staging_flag() -> int:
    """`staging: true` → Bezugsweg `castle_acme_staging`. Gibt die Anzahl zurück.

    Bis 18.08.2026 war die Testumgebung ein Ankreuzfeld NEBEN der Auswahl des
    Bezugswegs. Das hatte zwei Nachteile, die beide erst auffallen, wenn es zu
    spät ist:

    * Die Wahl „echte oder unechte Zertifikate" stand nicht in derselben Liste
      wie alle anderen Wege, obwohl sie genau das ist — die Wahl des Wegs.
    * Das Feld blieb gesetzt, wenn jemand auf einen anderen Bezugsweg wechselte.
      Dort war es unsichtbar, aber vorhanden. Wer später zu CASTLE zurückkehrte,
      bekam ohne weiteres Zutun wieder Testzertifikate.

    Idempotent: läuft bei jedem Start, ändert nur Einträge, die noch auf
    `castle_acme` mit gesetztem Flag stehen. Das Flag selbst bleibt stehen —
    `CastleAcmeBackend.ist_testumgebung()` liest es weiterhin, damit eine
    Konfiguration, die diese Umstellung aus irgendeinem Grund nicht durchläuft,
    nicht plötzlich echte Zertifikate bestellt.
    """
    import settings_store
    cfg = settings_store.get("CA_USER_CONFIG") or {}
    geaendert = {}
    for email, eintrag in cfg.items():
        if not isinstance(eintrag, dict):
            continue
        if eintrag.get("backend") == "castle_acme" and eintrag.get("staging"):
            geaendert[email] = {**eintrag, "backend": "castle_acme_staging"}
    if geaendert:
        settings_store.update({"CA_USER_CONFIG": {**cfg, **geaendert}})
    return len(geaendert)
