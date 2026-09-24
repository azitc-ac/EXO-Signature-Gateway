"""Reinject-Bytes tragen CRLF, keinen bare LF — sonst leerer Body.

ANLASS (2026-09-24): Im Modus REINJECT_MODE=smtp kam signierte Post mit
**leerem Body** an. Ursache: `handler` serialisierte die reinjizierte MIME mit
`msg.as_bytes()` OHNE `policy=SMTP` → bare LF (\\n). `smtplib.sendmail` reicht
Bytes roh weiter (nur Dot-Stuffing, keine Zeilenenden-Normalisierung), Exchange
zerlegt ein Multipart mit bare-LF-Boundaries nicht → text/plain mit leerem Body
zugestellt. (Im Graph-Modus fiel es nie auf, weil der Graph-Pfad vor dem Base64
auf CRLF normalisiert.)

Fix: alle Reinject-Serialisierungen laufen über `handler._smtp_bytes()`, das
IMMER `policy=SMTP` (CRLF) nutzt. CRLF ist zugleich die S/MIME-Kanonform.

Der Test schlägt fehl, wenn man `_smtp_bytes` auf plain `as_bytes()` zurückbaut:
dann tauchen bare LF auf und die Zählung CRLF==LF bricht.
"""
import email

import handler

RAW = (
    b"From: a@x\r\nTo: b@x\r\nSubject: t\r\nMIME-Version: 1.0\r\n"
    b'Content-Type: multipart/alternative; boundary="B"\r\n\r\n'
    b"--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nHallo Welt\r\n"
    b"--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
    b"<html><body><p>Hallo BODYMARK</p></body></html>\r\n--B--\r\n"
)


def test_smtp_bytes_nur_crlf_kein_bare_lf():
    msg = email.message_from_bytes(RAW)
    out = handler._smtp_bytes(msg)
    # Kein bare LF: jede \n ist Teil einer \r\n
    assert b"\r\n" in out
    assert out.count(b"\n") == out.count(b"\r\n"), (
        "policy=SMTP muss CRLF erzeugen; bare LF bricht Multipart-Boundaries "
        "in Exchange → leerer Body.")


def test_body_bleibt_erhalten():
    msg = email.message_from_bytes(RAW)
    out = handler._smtp_bytes(msg)
    m2 = email.message_from_bytes(out)
    htmls = [p.get_payload(decode=True) for p in m2.walk()
             if p.get_content_type() == "text/html"]
    assert htmls and b"BODYMARK" in htmls[0]


def test_kontrast_plain_as_bytes_haette_bare_lf():
    """Belegt die Ursache: plain as_bytes() (der zurückgebaute Zustand) erzeugt
    bare LF — genau das, was der Fix vermeidet."""
    msg = email.message_from_bytes(RAW)
    plain = msg.as_bytes()
    assert plain.count(b"\n") > plain.count(b"\r\n"), (
        "Erwartet bare LF aus plain as_bytes() — sonst greift der Test nicht.")
