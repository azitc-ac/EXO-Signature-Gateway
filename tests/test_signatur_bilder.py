"""Bilder der Add-in-Signatur als Inline-Anhang (signatur_bilder.py, v1.12.136).

Outlooks setSignatureAsync nimmt höchstens 30.000 Zeichen, Base64-Bilder
eingeschlossen; Kunden verkleinerten ihre Logos bis zur Unkenntlichkeit."""
import base64
import email
import email.policy
import os
import sys
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import signatur_bilder as sb  # noqa: E402

B64 = base64.b64encode(os.urandom(24000)).decode()          # ~32.000 Zeichen


def test_auslagern_macht_das_html_schlank_und_gleiche_bilder_einmal():
    html = (f'<table><tr><td><img src="data:image/png;base64,{B64}" width="150"></td>'
            f"<td><img src='data:image/jpeg;base64,{B64[:400]}'></td>"
            f'<td><img src="data:image/png;base64,{B64}"></td></tr></table>')
    neu, bilder = sb.auslagern(html)
    assert len(html) > 30000 and len(neu) < 600
    assert [b["typ"] for b in bilder] == ["image/png", "image/jpeg"]
    assert all(b["name"].startswith("gwsig-") and f'cid:{b["name"]}' in neu for b in bilder)
    assert bilder[0]["base64"] == B64
    # derselbe Inhalt → derselbe Name (das Add-in hängt nicht doppelt an)
    assert sb.auslagern(html)[1][0]["name"] == bilder[0]["name"]
    assert sb.auslagern("<p>ohne Bild</p>") == ("<p>ohne Bild</p>", [])


def _mail(html, *bilder):
    rel = MIMEMultipart("related")
    rel.attach(MIMEText(html, "html", "utf-8"))
    for name, cid in bilder:
        teil = MIMEImage(b"\x89PNG\r\n\x1a\n" + b"0" * 50, "png")
        teil.add_header("Content-ID", f"<{cid}>")
        teil.add_header("Content-Disposition", "inline", filename=name)
        rel.attach(teil)
    return email.message_from_bytes(rel.as_bytes(policy=email.policy.SMTP))


def test_verwaiste_signaturbilder_werden_entfernt_fremde_bleiben():
    m = _mail('<p>Text</p><img src="cid:abc123">',
              ("gwsig-111.png", "abc123"),        # verwiesen → bleibt
              ("gwsig-222.png", "def456"),        # verwaist → weg
              ("foto.png", "xyz"))                # nicht vom Add-in → bleibt
    assert sb.verwaiste_entfernen(m) == 1
    namen = [t.get_filename() for t in m.walk() if t.get_filename()]
    assert namen == ["gwsig-111.png", "foto.png"]


def test_ersetzen_der_addin_signatur_raeumt_bilder_mit_auf():
    import mail_processor
    m = _mail('<div id="Signature"><p>alt</p><img src="cid:gwsig-aaa.png"></div><p>Inhalt</p>',
              ("gwsig-aaa.png", "gwsig-aaa.png"))
    import types
    orig = mail_processor._addin_block_entfernen
    try:
        mail_processor._addin_block_entfernen = lambda html: html.replace(
            '<p>alt</p><img src="cid:gwsig-aaa.png">', "")
        mail_processor.addin_signatur_entfernen(m)
    finally:
        mail_processor._addin_block_entfernen = orig
    assert not [t for t in m.walk() if (t.get_filename() or "").startswith("gwsig-")]
    assert types  # noqa
