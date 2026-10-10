"""Lange fremde Kopfzeilen überstehen den Rückweg unverändert.

ANLASS (2026-10-10, Outlook-Testlauf T05b): Eine Antwort von Outlook trug in
`References` die Message-ID der Ursprungsmail — bei Exchange Online 85 Zeichen
ohne Leerzeichen. Nach dem Gateway stand dort
`=?utf-8?q?=3CAMBPR05MB…=40AMBPR05M?= =?utf-8?q?B12050=2E…?=`.
`email.policy.SMTP` faltet bei 78 Zeichen; was sich nicht umbrechen lässt,
kodiert das email-Paket nach RFC 2047. In References erkennt darin kein
Mailprogramm mehr eine Kennung, beim Empfänger zerfällt die Konversation.

Fix: `mime_policy.SMTP_REINJECT` (SMTP mit 998 Zeichen je Zeile) an beiden
Stellen, die empfangene Mails neu serialisieren.
"""
import email

import handler
import mail_processor
import smtp_submit

MID = "<AMBPR05MB120506B07CD9078AB2747CD69A1912@AMBPR05MB12050.eurprd05.prod.outlook.com>"
RAW = (
    "From: Alexander <alexander@zarenko.net>\r\nTo: echo25@mail.de\r\n"
    "Subject: =?utf-8?q?AW=3A_Pr=C3=BCfung?=\r\n"
    f"Message-ID: {MID.replace('A1912', 'B1912')}\r\n"
    "In-Reply-To: <179167236058.107.8929157007164733220@mail.de>\r\n"
    f"References: {MID}\r\n <179167236058.107.8929157007164733220@mail.de>\r\n"
    "MIME-Version: 1.0\r\n"
    'Content-Type: multipart/alternative; boundary="B"\r\n\r\n'
    "--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nAntwort\r\n"
    "--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
    "<html><body><p>Antwort</p></body></html>\r\n--B--\r\n"
).encode()


def _kopf(raw: bytes) -> str:
    return raw.split(b"\r\n\r\n", 1)[0].decode("ascii")


def _referenzen_klartext(raw: bytes) -> None:
    kopf = _kopf(raw)
    assert "=?utf-8?" not in kopf.split("References:", 1)[1].split("\r\n", 2)[0] + \
        kopf.split("References:", 1)[1].split("\r\n", 2)[1]
    assert f"References: {MID}" in kopf
    assert out_crlf(raw)


def out_crlf(raw: bytes) -> bool:
    return raw.count(b"\n") == raw.count(b"\r\n")


def test_smtp_bytes_laesst_lange_references_im_klartext():
    _referenzen_klartext(handler._smtp_bytes(email.message_from_bytes(RAW)))


def test_mit_signatur_eingefuegt_bleiben_references_im_klartext():
    """Der echte Weg: Signatur einfügen, dann serialisieren."""
    msg = email.message_from_bytes(RAW)
    neu = mail_processor.inject(msg, "<p>SIGMARK</p>", "SIGMARK", use_cid_images=False)
    out = handler._smtp_bytes(neu)
    _referenzen_klartext(out)
    html = [t.get_payload(decode=True) for t in email.message_from_bytes(out).walk()
            if t.get_content_type() == "text/html"]
    assert html and b"SIGMARK" in html[0]


def test_587_weg_laesst_lange_references_im_klartext():
    out = smtp_submit._rewrite_from(RAW, "relay@zarenko.net")
    _referenzen_klartext(out)
    assert "relay@zarenko.net" in _kopf(out)


def test_selbst_gesetzter_umlaut_betreff_bleibt_kodierbar():
    """Kontrast zu refold_source="none": eine vom Gateway gesetzte Kopfzeile
    muss weiterhin kodiert und gefaltet werden, sonst bräche das Serialisieren."""
    msg = email.message_from_bytes(RAW)
    del msg["Subject"]
    msg["Subject"] = handler._encode_subject("Grüße " + "x" * 90)
    kopf = _kopf(handler._smtp_bytes(msg))
    assert "Subject: =?utf-8?" in kopf
    assert all(len(z) <= 998 for z in kopf.split("\r\n"))
