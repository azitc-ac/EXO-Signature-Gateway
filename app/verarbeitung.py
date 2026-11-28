"""Verarbeitung eines Postfachs: vorbereitet, bereit, aktiv — die EINE Quelle.

WARUM ES DIE STUFE „VERARBEITUNG“ GIBT
--------------------------------------
Exchange erfasst ein neues Mitglied der Verteilerliste in der FromMemberOf-Regel
erst nach bis zu ~3 Stunden (gemessen 17.11.2026, `webui/routen/mailboxes.py`
`REGEL_VERZOEGERUNG`), und eine neue Zugriffsfreigabe (ApplicationAccessPolicy /
RBAC for Applications) wirkt ebenfalls verzögert. Wer Signatur oder S/MIME
einschaltet, wusste bisher nicht, ab wann es greift.

Die Exchange-Seite (langsam) ist deshalb von der Gateway-Seite (sofort)
getrennt:

* **verarbeitet** — Haken „Verarbeitung“: Das Postfach steht in der
  Signatur-Verteilerliste (Routing zum Gateway) und in der App-Scope-Gruppe
  (Zugriffsrecht). Das Gateway gibt seine Post unverändert weiter, solange
  weder Signatur noch S/MIME an ist (handler.py: „not in active MAILBOX_CONFIG“).
* **bereit** — die Exchange-Seite ist belegt: Eine Regelprobe (regel_probe.py)
  kam am Gateway an. Das belegt beides zugleich — die Probe geht per Graph
  `sendMail` ALS das Postfach (Zugriffsrecht wirkt) und erreicht das Gateway
  nur über die Transportregel (Regel greift).
* **aktiv** — Signatur oder S/MIME an. Einschalten lässt sich das erst, wenn
  das Postfach bereit ist (durchgesetzt beim Speichern, `mailboxes.py`).

Bestand: Einträge mit Signatur oder S/MIME von vor dieser Stufe gelten als
verarbeitet UND bereit — sie laufen ja schon. Wer danach nur Signatur/S/MIME
abschaltet und die Verarbeitung behält, bleibt bereit (Feld `bereit`, beim
Speichern übernommen); erst wer die Verarbeitung abschaltet, verlässt die
Verteilerliste und muss beim nächsten Mal wieder warten.

Hybrid-Nutzer ohne mögliche Probe (regel_probe.ENTFAELLT): Dort gilt das
Postfach nach Ablauf von REGEL_VERZOEGERUNG als bereit — einen Beleg gibt es
nicht, und ewig gesperrt wäre schlechter.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Gleiche Grösse wie webui/routen/mailboxes.REGEL_VERZOEGERUNG (dort für die
# Anzeige „wirkt in bis zu 3 h“). Hier eigen, damit dieses Modul ohne die
# Weboberfläche importierbar bleibt; tests/test_verarbeitung.py hält beide gleich.
REGEL_VERZOEGERUNG = timedelta(hours=3)


def aktiv(eintrag) -> bool:
    """Signatur oder S/MIME an — das Gateway bearbeitet die Post."""
    return isinstance(eintrag, dict) and bool(eintrag.get("sig") or eintrag.get("smime"))


def verarbeitet(eintrag) -> bool:
    """In Verteilerliste und App-Scope-Gruppe: Haken „Verarbeitung“ ODER aktiv
    (Bestand und alte Seiten ohne das Feld)."""
    return isinstance(eintrag, dict) and bool(eintrag.get("verarbeiten") or aktiv(eintrag))


def _zeit(wert) -> datetime | None:
    if not isinstance(wert, str) or not wert:
        return None
    try:
        t = datetime.fromisoformat(wert.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def bereit(eintrag, gesundheit: dict | None, jetzt: datetime | None = None) -> bool:
    """Darf Signatur/S/MIME eingeschaltet werden? Siehe Moduldoku."""
    if not verarbeitet(eintrag):
        return False
    if aktiv(eintrag) or eintrag.get("bereit") is True:
        return True
    import regel_probe
    if regel_probe.greift(gesundheit, eintrag):
        return True
    probe = (gesundheit or {}).get("regel_probe") or {}
    if probe.get("ergebnis") == "nicht_moeglich" and regel_probe.aktuell(probe, eintrag):
        seit = _zeit(eintrag.get("aktiviert_seit"))
        jetzt = jetzt or datetime.now(timezone.utc)
        return seit is None or jetzt - seit >= REGEL_VERZOEGERUNG
    return False


def offen(mailbox_config: dict | None, gesundheit_alle: dict | None) -> list[str]:
    """Adressen verarbeiteter, noch nicht bereiter Postfächer — für die
    automatische Regelprobe (scheduler._vorbereitung_pruefen)."""
    aus: list[str] = []
    for key, e in (mailbox_config or {}).items():
        if not verarbeitet(e):
            continue
        adresse = (e.get("primary") or (key if "@" in key else "")).strip().lower()
        if not adresse:
            continue
        if not bereit(e, (gesundheit_alle or {}).get(adresse)):
            aus.append(adresse)
    return sorted(set(aus))
