"""Lange Erklärtexte werden auf allen Seiten gekürzt, nicht nur auf einer.

Anlass: initHintClamps() wurde in v1.7.96 eingeführt, aber nur in
settings_connect.html aufgerufen. Die übrigen sechzehn Vorlagen mit zusammen
über hundert Erklärtexten blieben ungekürzt — auffindbar nur, indem man jede
Seite ansieht und sich an die frühere Vereinbarung erinnert. Kein Prüfskript
schlug an: Die Vorlagen sind syntaktisch einwandfrei, die Funktion existiert,
sie wird nur nicht gerufen.

Der Aufruf gehört deshalb in base.html. Diese Tests halten ihn dort fest.
"""
import re
from pathlib import Path

WURZEL = Path(__file__).resolve().parents[1]
BASE = WURZEL / "app" / "webui" / "templates" / "base.html"
COMMON = WURZEL / "app" / "webui" / "static" / "common.js"
VORLAGEN = WURZEL / "app" / "webui" / "templates"


def test_base_html_ruft_die_kuerzung():
    """Ohne diesen Aufruf wirkt die Kürzung nur dort, wo jemand daran denkt."""
    assert "initHintClamps()" in BASE.read_text(encoding="utf-8"), (
        "base.html ruft initHintClamps() nicht — dann bleiben alle Erklärtexte "
        "auf allen geerbten Seiten ungekürzt.")


def test_die_funktion_gibt_es_ueberhaupt():
    assert "function initHintClamps" in COMMON.read_text(encoding="utf-8")


def _koerper(js: str, name: str) -> str:
    """Rumpf einer Funktion — bis zur ersten Zeile, die nur `}` enthält.

    Nicht das erste querySelectorAll nach dem Funktionsnamen nehmen: Der
    Selektor ist über mehrere Zeilen zusammengesetzt, ein zeilenweiser Regex
    lief deshalb weiter und erwischte den des resize-Handlers.
    """
    ab = js.index("function " + name)
    ende = js.index("\n}", ab)
    return js[ab:ende]


def test_kuerzung_entscheidet_nach_darstellung_nicht_nach_tag():
    """Inline dargestellte Hinweise bleiben aussen vor — aber die Entscheidung
    faellt an der Darstellung, nicht am Elementnamen.

    ⚠️ Bis 2026-08-19 stand hier die Bedingung „kein span". Das war eine
    Naeherung fuer „nichts Inlines" und lag daneben: Auf den Einstellungsseiten
    sind Hinweise regelmaessig `span.hint` mit display:block. Zwei davon liefen
    auf einem Telefon ueber vier Zeilen, ohne je einen Schalter zu bekommen.

    display:-webkit-box aus einem echt inline stehenden Hinweis zu machen,
    verschoebe weiterhin das Layout — deshalb bleibt die Ausnahme, nur eben am
    richtigen Merkmal.
    """
    s = _koerper(COMMON.read_text(encoding="utf-8"), "initHintClamps")
    assert "'.hint:not(" in s, ("Auswahl auf Tags verengt — span.hint faellt "
                                "dann wieder durch:\n" + s)
    assert "getComputedStyle" in s and "inline" in s, (
        "Ohne Blick auf die tatsaechliche Darstellung wuerde auch ein inline "
        "stehender Hinweis zum Block gemacht:\n" + s)


def test_kuerzung_wird_gemessen_nicht_geschaetzt():
    """Die Textlänge darf nicht darüber entscheiden, ob ein Schalter erscheint.

    Ob zwei Zeilen genügen, hängt an der Breite des Kastens. Nach Zeichenzahl
    zu urteilen erzeugte Schalter, hinter denen nichts steckte — im Browser
    gemessen (tools/hintcheck.py), 8 von 23 waren leer.
    """
    js = COMMON.read_text(encoding="utf-8")
    s = _koerper(js, "_hintBewerten")
    assert "scrollHeight" in s and "clientHeight" in s, s
    assert "length" not in s, ("in _hintBewerten entscheidet die Messung, nicht "
                               "die Textlänge:\n" + s)


def test_css_gilt_fuer_jede_auszeichnung():
    """Ohne die CSS-Regel setzt das JS zwar data-clamp, es passiert aber nichts.

    ⚠️ Der Selektor darf NICHT auf Tags eingeschränkt sein. Bis 2026-08-19 stand
    hier `p.hint` und `div.hint`; auf den Einstellungsseiten sind Hinweise aber
    regelmässig `span.hint` mit display:block. Ein solcher span bekam im
    JavaScript eine Bewertung, aber nie eine Zeilenbegrenzung — die
    Ueberlaufmessung fiel dadurch immer negativ aus und der Schalter erschien
    nie. Zwei Texte liefen so auf einem Telefon über vier Zeilen.

    Welche Elemente gemeint sind, entscheidet das JavaScript anhand der
    tatsächlichen Darstellung (inline bleibt aussen vor) — nicht das CSS.
    """
    css = (WURZEL / "app" / "webui" / "static" / "style.css").read_text(encoding="utf-8")
    import re
    treffer = re.findall(r'([\w.]*)\.hint\[data-clamp="zu"\]', css)
    assert treffer, 'Regel für [data-clamp="zu"] fehlt ganz'
    verengt = [x for x in treffer if x]
    assert not verengt, (
        "Selektor auf Tags eingeschränkt: " + ", ".join(f"{x}.hint" for x in verengt))


def test_seiten_brauchen_keinen_eigenen_aufruf_mehr():
    """Wer nachrendert, darf zusätzlich rufen — aber keine Seite darf der
    einzige Aufrufer sein. Schlägt an, wenn base.html den Aufruf verliert und
    er nur noch in Einzelvorlagen steht."""
    eigene = [p.name for p in VORLAGEN.glob("*.html")
              if p.name != "base.html" and "initHintClamps()" in p.read_text(encoding="utf-8")]
    if eigene:
        assert "initHintClamps()" in BASE.read_text(encoding="utf-8"), (
            "Nur einzelne Vorlagen rufen die Kürzung auf, base.html nicht: "
            + ", ".join(eigene))
