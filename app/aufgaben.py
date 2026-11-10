"""Offene Aufgaben des Betreibers — der Sammelpunkt oben rechts.

ANLASS (2026-11-07)
-------------------
Was nach einem Update zu tun ist, stand bis dahin nur im Changelog
(„**Zu tun:**“). Wer ihn nicht las, erfuhr es nie — etwa, dass nach v1.10.80
eine erneute Anmeldung das Client Secret ablöst, oder dass die
S/MIME-Privatschlüssel ohne Passwort im Klartext liegen (das stand nur als
kleiner Hinweis auf einer Unterseite).

WIE ES GEBAUT IST
-----------------
Jede Aufgabe ist eine Prüfung aus `abnahme.py` — dieselbe Soll-Funktion, nicht
ein zweiter Nachbau. Damit verschwindet eine Aufgabe von selbst, sobald sie
erledigt ist, und es gibt keinen „Erledigt“-Knopf, der eine Lücke grün meldet.
Dazu kommt hier nur, was die Oberfläche braucht: wohin der Link führt, ob die
Aufgabe zusätzlich als Banner erscheint (`dringend`) und seit welcher Fassung
sie besteht (`seit`).

⚠️ Nur LOKALE Prüfungen gehören hierher — sie laufen bei jedem Seitenaufbau.
Prüfungen mit Netzabfrage (Exchange, Graph, die eigene Außenadresse) bleiben in
der Abnahme.

⚠️ Ein Changelog-Eintrag mit „Zu tun“ jenseits von „nichts“ braucht ab v1.10.80
eine Aufgabe mit passendem `seit` — oder eine begründete Ausnahme in
`tests/test_aufgaben.py`. Sonst stünde die Aufgabe wieder nur im Changelog.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Callable

import abnahme

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Aufgabe:
    kennung: str
    pruefung: Callable[[], dict]
    link: str
    dringend: bool = False
    seit: str = ""          # Fassung, deren Changelog-„Zu tun“ sie trägt
    # Wird die Aufgabe gerade im Hintergrund erledigt? Dann zeigt der
    # Sammelpunkt einen Fortschritt statt „offen“, und das Banner entfällt.
    laeuft: Callable[[], bool] | None = None


def _zertifikat_abbau_laeuft() -> bool:
    import setup_wizard
    return setup_wizard.abbau_laeuft()


AUFGABEN: tuple[Aufgabe, ...] = (
    Aufgabe("zertifikat-anmeldung", abnahme._anmeldenachweis,
            "/setup#step-4", dringend=True, seit="1.10.80",
            laeuft=_zertifikat_abbau_laeuft),
    Aufgabe("smime-schluessel", abnahme._smime_schluessel,
            "/settings/smime#schluesselablage", dringend=True, seit="1.10.82"),
    # Öffnet den Zustimmungsdialog aus base.html — auf jeder Seite.
    Aufgabe("vertragsbedingungen", abnahme._vertragsbedingungen,
            "#vertragsbedingungen", dringend=True),
    Aufgabe("admin-passwort", abnahme._admin_passwort, "/setup#step-1", dringend=True),
    Aufgabe("tls-zertifikat", abnahme._tls, "/setup#step-le", dringend=True),
    Aufgabe("lizenz", abnahme._fair_use, "/settings/connect#lizenz", dringend=True),
    Aufgabe("benachrichtigung", abnahme._benachrichtigung,
            "/settings#benachrichtigungen"),
    Aufgabe("signaturvorlage", abnahme._signaturvorlage, "/template"),
    Aufgabe("postfaecher", abnahme._postfaecher, "/mailboxes"),
    Aufgabe("wartende-zertifikate", abnahme._wartende_zertifikate, "/smime#wartend",
            seit="1.10.110"),
)

_TTL_S = 10
_zwischen: tuple[float, list[dict]] | None = None


def _eingerichtet() -> bool:
    """Vor der Einrichtung ist der Assistent die Aufgabenliste — sonst stünde
    auf jeder Seite „kein Zertifikat“, bevor es eins geben kann."""
    import config
    import settings_store
    return bool((config.TENANT_ID or settings_store.get("TENANT_ID"))
                and (config.CLIENT_ID or settings_store.get("CLIENT_ID")))


def _stempel(punkt: dict) -> str:
    """Kurzer Fingerabdruck des Befunds. Ein ausgeblendetes Banner kommt
    wieder, sobald sich der Befund ändert (etwa: mehr Schlüssel im Klartext)."""
    return hashlib.sha256(punkt.get("befund", "").encode()).hexdigest()[:10]


def offene() -> list[dict]:
    """Alle offenen Aufgaben, dringende zuerst. Eine gescheiterte Prüfung
    erscheint als offene Aufgabe — still verschwinden darf sie nicht."""
    global _zwischen
    jetzt = time.monotonic()
    if _zwischen and jetzt - _zwischen[0] < _TTL_S:
        return _zwischen[1]
    liste: list[dict] = []
    if _eingerichtet():
        for a in AUFGABEN:
            try:
                p = a.pruefung()
            except Exception as exc:                  # noqa: BLE001
                log.warning("Aufgabe %s: Prüfung gescheitert: %s", a.kennung, exc)
                p = {"name": a.kennung, "zustand": abnahme.UNBEKANNT,
                     "befund": f"Prüfung gescheitert: {str(exc)[:120]}", "zu_tun": ""}
            if p["zustand"] not in (abnahme.OFFEN, abnahme.UNBEKANNT):
                continue
            try:
                laeuft = bool(a.laeuft and a.laeuft())
            except Exception:                         # noqa: BLE001 — dann eben „offen“
                laeuft = False
            liste.append({"kennung": a.kennung, "titel": p["name"], "laeuft": laeuft,
                          "befund": p.get("befund", ""), "zu_tun": p.get("zu_tun", ""),
                          "link": a.link, "dringend": a.dringend, "seit": a.seit,
                          "stempel": _stempel(p)})
    liste.sort(key=lambda x: not x["dringend"])
    _zwischen = (jetzt, liste)
    return liste


def vergessen() -> None:
    """Zwischenspeicher leeren — nach einer Änderung, die eine Aufgabe erledigt."""
    global _zwischen
    _zwischen = None
