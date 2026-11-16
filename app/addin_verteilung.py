"""Verteilung einer neuen Add-in-Fassung: Handlungsbedarf oder nur Warten?

ANLASS (2026-11-15)
-------------------
Stand ein Postfach noch auf der alten Fassung des Verfassen-Add-ins, meldeten
Postfachprüfung und Aufgabe „Admin Center → Integrierte Apps → Aktualisieren“ —
auch dann, als das längst geschehen war und Exchange die neue Fassung nur noch
Postfach für Postfach verteilte (gemessen: 6 von 9 Postfächern aktuell, 2 auf
der alten Fassung, 1 ohne Installation). Der Betreiber konnte nichts tun und
bekam trotzdem eine Handlungsanweisung.

DIE EINE SOLL-FUNKTION (`einordnen`)
------------------------------------
Für Postfachprüfung (health_check) UND Abnahme/Aufgabe (abnahme) — damit beide
nicht auseinanderlaufen:

* AKTUELL         — das Postfach hat die Soll-Fassung (oder eine neuere).
* AKTUALISIEREN   — noch KEIN Postfach wurde je auf der Soll-Fassung gesehen:
                    Das Manifest-Update im Admin Center steht aus.
* VERTEILUNG      — mindestens ein Postfach hat sie schon, dieses noch nicht,
                    und seit dem ersten Sichten sind < 72 h vergangen. Kein
                    Handlungsbedarf: Exchange verteilt (laut Microsoft bis 72 h).
* ZUWEISUNG       — 72 h nach dem ersten Sichten noch nicht da: vermutlich ist
                    das Postfach nicht in der Zuweisung (Add-ins an alle).
* NICHT_INSTALLIERT — vor Ablauf des Fensters neutral (Teilzuweisung möglich,
                    oder die Verteilung kommt noch); danach wie ZUWEISUNG.

Den Zeitpunkt der Aktualisierung im Admin Center sieht das Gateway nicht. Die
Uhr startet deshalb beim frühesten Prüflauf, der ein Postfach auf der
Soll-Fassung sah (`_ADDIN_VERTEILUNG`, je Add-in und Soll-Fassung; eine neue
Soll-Fassung beginnt neu).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import settings_store

KEY = "_ADDIN_VERTEILUNG"
FENSTER = timedelta(hours=72)

AKTUELL = "aktuell"
AKTUALISIEREN = "aktualisieren"
VERTEILUNG = "verteilung"
ZUWEISUNG = "zuweisung"
NICHT_INSTALLIERT = "nicht_installiert"


def _tupel(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(t) for t in str(v).split("."))
    except ValueError:
        return (0,)


def _jetzt() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def ist_aktuell(ist: str, soll: str) -> bool:
    return bool((ist or "").strip()) and _tupel(ist) >= _tupel(soll)


def erstmals(art: str, soll: str) -> datetime | None:
    """Frühester Prüflauf, der ein Postfach auf `soll` sah — None, wenn noch nie
    (oder nur für eine frühere Soll-Fassung)."""
    e = (settings_store.get(KEY) or {}).get(art) or {}
    if e.get("version") != soll or not e.get("erstmals"):
        return None
    try:
        return datetime.strptime(e["erstmals"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def gesehen(art: str, soll: str, ist: str, jetzt: datetime | None = None) -> None:
    """Ein Prüflauf sah ein Postfach mit Fassung `ist`. Ist es die Soll-Fassung
    und die Uhr läuft für sie noch nicht, startet sie jetzt."""
    if not ist_aktuell(ist, soll) or erstmals(art, soll) is not None:
        return
    stand = dict(settings_store.get(KEY) or {})
    stand[art] = {"version": soll, "erstmals": _iso(jetzt or _jetzt())}
    settings_store.force_update({KEY: stand})


def einordnen(art: str, soll: str, ist: str, jetzt: datetime | None = None) -> str:
    """Lage EINES Postfachs (siehe Modulkopf)."""
    if ist_aktuell(ist, soll):
        return AKTUELL
    beginn = erstmals(art, soll)
    if beginn is None:
        return NICHT_INSTALLIERT if not (ist or "").strip() else AKTUALISIEREN
    if (jetzt or _jetzt()) - beginn < FENSTER:
        return NICHT_INSTALLIERT if not (ist or "").strip() else VERTEILUNG
    return ZUWEISUNG


ZUWEISUNG_WEG = ("Admin Center → Einstellungen → Integrierte Apps → Add-in → Benutzer: "
                 "an alle (Jeder) zuweisen. Seit über 72 Stunden hat Exchange die neue "
                 "Fassung diesem Postfach nicht ausgeliefert.")
AKTUALISIEREN_WEG = ("Einrichtung → Verfassen-Add-in → „Integrierte Apps öffnen“ → Add-in → "
                     "Aktualisieren und die neuen Berechtigungen bestätigen. Exchange "
                     "verteilt die neue Fassung danach binnen bis zu 72 Stunden.")
