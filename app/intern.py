"""Ist eine Adresse intern? — EINE Definition für Versand und Add-in.

Intern ist eine Adresse, wenn sie
  1. einem Postfach dieses Tenants gehört (exo_mailboxes.known_addresses), oder
  2. auf eine akzeptierte Domäne des Tenants endet (Authoritative oder
     InternalRelay — setup_wizard.list_accepted_domains, zwischengespeichert).

Bis v1.11.55 zählte nur (1). Kontakte, Verteiler und Gruppen ohne eigenes
Postfach unter der eigenen Domäne galten als extern; eine rein interne Mail an
einen Verteiler wurde dadurch signiert wie eine Mail nach außen.

Beide Quellen werden NIE live abgefragt (heißer SMTP-Pfad), nur der
vorhandene Zwischenspeicher gelesen. Ist beides leer, ist „intern“ unbekannt:
`alle_intern` liefert dann False — im Zweifel extern, damit eine interne
Signatur nie nach außen geht.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _domaenen() -> set[str]:
    try:
        import setup_wizard
        d = setup_wizard._domaenen_von_platte() or {}
        return {x.strip().lower() for x in d.get("domaenen") or [] if x}
    except Exception as exc:                                 # noqa: BLE001
        log.debug("Domänenliste nicht lesbar: %s", exc)
        return set()


def _postfaecher() -> set[str]:
    try:
        import exo_mailboxes
        return exo_mailboxes.known_addresses()
    except Exception as exc:                                 # noqa: BLE001
        log.debug("Postfachliste nicht lesbar: %s", exc)
        return set()


def ist_intern(adresse: str, postfaecher: set[str] | None = None,
               domaenen: set[str] | None = None) -> bool:
    adr = (adresse or "").strip().lower()
    if "@" not in adr:
        return False
    postfaecher = _postfaecher() if postfaecher is None else postfaecher
    domaenen = _domaenen() if domaenen is None else domaenen
    return adr in postfaecher or adr.rsplit("@", 1)[1] in domaenen


def alle_intern(adressen) -> bool:
    """True nur, wenn es mindestens eine Adresse gibt und JEDE intern ist."""
    liste = [a for a in (adressen or []) if (a or "").strip()]
    if not liste:
        return False
    postfaecher, domaenen = _postfaecher(), _domaenen()
    if not postfaecher and not domaenen:
        return False
    return all(ist_intern(a, postfaecher, domaenen) for a in liste)
