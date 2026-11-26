"""Neue Lexware-Vorlage (E-Mail-Baukasten): „Box weg, wie früher“.

Zielbild (26.11.2026): Grauer Rahmen, Hintergrund und Zentrierung der neuen
Vorlage entfernen; linksbündig als Geschäftsbrief, Schrift wie die übrigen
Lexware-Korrekturen (Calibri 11pt); die Signatur sitzt nach dem letzten Text.
Nur für als Lexware ERKANNTE Belege (X-Mailer lxo) — die Klasse es-wrapper
verwenden auch andere Newsletter-Werkzeuge, deren Gestaltung bleibt.
Die alte Vorlage prüft weiter tests/test_lexware_ausrichtung.py.

Vorlage: tests/daten/werkzeug_layoutbox.html (Gerüst nachgebildet, Text erfunden).
"""
import re
from pathlib import Path

import pytest

import mail_processor as mp

VORLAGE = (Path(__file__).parent / "daten" / "werkzeug_layoutbox.html").read_text(encoding="utf-8")


@pytest.fixture
def zaehler(monkeypatch):
    import settings_store
    import stats
    gezaehlt: list[str] = []
    echt = settings_store.get
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a: True if k == "LEXWARE_FIX_FORMAT" else echt(k, *a))
    monkeypatch.setattr(stats, "increment", lambda name, *a, **kw: gezaehlt.append(name))
    return gezaehlt


def _body(html: str) -> str:
    return html[html.lower().find("<body"):]


def _sichtbar(html: str) -> str:
    ohne = re.sub(r"<(style|script)\b[^>]*>.*?</\1>|<!--.*?-->", "", html, flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", ohne)).strip()


def test_box_weg_linksbuendig(zaehler):
    neu = _body(mp._fix_lexware_format(VORLAGE, lexware=True))
    assert not mp._RESTZENTRIERUNG_RE.search(neu), mp._RESTZENTRIERUNG_RE.search(neu)
    assert not re.search(r"bgcolor|background|<v:background", neu, re.I), \
        re.search(r".{40}(bgcolor|background|<v:background).{40}", neu, re.I | re.S)
    assert "lexware_unbekannt" not in zaehler, zaehler


def test_schrift_wie_die_alten_korrekturen(zaehler):
    neu = _body(mp._fix_lexware_format(VORLAGE, lexware=True))
    assert set(re.findall(r"font-family:[^;\"]*", neu)) == {"font-family:Calibri,sans-serif"}
    assert set(re.findall(r"font-size:[^;\"]*", neu)) == {"font-size:11.0pt"}
    assert "mso-line-height-rule:exactly" not in neu
    assert not re.search(r"line-height:\s*\d+px", neu)


def test_kein_text_geht_verloren(zaehler):
    neu = mp._fix_lexware_format(VORLAGE, lexware=True)
    assert _sichtbar(neu) == _sichtbar(VORLAGE)
    assert "&amp; die Konditionen; R&uuml;ckfragen &lt;gern&gt;" in neu


def test_signatur_nach_dem_letzten_text(zaehler):
    from test_signatur_in_layoutbox import _offene_tags_vor
    neu = mp._append_html_sig(mp._fix_lexware_format(VORLAGE, lexware=True), "<p>SIG</p>")
    pos = neu.index(mp._SIG_MARKER_START)
    assert neu.index("Max Mustermann") < pos
    assert _offene_tags_vor(neu, pos)[-1] == "td"


def test_fremdes_werkzeug_bleibt_unberuehrt(zaehler):
    """Dieselbe Baukasten-Vorlage ohne Lexware-Kennung: keine Gestaltungsänderung."""
    assert mp._fix_lexware_format(VORLAGE, lexware=False) == VORLAGE
    assert not [z for z in zaehler if z.startswith("lexware")]


def test_weitere_fassung_mit_restzentrierung_faellt_auf(zaehler):
    abgewandelt = VORLAGE.replace("<p style=", "<center><p style=", 1).replace(
        "Damen und Herren,</p>", "Damen und Herren,</p></center>", 1)
    mp._fix_lexware_format(abgewandelt, lexware=True)
    assert "lexware_unbekannt" in zaehler, zaehler
