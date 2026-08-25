"""Breite Tabellen müssen auf dem Telefon bedienbar bleiben.

ANLASS (2026-08-25)
-------------------
Der Nutzer: *„die Tabelle ist nicht scrollbar ich komme auf dem Handy kaum an
die Buttons rechts wenn der Modus aktiv ist"* — die Warteschlange des
Wartungsmodus. Bei 393px gemessen: Der Löschen-Knopf lag bei x=896, das Fenster
war 393 breit, und nichts liess sich schieben.

Die Klasse `.tabellen-rollbereich` existierte da bereits — benutzt haben sie
GENAU ZWEI Vorlagen, beide kurz zuvor gebaut. Neun ältere Tabellen standen
daneben. Genau der Befund, für den CLAUDE.md eine Struktur verlangt statt eines
Vermerks im Changelog.

Mobile Kompatibilität (≤393px) gehört zur Fertigdefinition wie der Dunkelmodus.
Beides hält nur, weil ein Skript es prüft.
"""
import re
import subprocess
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
SKRIPT = WURZEL / "tools" / "tabellencheck.py"
VORLAGEN = WURZEL / "app" / "webui" / "templates"

import re as _re

STYLE = (WURZEL / "app" / "webui" / "static" / "style.css").read_text("utf-8")
# CSS ohne Kommentare — sonst findet ein Test seine Eigenschaft im erklärenden
# Text darüber und bleibt grün, obwohl die Regel fehlt.
STYLE_PUR = _re.sub(r"/\*.*?\*/", " ", STYLE, flags=_re.S)


def test_bestand_ist_sauber():
    r = subprocess.run([sys.executable, str(SKRIPT)], cwd=str(WURZEL),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_der_rollbereich_rollt_wirklich():
    """Auf die WIRKUNG prüfen, nicht auf das Vorkommen des Klassennamens."""
    regel = re.search(r"\.tabellen-rollbereich\s*\{([^}]*)\}", STYLE_PUR)
    assert regel, "Die Klasse .tabellen-rollbereich hat keine Regel."
    assert re.search(r"overflow-x:\s*auto", regel.group(1)), (
        "Der Rollbereich rollt nicht waagerecht — dann ist er nur ein <div>.")


def test_wartungsschlange_ist_erreichbar():
    """Der gemeldete Fall, als Stellvertreter für die Verdrahtung.

    Beides zusammen ist nötig: Ohne Bereich läuft die Aktionsspalte aus dem
    Bild, ohne Mindestbreite quetscht `width:100%` fünf Spalten in 393px und
    aus Adressen und Betreff werden Ellipsen.
    """
    q = (VORLAGEN / "advanced.html").read_text("utf-8")
    # ⚠️ `container.innerHTML` steht zweimal: einmal für den leeren Fall
    # („Keine zurückgehaltenen Mails"), einmal für die Tabelle. Der erste
    # Treffer ist der falsche — ein Anker, der die falsche Stelle trifft,
    # meldet einen Fehler, den es nicht gibt.
    stelle = q[q.index("container.innerHTML = '<div"):][:400]
    assert "tabellen-rollbereich" in stelle, (
        "Die Warteschlange steht nicht in einem Rollbereich.")
    assert re.search(r"min-width:\s*\d+px", stelle), (
        "Der Warteschlange fehlt die Mindestbreite — sie quetscht sich dann "
        "zusammen, statt zu rollen.")


def test_schmale_tabellen_werden_nicht_beanstandet():
    """Gegenrichtung: Ein Werkzeug, das Richtiges anmahnt, wird weggedrückt.

    Zwei Spalten ohne Knöpfe (Schlüssel/Wert-Aufstellungen) passen auf 393px.
    Ein Rollbalken, der nie gebraucht wird, ist kein Fortschritt.
    """
    sys.path.insert(0, str(WURZEL / "tools"))
    import tabellencheck
    beispiel = ('<table class="kv-table"><tr><th>Postfächer</th>'
                '<td>12</td></tr></table>')
    assert "kv-table" in (VORLAGEN / "settings_connect.html").read_text("utf-8"), (
        "Beispiel nicht mehr im Bestand — der Test prüft ins Leere.")
    assert tabellencheck.pruefe(WURZEL) == [], (
        "Der Prüfer beanstandet eine Tabelle, die auf das Telefon passt.")


def test_pruefung_laeuft_in_der_ci():
    ci = (WURZEL / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    assert "tools/tabellencheck.py" in ci, (
        "Ein Prüfskript, das nur von Hand läuft, läuft irgendwann nicht mehr.")
