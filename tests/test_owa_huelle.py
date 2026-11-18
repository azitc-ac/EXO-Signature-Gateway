"""OWA-Hülle `divtagdefaultwrapper` ist keine Zitatgrenze, wenn sie den eigenen
Text umschliesst (v1.12.74).

ANLASS (18.11.2026, Exchange SE lokal, OWA): Eine NEUE Mail mit der von OWA
eingefügten Gateway-Signatur kam beim Empfänger mit zwei Blöcken an — der
OWA-Signatur und zusätzlich der Antwort-Signatur des Gateways, samt Trennlinie
ÜBER dem Text. Protokoll: „SIG-KETTE: … Antwort-Signatur … (erkannt: Kennung
im Zitat)“ und „Signature inserted before OWA forward wrapper“.

Ursache: Klassisches OWA umschliesst den ganzen Rumpf mit
`<div id="divtagdefaultwrapper">`, und das Gateway hielt jede solche Hülle für
den Anfang eines Zitats. Damit war der ganze Rumpf „Zitat“: Der
Verfassenbereich war leer (keine Kennung → nicht überspringen), die Kette fand
die eigene, markierte Signatur „im Zitat“.

Aufbau unten wie in der gesendeten Mail gemessen (Gesendete Elemente, per EWS
MimeContent), Inhalte erfunden.
"""
import email

import pytest

import mail_processor as mp

from test_zusatz_ohne_signatur import ABS, _mail, _versand, anlage  # noqa: F401

MERKMALE = {ABS}

# Wie OWA lokal sie einfügt: signaturehtml aus OWA.UserOptions in einem
# <div id="Signature">, alles in der Hülle.
OWA_SIG = ('<div id="Signature">' + mp.markiert(
    '<table><tr><td>Mit freundlichen Grüßen</td></tr><tr><td><b>Max Muster</b></td></tr>'
    f'<tr><td><a href="mailto:{ABS}">{ABS}</a></td></tr></table>') + "</div>")
HUELLE = ('<div id="divtagdefaultwrapper" style="font-size:12pt;color:#000000;'
          'font-family:Calibri,Helvetica,sans-serif;" dir="ltr">')


def _owa_neu(sig=OWA_SIG, vor_huelle=""):
    return f"{vor_huelle}{HUELLE}\n<p>Test: genau eine Signatur erwartet.</p>\n<p><br>\n</p>\n{sig}\n</div>"


def _owa_antwort(zitat_sig=OWA_SIG.replace('id="exo-sig-s"', 'id="x_exo-sig-s"')):
    """Antwort aus OWA lokal: eigener Text in der Hülle, darunter Strich,
    Kopfblock und das Zitat der eigenen, signierten Mail."""
    return (f'{HUELLE}<p>Danke, passt so.</p></div>'
            '<hr style="display:inline-block;width:98%" tabindex="-1">'
            '<div id="divRplyFwdMsg" dir="ltr"><b>Von:</b> Max Muster<br><b>Gesendet:</b> heute<br>'
            '<b>Betreff:</b> Angebot</div>'
            f'<div><p>Hier das Angebot.</p>{zitat_sig}</div>')


def _msg(rumpf):
    return email.message_from_bytes(_mail(rumpf=rumpf))


# ── Erkennung (mail_processor) ────────────────────────────────────────────────

def test_neue_mail_owa_signatur_steht_im_verfassenbereich():
    msg = _msg(_owa_neu())
    assert mp._find_first_quote_wrapper_pos(mp.extract_html(msg)) is None
    assert mp._has_own_sig_in_compose_area(msg)
    assert mp.kette_grund(msg, MERKMALE, [], MERKMALE) == ""


@pytest.mark.parametrize("vor", ["", "\n  ", "<!-- Kommentar -->", "<p>&nbsp;</p>", "<span> </span>"],
                         ids=["nichts", "leerraum", "kommentar", "nbsp", "leeres-element"])
