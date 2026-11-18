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


# ── Hybrid-Nutzer: Abruf statt Get-App (seit v1.12.69) ────────────────────────
#
# Für Hybrid-Nutzer stellt der Betreiber die Add-ins in Exchange LOKAL bereit
# (New-App in der Exchange-Verwaltungsshell) — das Microsoft 365 Admin Center
# kann es nicht („Add-in deployment to an Exchange on-premises Mailbox“ steht
# dort unter „nicht unterstützt“). Get-App, mit dem die Postfachprüfung die
# Fassung liest, gibt es nur in Exchange Online; die lokale Verwaltungsshell
# erreicht das Gateway nicht. Der einzige Beleg, dass die Bereitstellung wirkt,
# ist deshalb ein Abruf aus Outlook: Das Verfassen-Add-in holt die Signatur,
# der Knopf ebenso. Gemerkt wird je Postfach-Eintrag und Add-in der Tag des
# letzten Abrufs — höchstens ein Schreibvorgang je Tag und Add-in.

LOKAL_KEY = "_ADDIN_LOKAL"


def _eintrag_schluessel(adresse: str) -> str:
    import mailbox_match
    a = (adresse or "").strip().lower()
    return mailbox_match.match_sender_key(settings_store.get("MAILBOX_CONFIG") or {}, a) or a


def lokal_gesehen(adresse: str, art: str, client: str = "", jetzt: datetime | None = None) -> None:
    """Abruf des Add-ins `art` (verfassen|knopf) durch den Hybrid-Nutzer `adresse`.
    Nur für Hybrid-Nutzer aufrufen (exo_mailboxes.ist_hybrid)."""
    schluessel = _eintrag_schluessel(adresse)
    if not schluessel:
        return
    tag = (jetzt or _jetzt()).strftime("%Y-%m-%d")
    stand = settings_store.get(LOKAL_KEY) or {}
    alt = ((stand.get(schluessel) or {}).get(art) or {}) if isinstance(stand, dict) else {}
    if alt.get("tag") == tag:
        return
    neu = dict(stand) if isinstance(stand, dict) else {}
    eintrag = dict(neu.get(schluessel) or {})
    eintrag[art] = {"tag": tag, "client": (client or "")[:20]}
    neu[schluessel] = eintrag
    settings_store.force_update({LOKAL_KEY: neu})


def lokal_stand(adresse: str, art: str) -> dict:
    """{tag, client} des letzten Abrufs — {} wenn nie gesehen."""
    stand = settings_store.get(LOKAL_KEY) or {}
    if not isinstance(stand, dict):
        return {}
    return dict((stand.get(_eintrag_schluessel(adresse)) or {}).get(art) or {})
