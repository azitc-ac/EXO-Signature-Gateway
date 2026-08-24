"""Endpunkte ohne Bedienelement fallen auf — und Fehlalarme bleiben aus.

ANLASS (24.08.2026)
-------------------
Rückwärtsrichtung des Auftrags „nichts soll unter der Oberfläche schlummern":
`jsscopecheck.js` findet Bedienelemente ohne Funktion, für die Gegenrichtung gab
es nichts. Ein Endpunkt sieht durch seinen Dekorator immer benutzt aus, und
`deadcheck.py` prüft ausdrücklich nur JavaScript.

Erster Lauf: vier tote Endpunkte, darunter `/api/system/update/whats-new` mit
einer zweiten, abweichenden Changelog-Auswertung neben der tatsächlich
benutzten aus `update_core.py`.

⚠️ Der zweite Test ist der wichtigere. Aufrufe werden zur Laufzeit
zusammengesetzt (`fetch('/api/setup/verify/' + type)`), und beim ersten Entwurf
waren **vier von zehn Funden** genau das — Fehlalarm. Ein Werkzeug, dessen
Meldungen man wegdrückt, hört man auch dann nicht mehr, wenn es recht hat.
"""
import subprocess
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
SKRIPT = WURZEL / "tools" / "routencheck.py"


def _lauf(cwd=None):
    return subprocess.run([sys.executable, str(SKRIPT)],
                          cwd=str(cwd or WURZEL),
                          capture_output=True, text=True)


def test_bestand_ist_sauber():
    r = _lauf()
    assert r.returncode == 0, (
        "Es gibt einen API-Endpunkt, den die Oberfläche nicht aufruft:\n"
        + r.stdout + r.stderr)


def test_zusammengesetzte_aufrufe_gelten_als_aufruf(tmp_path):
    """`fetch('/api/x/y/' + z)` ist ein Aufruf, auch wenn der volle Pfad nirgends steht."""
    sys.path.insert(0, str(WURZEL / "tools"))
    import routencheck

    text = "const r = await fetch('/api/setup/verify/' + type);"
    art, _ = routencheck.einordnen("/api/setup/verify/azure", text)
    assert art == "ok", (
        "Ein zur Laufzeit zusammengesetzter Aufruf darf nicht als Fund gelten — "
        "das war beim ersten Entwurf bei vier von zehn Meldungen der Fall.")

    # Auch die Template-Literal-Schreibweise
    art, _ = routencheck.einordnen(
        "/api/smime/keyvault/migrate/{email}",
        "fetch(`/api/smime/keyvault/migrate/${encodeURIComponent(email)}`)")
    assert art == "ok"


def test_echter_fund_wird_gemeldet():
    sys.path.insert(0, str(WURZEL / "tools"))
    import routencheck
    art, _ = routencheck.einordnen("/api/voellig/erfunden", "nichts davon hier")
    assert art == "fund"


def test_bedientes_geschwister_entlastet_nicht():
    """`/api/logs/search` beweist nichts über `/api/logs/files`.

    Der erste Entwurf liess einen blossen Präfixtreffer als „zu prüfen" durch,
    ohne den Rückgabewert zu setzen. Die Gegenprobe zeigte, was das wert war:
    Ein wieder eingebautes, totes `/api/logs/files` blieb unbeanstandet, weil
    daneben `/api/logs/search` bedient wird — genau der häufige Fall wäre
    durchgerutscht. Entlastend ist ein Präfix nur mit Zusammensetzung.
    """
    sys.path.insert(0, str(WURZEL / "tools"))
    import routencheck
    art, praefix = routencheck.einordnen(
        "/api/logs/files", "await getJSON('/api/logs/search?q=' + q)")
    assert art == "fund", "ein bedientes Geschwister ist kein Beleg"
    assert praefix == "/api/logs", "das gefundene Präfix gehört in die Meldung"


def test_ausnahmen_sind_begruendet():
    sys.path.insert(0, str(WURZEL / "tools"))
    import routencheck
    for route, grund in routencheck.ACCEPTED.items():
        assert len(grund) > 20, (
            f"{route} steht ohne tragfähige Begründung in ACCEPTED — dann ist es "
            "keine Ausnahme, sondern ein vergessener Rest.")


def test_pruefung_laeuft_in_der_ci():
    ci = (WURZEL / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    assert "tools/routencheck.py" in ci, (
        "Ein Prüfskript, das nur von Hand läuft, läuft irgendwann nicht mehr.")
