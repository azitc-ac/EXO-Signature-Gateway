"""Routen-Bestandsaufnahme — Sicherheitsnetz für das Aufteilen von app.py.

WOFÜR
-----
`app/webui/app.py` hat 4945 Zeilen und keinerlei Testabdeckung. Wer die Datei
aufteilt, kann eine Route verlieren, doppelt registrieren oder ihre Methoden
verändern, ohne dass es auffällt — der Fehler zeigt sich erst, wenn ein Nutzer
auf eine Schaltfläche drückt und nichts passiert.

Diese Datei hält deshalb die vollständige Routentabelle fest. Sie prüft KEIN
Verhalten (das können Unittests der einzelnen Funktionen besser), sondern nur:
*es ist danach dieselbe Oberfläche wie davor*. Genau die Frage, die bei einem
reinen Umsortieren zählt.

WENN DIESER TEST FEHLSCHLÄGT
----------------------------
Entweder wurde beim Umbau etwas verloren — dann ist es ein Fehler. Oder eine
Route wurde absichtlich hinzugefügt oder entfernt — dann die Momentaufnahme
bewusst neu erzeugen:

    python3 tests/test_routes.py --snapshot

Nie blind neu erzeugen: der Sinn ist, dass die Änderung durch die Hand geht.
"""
import json
import sys
from pathlib import Path

import pytest

SNAPSHOT = Path(__file__).parent / "routes_snapshot.json"


def aktuelle_routen() -> list[dict]:
    """Alle Routen als vergleichbare, sortierte Liste."""
    from webui.app import app
    routen = []
    for r in app.routes:
        pfad = getattr(r, "path", None)
        if pfad is None:
            continue
        methoden = sorted(getattr(r, "methods", None) or [])
        # HEAD wird von Starlette automatisch zu GET ergaenzt und sagt nichts
        # ueber die Anwendung aus.
        methoden = [m for m in methoden if m != "HEAD"]
        routen.append({
            "pfad": pfad,
            "methoden": methoden,
            "name": getattr(r, "name", "") or "",
        })
    return sorted(routen, key=lambda r: (r["pfad"], ",".join(r["methoden"])))


def test_momentaufnahme_existiert():
    assert SNAPSHOT.is_file(), (
        "Routen-Momentaufnahme fehlt — mit "
        "`python3 tests/test_routes.py --snapshot` erzeugen")


def test_routentabelle_unveraendert():
    erwartet = json.loads(SNAPSHOT.read_text())
    ist = aktuelle_routen()

    e_pfade = {(r["pfad"], ",".join(r["methoden"])) for r in erwartet}
    i_pfade = {(r["pfad"], ",".join(r["methoden"])) for r in ist}

    verloren = sorted(e_pfade - i_pfade)
    neu = sorted(i_pfade - e_pfade)

    meldung = []
    if verloren:
        meldung.append("VERLOREN (waren da, sind weg):\n  "
                       + "\n  ".join(f"{m} {p}" for p, m in verloren))
    if neu:
        meldung.append("NEU (sind da, waren nicht):\n  "
                       + "\n  ".join(f"{m} {p}" for p, m in neu))
    assert not meldung, (
        "\n\n" + "\n\n".join(meldung)
        + "\n\nWar das Absicht? Dann die Momentaufnahme bewusst neu erzeugen:"
          "\n  python3 tests/test_routes.py --snapshot\n")


def test_keine_doppelten_routen():
    """Zwei Registrierungen desselben Pfads mit derselben Methode: die zweite
    ist tot. Beim Verschieben von Endpunkten zwischen Dateien leicht passiert."""
    gesehen: dict[tuple, str] = {}
    doppelt = []
    for r in aktuelle_routen():
        for m in r["methoden"]:
            schluessel = (r["pfad"], m)
            if schluessel in gesehen:
                doppelt.append(f"{m} {r['pfad']}  ({gesehen[schluessel]} / {r['name']})")
            gesehen[schluessel] = r["name"]
    assert not doppelt, "doppelt registrierte Routen:\n  " + "\n  ".join(doppelt)


def test_alle_api_routen_haben_einen_namen():
    """Namenlose Endpunkte sind ein Zeichen dafür, dass ein Dekorator beim
    Verschieben verloren ging."""
    ohne = [r["pfad"] for r in aktuelle_routen()
            if r["pfad"].startswith("/api/") and not r["name"]]
    assert not ohne, f"Routen ohne Namen: {ohne}"


if __name__ == "__main__":
    if "--snapshot" in sys.argv:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
        routen = aktuelle_routen()
        SNAPSHOT.write_text(json.dumps(routen, indent=2, ensure_ascii=False) + "\n")
        print(f"Momentaufnahme geschrieben: {len(routen)} Routen → {SNAPSHOT}")
    else:
        print(__doc__)
