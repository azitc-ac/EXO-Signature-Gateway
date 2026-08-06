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


def test_kuerzung_erfasst_absaetze_und_bloecke():
    """span.hint bleibt bewusst aussen vor (inline, würde umbrechen)."""
    js = COMMON.read_text(encoding="utf-8")
    sel = re.search(r"querySelectorAll\((['\"])(.+?)\1\)", js[js.index("function initHintClamps"):])
    assert sel, "Selektor nicht gefunden"
    s = sel.group(2)
    assert "p.hint" in s and "div.hint" in s, s
    assert "span.hint" not in s, (
        "span.hint steht meist inline hinter einem Feld; display:-webkit-box "
        "macht daraus einen Block und verschiebt das Layout: " + s)


def test_css_deckt_beide_elementtypen_ab():
    """Ohne die CSS-Regel setzt das JS zwar data-clamp, es passiert aber nichts."""
    css = (WURZEL / "app" / "webui" / "static" / "style.css").read_text(encoding="utf-8")
    assert 'p.hint[data-clamp="zu"]' in css
    assert 'div.hint[data-clamp="zu"]' in css


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
