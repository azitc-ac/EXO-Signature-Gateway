"""Werkzeug-Mails mit Layout-Box: Die Signatur gehört IN die Box.

Anlass (26.11.2026): Eine Auftragsbestätigung aus Lexware Office (neue Vorlage
aus einem E-Mail-Baukasten, Kennzeichen `es-wrapper`) legt den Text in eine
zentrierte weiße Box mit 600 px Breite auf grauem Rahmen. Das Gateway setzte
die Signatur vor `</body>` — unter den Rahmen, ausserhalb der Box,
linksbündig auf Grau, mit anderem Rand als der Text. Die Lexware-Korrekturen
(`_fix_lexware_*`) griffen nicht: Sie erkennen nur die ältere Vorlage am
Marker `id="templateBody"`.

Allgemeine Regel statt Sonderfall (`mail_processor._inhalts_container`):
Steckt der Inhalt hinter einer Kette einzelner Hüllen, und ist darunter eine
Layout-Box (Tabelle, Breite, Hintergrund), kommt die Signatur ans Ende des
innersten Containers. Sonst bleibt es bei „vor </body>“.

Die Vorlage `daten/werkzeug_layoutbox.html` bildet das Gerüst der echten Mail
nach; Text, Namen und Nummern sind erfunden.
"""
import re
from pathlib import Path

import mail_processor as mp

VORLAGE = (Path(__file__).parent / "daten" / "werkzeug_layoutbox.html").read_text(encoding="utf-8")
SIG = '<table><tr><td style="padding:0">SIGNATUR-TEST</td></tr></table>'


def _sichtbar(html: str) -> str:
    ohne = re.sub(r"<(style|script)\b[^>]*>.*?</\1>|<!--.*?-->", "", html, flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", ohne)).strip()


def _offene_tags_vor(html: str, pos: int) -> list[str]:
    """Die Tags, in denen Position `pos` steckt (äußerster zuerst)."""
    stapel = []
    vorher = re.sub(r"<!--.*?-->|<(style|script)\b[^>]*>.*?</\1>", "", html[:pos], flags=re.S | re.I)
    for m in re.finditer(r"<(/?)([a-zA-Z][\w:.-]*)([^>]*)>", vorher):
        zu, tag, attr = m.group(1), m.group(2).lower(), m.group(3)
        if ":" in tag or tag in mp._INHALT_VOID or attr.rstrip().endswith("/"):
            continue
        if zu:
            if tag in stapel:
                while stapel.pop() != tag:
                    pass
        else:
            stapel.append(tag if tag != "table" or "600px" not in attr else "table600")
    return stapel


def test_signatur_steht_in_der_textzelle_der_box():
    neu = mp._append_html_sig(VORLAGE, SIG)
    pos = neu.index(mp._SIG_MARKER_START)
    rahmen = _offene_tags_vor(neu, pos)
    assert "table600" in rahmen, f"Signatur ausserhalb der 600-px-Box: {rahmen}"
    assert rahmen[-1] == "td", rahmen                  # direkt in der Textzelle
    # nach dem letzten Inhalt, nicht davor
    assert neu.index("Max Mustermann") < pos
    # die Signatur schliesst die Zelle ab: danach kommt nur noch Gerüst
    nach = neu[neu.index(mp._SIG_MARKER_END) + len(mp._SIG_MARKER_END):]
    assert re.match(r"\s*</td>", nach), nach[:80]


def test_kein_inhalt_geht_verloren():
    """Entities, Semikolons und spitze Klammern im Text bleiben unverändert."""
    neu = mp._append_html_sig(VORLAGE, SIG)
    assert _sichtbar(neu).replace(" SIGNATUR-TEST", "") == _sichtbar(VORLAGE)
    assert "&amp; die Konditionen; R&uuml;ckfragen &lt;gern&gt;" in neu


def test_einfache_mail_bleibt_vor_body_ende():
    """Ohne Layout-Box (Outlook: body > div.WordSection1 > p…) nichts ändern."""
    html = ('<html><body lang="DE"><div class="WordSection1"><p class="MsoNormal">Hallo,</p>'
            '<p class="MsoNormal">Gruß<o:p></o:p></p></div></body></html>')
    neu = mp._append_html_sig(html, SIG)
    assert re.search(re.escape(mp._SIG_MARKER_END) + r"\s*</body>", neu), neu


def test_mehrere_bloecke_oben_bleibt_vor_body_ende():
    """Kopf, Inhalt, Fuß nebeneinander: keine eindeutige Box → wie bisher."""
    html = ('<html><body><table width="600"><tr><td>Kopf</td></tr></table>'
            '<table width="600"><tr><td>Text</td></tr></table></body></html>')
    neu = mp._append_html_sig(html, SIG)
    assert re.search(re.escape(mp._SIG_MARKER_END) + r"\s*</body>", neu), neu


def test_unsauberes_geruest_wird_nicht_geraten():
    html = ('<html><body><div style="background:#eee"><table width="600"><tr><td>Text'
            '</div></td></tr></table></body></html>')
    assert mp._inhalts_container(html) is None
    # überzähliges schließendes Tag: am Ende wieder „ausgeglichen“, trotzdem unklar
    ueberzaehlig = ('<html><body><div style="background:#eee"><table width="600"><tr>'
                    '<td>Text</td></tr></table></div></div></body></html>')
    assert mp._inhalts_container(ueberzaehlig) is None
    neu = mp._append_html_sig(html, SIG)
    assert re.search(re.escape(mp._SIG_MARKER_END) + r"\s*</body>", neu), neu


def test_zitat_hat_weiter_vorrang():
    """Eine Antwort mit Zitat geht wie bisher vor das Zitat — die Box-Regel
    gilt nur für den Rückfall ohne Zitat."""
    html = ('<html><body><div style="width:600px;background:#fff"><p>Neu</p>'
            '<blockquote><p>Alt</p></blockquote></div></body></html>')
    neu = mp._append_html_sig(html, SIG)
    assert neu.index("SIGNATUR-TEST") < neu.index("<blockquote")
    assert neu.index("Neu") < neu.index("SIGNATUR-TEST")
