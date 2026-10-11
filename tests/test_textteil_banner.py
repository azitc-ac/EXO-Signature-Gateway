"""Banner und Disclaimer stehen auch im Textteil der Mail.

ANLASS (Outlook-Testlauf T02/T04, 10./11.10.2026): Mit S/MIME fehlte der
Banner im text/plain, ohne S/MIME stand er dort. Der Handler hängte Banner und
Disclaimer nur an sig_html; den Textteil schrieb er nur mit der Signatur. Ohne
Signatur baute Exchange den Textteil aus dem HTML neu (daher dort der Banner),
eine signierte Mail kann Exchange nicht mehr ändern. Für den Disclaimer heißt
das: Leser der Textfassung signierter Mails sahen ihn nie.
"""
import email
import re
from pathlib import Path

import handler
import mail_processor

HANDLER = (Path(__file__).resolve().parent.parent / "app/handler.py").read_text(encoding="utf-8")


def test_txt_anhaengen():
    assert handler._txt_anhaengen("Sig\n", "Banner") == "Sig\n\nBanner\n"
    assert handler._txt_anhaengen("", "Banner\n") == "Banner\n"
    assert handler._txt_anhaengen("Sig", "\nDiscl\n") == "Sig\n\nDiscl\n"


def test_handler_haengt_banner_und_disclaimer_an_den_textteil():
    for teil in ("_banner_txt", "_disclaimer_txt"):
        assert re.search(rf"sig_txt = _txt_anhaengen\(sig_txt, {teil}\)", HANDLER), teil
    # und verwirft den Textteil nicht mehr
    assert "_banner_html, _ = signature_engine.render" not in HANDLER
    assert "_disclaimer_html, _ = signature_engine.render" not in HANDLER


def test_textteil_der_mail_traegt_den_banner():
    raw = (b"From: a@x.de\r\nTo: b@y.de\r\nSubject: t\r\nMIME-Version: 1.0\r\n"
           b'Content-Type: multipart/alternative; boundary="B"\r\n\r\n'
           b"--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nHallo\r\n"
           b"--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
           b"<html><body><p>Hallo</p></body></html>\r\n--B--\r\n")
    sig_txt = handler._txt_anhaengen("Max Muster\n", "Schon gewusst? BANNERTEXT")
    neu = mail_processor.inject(email.message_from_bytes(raw), "<p>Max</p><p>BANNER</p>",
                                sig_txt, use_cid_images=False)
    plain = [p.get_payload(decode=True).decode() for p in neu.walk()
             if p.get_content_type() == "text/plain"]
    assert plain and "BANNERTEXT" in plain[0] and "Max Muster" in plain[0]
