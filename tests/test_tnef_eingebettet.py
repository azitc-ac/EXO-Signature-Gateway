"""TNEF (winmail.dat) mit Add-in-Signatur: eingebettetes Logo und Kennzeichen
bleiben erhalten (v1.11.66).

ANLASS (12.11.2026): Das klassische Outlook schickt interne Post als TNEF. Steckt
darin eine vom Verfassen-Add-in gesetzte Signatur mit Logo, passierte zweierlei:
  1. `_expand_tnef` hängte das Logo als application/octet-stream-Anhang OHNE
     Content-ID an — das Bild erschien als loser Anhang, an seiner Stelle stand
     „Das verknüpfte Bild kann nicht angezeigt werden“.
  2. Outlook schreibt das Kennzeichen des Add-ins in TNEF ohne Anführungszeichen
     (`<div id=exo-sig-s>`). Die Prüfungen suchten nur `id="…"`/`id='…'` — die
     Signatur galt als fehlend, das Gateway hängte eine zweite an.

`daten/winmail_addin_logo.dat` ist die TNEF-Hülle einer echten Mail aus dem
eigenen Test-Tenant (Erika → Alexander, „ACME: TEST-logo“), aufgenommen über den
MIME-Mitschnitt des Gateways.
"""
import email
import email.mime.base
import email.mime.multipart
import email.mime.text
import re
from pathlib import Path

import pytest

import mail_processor as mp

DATEI = Path(__file__).resolve().parent / "daten" / "winmail_addin_logo.dat"
CID = "image001.png@01DD72E0.C19C2720"


def _mail(blob: bytes) -> email.message.Message:
    m = email.mime.multipart.MIMEMultipart("mixed")
    m["From"] = "Erika Mustermann <erika@zarenko.net>"
    m["To"] = "alexander@zarenko.net"
    m["Subject"] = "Logo-Test"
    m.attach(email.mime.text.MIMEText("hallo", "plain", "utf-8"))
    t = email.mime.base.MIMEBase("application", "ms-tnef")
    t.set_payload(blob)
    email.encoders.encode_base64(t)
    t.add_header("Content-Disposition", "attachment", filename="winmail.dat")
    m.attach(t)
    return email.message_from_bytes(m.as_bytes())


@pytest.fixture
def entpackt():
    return mp._expand_tnef(_mail(DATEI.read_bytes()))


def test_logo_bleibt_eingebettet(entpackt):
    assert entpackt.get_content_type() == "multipart/related"
    bilder = [p for p in entpackt.walk() if p.get_content_maintype() == "image"]
    assert len(bilder) == 1
    bild = bilder[0]
    assert bild.get_content_type() == "image/png"
    assert bild["Content-ID"] == f"<{CID}>"
    assert bild.get_content_disposition() == "inline"
    assert bild.get_payload(decode=True)[:8] == b"\x89PNG\r\n\x1a\n"
    html = mp.extract_html(entpackt)
    assert f'src="cid:{CID}"' in html                         # Verweis und Teil passen zusammen
    assert not any(p.get_content_disposition() == "attachment" for p in entpackt.walk())


def test_kopfzeilen_bleiben(entpackt):
    assert entpackt["From"] == "Erika Mustermann <erika@zarenko.net>"
    assert entpackt["Subject"] == "Logo-Test"


def test_add_in_signatur_wird_erkannt(entpackt):
    html = mp.extract_html(entpackt)
    assert "<div id=exo-sig-s>" in html                      # so schreibt es Outlook in TNEF
    assert mp._has_own_sig_in_compose_area(entpackt)
    assert mp._addin_block_spanne(html) is not None


# ── Echte Dateianhänge bleiben Anhänge ─────────────────────────────────────────

class _Attr:
    def __init__(self, name, data):
        self.name, self.data = name, data


class _Anhang:
    def __init__(self, name, data, attrs):
        self.name, self.data, self.mapi_attrs = name, data, [_Attr(k, v) for k, v in attrs.items()]


def test_anhang_ohne_content_id_bleibt_anhang(monkeypatch):
    import tnefparse

    class _Tnef:
        def __init__(self, _):
            self.htmlbody = '<html><body>x<img src="cid:bild@1"></body></html>'
            self.attachments = [
                _Anhang("bericht.pdf", b"%PDF-1.4", {0x370E: b"application/pdf\x00"}),
                _Anhang("bild.png", b"\x89PNG", {0x3712: "bild@1\x00", 0x370E: "image/png"}),
            ]
    monkeypatch.setattr(tnefparse, "TNEF", _Tnef)
    n = mp._expand_tnef(_mail(b"egal"))
    assert n.get_content_type() == "multipart/mixed"
    rel, pdf = n.get_payload()
    assert rel.get_content_type() == "multipart/related"
    assert pdf.get_content_type() == "application/pdf"
    assert pdf.get_content_disposition() == "attachment" and pdf.get_filename() == "bericht.pdf"
    bild = rel.get_payload()[1]
    assert bild["Content-ID"] == "<bild@1>" and bild.get_content_disposition() == "inline"


# ── Eine Erkennung für jede Schreibweise ──────────────────────────────────────

@pytest.mark.parametrize("text", [
    '<div id="exo-sig-s">', "<div id='exo-sig-s'>", "<div id=exo-sig-s>",
    '<div id="x_exo-sig-s">', "<div id=x_exo-sig-s class=x>", "<DIV ID = exo-sig-s>",
    '<div class="exo-gateway-sig">', '<div class="a exo-gateway-sig b">',
    "<div class=exo-gateway-sig>", "<!-- exo-sig-start -->",
])
def test_kennung_in_jeder_schreibweise(text):
    assert mp.hat_kennung(text)


@pytest.mark.parametrize("text", [
    "<div id=exo-sig-sx>", '<div id="exo-sig-start">', '<div class="exo-gateway-sig-alt">',
    "exo-sig-s ohne Attribut", '<div title="exo-gateway-sig">', "",
])
def test_keine_kennung(text):
    assert not mp.hat_kennung(text)


def test_alle_pruefstellen_nutzen_die_eine_erkennung():
    """Keine feste Schreibweise mehr in den Prüfungen — sonst fehlt beim
    nächsten Format wieder eine."""
    quelle = (Path(mp.__file__)).read_text(encoding="utf-8")
    for alt in ("'id=\"x_exo-sig-s\"' in", "\"id='x_exo-sig-s'\" in",
                "html.find('id=\"x_exo-sig-s\"')", "f'class=\"{_SIG_CLASS}\"' in"):
        assert alt not in quelle, alt
    for fn in ("_has_own_sig_in_compose_area", "_has_sig_in_thread", "marker_im_zitat"):
        rumpf = quelle.split(f"def {fn}(", 1)[1].split("\ndef ", 1)[0]
        assert "hat_kennung(" in rumpf, fn
    assert re.search(r"_ADDIN_DIV_RE = re\.compile\(.*\[\"']\?", quelle)
