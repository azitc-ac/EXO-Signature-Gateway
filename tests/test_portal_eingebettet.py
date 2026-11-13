"""Nachrichtenportal: eingebettete Bilder (Logo der Signatur) werden angezeigt
(v1.12.6).

ANLASS (13.11.2026, DEV-Tenant): „#enc“ an einen Empfänger ohne Zertifikat →
Portal. Dort stand an der Stelle des Logos ein kaputtes Bild (src="cid:…"), und
unter ANHÄNGE derselbe Teil als „image001.png 0 KB“. Der Browser kennt kein
cid:, und `_extract_mime_payload` legte jeden Teil mit Dateinamen als Anhang ab.
"""
import base64
import email.mime.application
import email.mime.image
import email.mime.multipart
import email.mime.text

import handler
import mail_processor as mp
from test_tnef_eingebettet import CID, DATEI, _mail as _tnef_mail

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


def _payload(roh: bytes) -> dict:
    return handler._extract_mime_payload(roh, "a@firma.de", "b@extern.de", "", "Test", "")


def test_echtes_outlook_logo_wird_eingebettet():
    """Classic Outlook (winmail.dat aus dem Test-Tenant, entpackt wie im Handler)."""
    msg = mp._expand_tnef(_tnef_mail(DATEI.read_bytes()))
    p = _payload(msg.as_bytes())
    assert f"cid:{CID}" not in p["html"]
    assert 'src="data:image/png;base64,iVBORw0KGgo' in p["html"]
    assert p["attachments"] == []


def _gebaut(html: str, *teile):
    m = email.mime.multipart.MIMEMultipart("mixed")
    rel = email.mime.multipart.MIMEMultipart("related")
    rel.attach(email.mime.text.MIMEText(html, "html", "utf-8"))
    m.attach(rel)
    for t in teile:
        (rel if t.get("Content-ID") else m).attach(t)
    return m.as_bytes()


def _bild(cid: str | None, name="logo.png", disposition="inline"):
    b = email.mime.image.MIMEImage(PNG, "png")
    if cid:
        b.add_header("Content-ID", f"<{cid}>")
    b.add_header("Content-Disposition", disposition, filename=name)
    return b


def test_echte_anhaenge_bleiben_anhaenge():
    pdf = email.mime.application.MIMEApplication(b"%PDF-1.4", "pdf")
    pdf.add_header("Content-Disposition", "attachment", filename="bericht.pdf")
    p = _payload(_gebaut('<p>x</p><img src="cid:logo@1">', _bild("logo@1"), pdf))
    assert [a["name"] for a in p["attachments"]] == ["bericht.pdf"]
    assert "data:image/png;base64," in p["html"]


def test_nicht_verwiesenes_bild_bleibt_anhang():
    p = _payload(_gebaut("<p>ohne Bild</p>", _bild("frei@1", "foto.png")))
    assert [a["name"] for a in p["attachments"]] == ["foto.png"]
    assert base64.b64decode(p["attachments"][0]["data"]) == PNG


def test_bild_als_anhang_markiert_bleibt_anhang():
    """Ausdrücklich „attachment“ — auch mit Content-ID nicht ins HTML ziehen."""
    p = _payload(_gebaut('<img src="cid:x@1">', _bild("x@1", "scan.png", "attachment")))
    assert [a["name"] for a in p["attachments"]] == ["scan.png"]
    assert "cid:x@1" in p["html"]


def test_cid_teilt_kein_praefix():
    """cid:logo@1 darf cid:logo@10 nicht treffen."""
    zwei = (_bild("logo@1", "a.png"), _bild("logo@10", "b.png"))
    p = _payload(_gebaut('<img src="cid:logo@10"><img src="CID:logo@1">', *zwei))
    assert "cid:" not in p["html"].lower()
    assert p["html"].count("data:image/png;base64,") == 2 and p["attachments"] == []


def test_portal_zeigt_kleine_anhaenge_nicht_als_0_kb():
    from pathlib import Path
    seite = (Path(__file__).resolve().parent.parent / "app/webui/templates/portal.html").read_text("utf-8")
    assert "Math.round(atob(a.data).length/1024)" not in seite
    assert "'&lt;1'" in seite


def test_vorschau_der_warteschlange_zeigt_das_logo(tmp_path, monkeypatch):
    """Geschwisterstelle: Die Vorschau in der Warteschlange zeigt der Browser."""
    import json
    import held_mails
    monkeypatch.setattr(held_mails, "_HELD_DIR", tmp_path)
    roh = _gebaut('<p>x</p><img src="cid:logo@1">', _bild("logo@1"))
    mid = held_mails.hold("a@firma.de", ["b@extern.de"], roh, reason="delivery_failed")
    vorschau = json.loads((tmp_path / f"{mid}.json").read_text("utf-8"))["html_preview"]
    assert "cid:logo@1" not in vorschau and "data:image/png;base64," in vorschau
