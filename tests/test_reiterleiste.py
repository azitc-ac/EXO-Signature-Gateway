"""Die Reiterleiste rollt — und man sieht es.

ANLASS (2026-08-25)
-------------------
Der Nutzer auf Windows/Edge: „der Menüpunkt Einrichtung ist rechts
abgeschnitten bei den anderen Seiten, nicht bei SMTP-Relay."

Gemessen: Die Leiste braucht mit acht Reitern 1095px, sichtbar sind 912px.
⚠️ Mit SIEBEN Reitern fehlten bereits 50px — der Relay-Reiter hat den Befund
vergrössert, nicht verursacht. Auf der Relay-Seite fiel es nicht auf, weil die
`weit` gesetzt hatte und damit als einzige Einstellungsseite 1600px breit war.

Zwei Dinge sind seither anders: Die Relay-Seite ist so breit wie ihre
Geschwister, und die Leiste zeigt mit einem Verlauf an, dass es seitlich
weitergeht (`data-rollen`, gesetzt in base.html, gezeichnet in style.css).

WAS DIESER TEST PRÜFT — und was nicht
--------------------------------------
Er prüft die Klasse Fehler, die ohne Browser sichtbar ist: dass JavaScript und
CSS über dieselben Zustände reden. Setzt das Skript einen vierten Wert, den
keine Regel kennt, bleibt der Verlauf einfach aus — kein Fehler, keine Meldung,
nur eine Leiste, die wieder wie abgeschnitten aussieht.

Er prüft NICHT, ob der Verlauf gut aussieht. Das ist im Browser gemessen
worden und lässt sich hier nicht nachstellen.
"""
import re
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
VORLAGEN = WURZEL / "app" / "webui" / "templates"
STATIK = WURZEL / "app" / "webui" / "static"

BASE = (VORLAGEN / "base.html").read_text("utf-8")
STYLE = (STATIK / "style.css").read_text("utf-8")


def _zustaende_aus_js() -> set[str]:
    """Die Werte, die `_leistenRollhinweis()` in `data-rollen` schreiben kann."""
    block = BASE[BASE.index("function _leistenRollhinweis"):]
    block = block[:block.index("}")]
    return set(re.findall(r"'([a-zäöü]+)'", block))


def test_js_und_css_kennen_dieselben_zustaende():
    js = _zustaende_aus_js()
    css = set(re.findall(r'\.nav-sub-tabs\[data-rollen="([a-zäöü]+)"\]', STYLE))

    assert js, "Kein Zustand im Skript gefunden — der Test greift ins Leere."
    # 'nein' ist der Ruhezustand und braucht bewusst keine Regel: keine Maske.
    ohne_regel = js - css - {"nein"}
    assert not ohne_regel, (
        f"Das Skript setzt data-rollen={ohne_regel}, aber style.css kennt nur "
        f"{sorted(css)}. Der Verlauf bliebe aus — ohne Fehlermeldung, die Leiste "
        "sähe wieder abgeschnitten aus.")

    verwaist = css - js
    assert not verwaist, (
        f"style.css zeichnet einen Verlauf für {verwaist}, aber das Skript setzt "
        "diesen Wert nie. Tote Regel oder umbenannter Zustand.")


def test_ruhezustand_bekommt_keine_maske():
    """Ohne Überstand kein Verlauf — sonst sähe eine passende Leiste beschnitten aus."""
    assert '[data-rollen="nein"]' not in STYLE
    assert "'nein'" in BASE, "Der Ruhezustand muss gesetzt werden, nicht weggelassen"


def test_maske_statt_farbkasten():
    """⚠️ Der Verlauf darf keine Hintergrundfarbe brauchen.

    Ein überlagerter Kasten mit `background: #f4f6f9` wäre eine zweite Stelle,
    die bei jeder Themenänderung mitzupflegen ist — und im Dunkelmodus als
    heller Streifen stehen bliebe. `mask-image` blendet aus, statt zu übermalen,
    und ist damit von selbst richtig.
    """
    for zustand in ("rechts", "links", "beides"):
        regel = re.search(rf'\.nav-sub-tabs\[data-rollen="{zustand}"\]\s*\{{(.*?)\}}',
                          STYLE, re.S)
        assert regel, f"keine Regel für {zustand}"
        inhalt = regel.group(1)
        assert "mask-image" in inhalt, f"{zustand}: kein mask-image"
        assert "background" not in inhalt, (
            f"{zustand} übermalt mit einer Hintergrundfarbe statt auszublenden — "
            "im Dunkelmodus bliebe ein heller Streifen stehen.")


def test_webkit_praefix_ist_dabei():
    """Ohne Präfix zeigt Safari (und älteres Edge) keinen Verlauf."""
    for zustand in ("rechts", "links", "beides"):
        regel = re.search(rf'\.nav-sub-tabs\[data-rollen="{zustand}"\]\s*\{{(.*?)\}}',
                          STYLE, re.S).group(1)
        assert "-webkit-mask-image" in regel, zustand


@pytest.mark.parametrize("vorlage", ["relay.html"])
def test_einstellungsseiten_sind_gleich_breit(vorlage):
    """⚠️ Der zweite Teil des Befunds.

    Die Relay-Seite hatte `weit` (1600px statt 960px) und war damit die einzige
    Einstellungsseite, die aus der Reihe fiel — auffällig beim Reiterwechsel,
    und sie verdeckte nebenbei, dass die Leiste überhaupt zu breit ist.

    Die Tabellen darin rollen in ihrem eigenen Bereich; sie brauchen die
    Sonderbreite nicht.
    """
    text = (VORLAGEN / vorlage).read_text("utf-8")
    assert "main_klasse" not in text, (
        f"{vorlage} setzt eine eigene Hauptbreite und fällt damit aus der Reihe "
        "der Einstellungsseiten.")


def test_relay_tabellen_nutzen_den_gemeinsamen_rollbereich():
    """Nicht neu implementieren, was es gibt — `.tabellen-rollbereich` ist da."""
    text = (VORLAGEN / "relay.html").read_text("utf-8")
    assert text.count('class="tabellen-rollbereich"') == 2
    assert "overflow-x:auto" not in text, (
        "Der Rollbereich ist mit Inline-CSS nachgebaut statt die vorhandene "
        "Klasse zu nutzen.")
    assert text.count('class="config-table"') == 2, (
        "Die Tabellen brauchen die gemeinsame Tabellenklasse — sonst fehlen "
        "Rahmen, Zeilentrenner und die Dark-Mode-Abdeckung.")