def test_unsichtbares_vor_der_huelle_aendert_nichts(vor):
    html = mp.extract_html(_msg(_owa_neu(vor_huelle=vor)))
    assert mp._find_first_quote_wrapper_pos(html) is None


def test_antwort_kette_wird_weiter_erkannt():
    """Gegenprobe: eigene markierte Signatur NUR im Zitat → Kette wie bisher."""
    msg = _msg(_owa_antwort())
    html = mp.extract_html(msg)
    assert html.index('id="divRplyFwdMsg"') > mp._find_first_quote_wrapper_pos(html) > html.index("passt so")
    assert not mp._has_own_sig_in_compose_area(msg)
    assert mp.kette_grund(msg, MERKMALE, [], MERKMALE) == "kennung"


@pytest.mark.parametrize("vor", ["<p>Siehe unten.</p>", '<img src="cid:x">'], ids=["text", "bild"])
def test_huelle_hinter_eigenem_inhalt_bleibt_zitatgrenze(vor):
    """Gegenprobe: zitierte OWA-Mail ohne andere Grenze (etwa von einem Programm
    ohne erkannten Trenner) — die Hülle des Originals bleibt die Grenze."""
    html = f"<html><body>{vor}<div id=\"divtagdefaultwrapper\"><p>Original</p></div></body></html>"
    assert mp._find_first_quote_wrapper_pos(html) == html.index("<div id=\"divtagdefaultwrapper\"")
    raus = mp._append_html_sig(html, "<p>SIG</p>")
    assert raus.index("SIG") < raus.index("Original")


def test_zweite_huelle_im_zitat_zaehlt_die_erste_nicht():
    html = (f"<html><body>{HUELLE}<p>Eigener Text</p></div><p>Ursprüngliche Nachricht</p>"
            '<div id="x_divtagdefaultwrapper"><p>Original</p></div></body></html>')
    assert mp._find_first_quote_wrapper_pos(html) == html.index('<div id="x_divtagdefaultwrapper"')


# ── Einfügen: hinter den Text, ohne Trennlinie ───────────────────────────────

def test_neue_mail_ohne_signatur_gateway_setzt_unter_den_text():
    html = f"<html><body>{_owa_neu(sig='')}</body></html>"
    raus = mp._append_html_sig(html, "<p>GATEWAY</p>")
    assert raus.index("genau eine Signatur") < raus.index("GATEWAY")
    assert "<hr" not in raus, "neue Mail: keine Trennlinie"


# ── Durch den echten handle_DATA ──────────────────────────────────────────────

def test_handler_neue_mail_mit_owa_signatur_keine_zweite(anlage):  # noqa: F811
    anlage["MAILBOX_CONFIG"][ABS]["min_template"] = "Minimal"
    html = _versand(anlage, _mail(rumpf=_owa_neu()))
    assert "[Sig]" not in html and "[Minimal]" not in html, "zweite Signatur angehängt"
    assert html.count("Max Muster") == 1
    assert html.index("genau eine Signatur") < html.index("Max Muster")


def test_handler_antwort_kennung_im_zitat_minimal(anlage):  # noqa: F811
    """Gegenprobe: echte Antwort, eigene Signatur nur im Zitat → Antwort-Signatur."""
    anlage["MAILBOX_CONFIG"][ABS]["min_template"] = "Minimal"
    html = _versand(anlage, _mail(betreff="AW: Angebot", rumpf=_owa_antwort()))
    assert "[Minimal]" in html and "[Sig]" not in html
    assert html.index("passt so") < html.index("[Minimal]") < html.index("divRplyFwdMsg")


def test_handler_neue_mail_ohne_owa_signatur_volle_signatur(anlage):  # noqa: F811
    html = _versand(anlage, _mail(rumpf=_owa_neu(sig="")))
    assert "[Sig]" in html
    assert html.index("genau eine Signatur") < html.index("[Sig]")
