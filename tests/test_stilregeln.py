"""Stilregeln, die still brechen können.

ANLASS (07.08.2026)
Die feste Kopfzeile der Postfachtabelle hing an `min-width: 1100px`. Ein
verkleinertes Browserfenster am Rechner fiel damit in die Telefon-Behandlung —
schmal, aber mit Maus, und dort ist der waagerechte Rollbalken genauso schwer
zu erreichen. Gemeldet als „die Titelleiste scrollt raus".

Die eigentliche Falle steckt aber tiefer und ist die, gegen die hier geprüft
wird: `position: sticky` bezieht sich auf den nächsten Rollbereich. Der Wrapper
IST einer, weil er `overflow-x: auto` trägt. Ohne eigene Höhe rollt er senkrecht
nicht — dann klebt dort nichts, ganz gleich wie richtig die `sticky`-Regel
aussieht. Trennt jemand später `max-height` und `position: sticky` in
verschiedene Media-Blöcke, ist die Kopfzeile lautlos wieder weg.

Das ist eine Textprüfung und kein Ersatz für den Blick in den Browser. Sie
fängt genau den Rückbau, der niemandem auffällt: Im Browser gemessen wurde mit
Playwright (nicht in der CI verfügbar) — bei 900x700 rutschte die Kopfzeile mit
der alten Regel auf y=-39, mit der neuen bleibt sie stehen.
"""
import re
from pathlib import Path

import pytest

STIL = Path(__file__).resolve().parent.parent / "app" / "webui" / "static" / "style.css"


@pytest.fixture(scope="module")
def css() -> str:
    return STIL.read_text(encoding="utf-8")


def _block(css: str, bedingung: str) -> str:
    """Inhalt des @media-Blocks mit dieser Bedingung."""
    i = css.index("@media " + bedingung)
    tiefe, start = 0, css.index("{", i)
    for j in range(start, len(css)):
        if css[j] == "{":
            tiefe += 1
        elif css[j] == "}":
            tiefe -= 1
            if tiefe == 0:
                return css[start:j]
    raise AssertionError("Block nicht geschlossen: " + bedingung)


def test_kopfzeile_und_ausschnitt_stehen_im_selben_block(css):
    """Getrennt wirkt die klebende Kopfzeile nicht — siehe Modulkopf."""
    block = _block(css, "(pointer: fine), (min-width: 1100px)")
    assert "max-height" in block, "der eigene Ausschnitt fehlt"
    assert "overflow: auto" in block, "ohne senkrechtes Rollen klebt nichts"
    assert "position: sticky" in block, "die Kopfzeile klebt nicht"
    assert ".tabellen-rollbereich thead th" in block


def test_tabelle_haengt_nicht_allein_an_der_fensterbreite(css):
    """Ein verkleinertes Fenster am Rechner ist schmal, aber kein Telefon."""
    stelle = css.index(".tabellen-rollbereich thead th")
    davor = css[:stelle]
    bedingung = davor[davor.rindex("@media"):].splitlines()[0]
    assert "pointer: fine" in bedingung, \
        f"Tabelle wieder allein an die Breite gebunden: {bedingung.strip()}"


def test_vorschau_klebt_ab_der_gemessenen_umbruchschwelle(css):
    """Die beiden Baukasten-Spalten stehen bis 760px nebeneinander (im Browser
    nachgemessen). Eine höhere Grenze liesse die Vorschau davonrollen, obwohl
    daneben Platz ist — genau der Fehler, der hier behoben wurde."""
    stelle = css.index(".baukasten-vorschau")
    davor = css[:stelle]
    bedingung = davor[davor.rindex("@media"):].splitlines()[0]
    treffer = re.findall(r"min-width:\s*(\d+)px", bedingung)
    assert treffer, f"keine Breitengrenze gefunden: {bedingung.strip()}"
    assert min(int(t) for t in treffer) <= 760, \
        f"Vorschau klebt erst ab {min(treffer)}px, die Spalten stehen ab 760px nebeneinander"


def test_fliesstext_bleibt_schmal(css):
    """`main.weit` gilt nur für Seiten, die es anfordern. Würde die Breite auf
    `main` selbst gehoben, liefen Einstellungs- und Rechtstexte über die volle
    Bildschirmbreite."""
    assert re.search(r"^main\s*\{[^}]*max-width:\s*960px", css, re.M), \
        "die Grundbreite von 960px wurde verändert"
    assert "main.weit" in css
