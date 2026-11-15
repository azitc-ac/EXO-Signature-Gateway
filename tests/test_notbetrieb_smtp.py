"""Notbetrieb: Senden aus Mailprogrammen über den 587-Listener.

Durch den echten Listener (main._LenientController mit dem echten
Authenticator) und den echten Handler, Client ist Pythons smtplib. Zusagen:
AD-Kennwort nur bei eingeschaltetem Schalter, Senden nur im ausgerufenen
Notfall (oder Probegruppe), nur mit Senderecht aus dem Rechte-Abbild,
Signatur wie im Webmail, Bcc nie in der übertragenen Mail, Gesendet-Kopie
vom Gateway — und das APPEND des Programms legt sie nicht ein zweites Mal ab.
"""
from __future__ import annotations

import smtplib
import socket
from email.message import EmailMessage
from pathlib import Path

import pytest

import main
import notbetrieb
from test_notbetrieb_webmail import _lokal, _mime, _teile, postfix, schatten  # noqa: F401


def _freier_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


listener_port: dict = {}


@pytest.fixture
def listener(postfix, schatten, monkeypatch):  # noqa: F811
    import notbetrieb_anmeldung
    schatten["NOTBETRIEB_SMTP"] = True
    versuche = []

    def anmelden(name, kennwort, quelle=""):
        versuche.append((name, quelle))
        if kennwort != "richtig":
            return {"ok": False, "error": "falsch"}
        return {"ok": True, "benutzer": name, "postfach": "a@firma.at", "zugriff": []}
    monkeypatch.setattr(notbetrieb_anmeldung, "anmelden", anmelden)
    import handler
    port = _freier_port()
    ctrl = main._LenientController(
        handler.SignatureHandler(), hostname="127.0.0.1", port=port,
        auth_required=True, auth_require_tls=False, authenticator=main._submission_authenticator)
    ctrl.start()

    def senden(msg, von="a@firma.at", an=None, kennwort="richtig"):
        with smtplib.SMTP("127.0.0.1", port, timeout=10) as c:
            c.login("a.ad@firma.local", kennwort)
            return c.send_message(msg, von, an)
    listener_port["port"] = port
    yield senden, versuche, schatten, postfix
    ctrl.stop()


def _mail(von="Anna <a@firma.at>", an="b@firma.at, kunde@extern.de", betreff="Aus Outlook"):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = von, an, betreff
    m["Message-ID"] = "<outlook-1@firma.at>"
    m.set_content("Hallo aus Outlook")
    m.add_alternative("<p>Hallo aus Outlook</p>", subtype="html")
    return m


def test_senden_mit_signatur_bcc_und_gesendet_kopie(listener):
    senden, versuche, _, uebergeben = listener
    senden(_mail(), an=["b@firma.at", "kunde@extern.de", "geheim@extern.de"])
    assert versuche == [("a.ad@firma.local", "127.0.0.1")]
    (von, empf, inhalt), = uebergeben                         # extern: Direktversand
    assert sorted(empf) == ["geheim@extern.de", "kunde@extern.de"]
    assert b"geheim@extern.de" not in inhalt.split(b"\r\n\r\n", 1)[0]
    assert "SIG Anna Muster" in _teile(inhalt)[0] and "SIG Anna Muster" in _teile(inhalt)[1]
    intern, = _lokal("b@firma.at", "inbox")                    # intern: in die Kopie
    assert "SIG Anna Muster" in _teile(Path(intern["datei"]).read_bytes())[1]
    gesendet, = _lokal("a@firma.at", "sentitems")
    assert _mime(gesendet)["Bcc"] == "geheim@extern.de"
    assert gesendet["imid"] == "<outlook-1@firma.at>"


def test_auth_login_fragt_wie_ueblich(listener):
    """Outlook Classic antwortet nur auf die üblichen Rückfragen „Username:“ /
    „Password:“ — auf aiosmtpds „User Name\\0“ hing es still."""
    import base64
    senden, versuche, _, _ = listener
    port = listener_port["port"]
    with socket.create_connection(("127.0.0.1", port), timeout=10) as roh:
        f = roh.makefile("rwb")

        def befehl(x):
            f.write(x + b"\r\n")
            f.flush()
            antwort = f.readline()
            while antwort[3:4] == b"-":
                antwort = f.readline()
            return antwort.strip()
        f.readline()
        befehl(b"EHLO outlook")
        assert befehl(b"AUTH LOGIN") == b"334 " + base64.b64encode(b"Username:")
        assert befehl(base64.b64encode(b"a.ad@firma.local")) == b"334 " + base64.b64encode(b"Password:")
        assert befehl(base64.b64encode(b"richtig")).startswith(b"235")
    assert versuche == [("a.ad@firma.local", "127.0.0.1")]


def test_falsches_kennwort(listener):
    senden, versuche, _, uebergeben = listener
    with pytest.raises(smtplib.SMTPAuthenticationError):
        senden(_mail(), kennwort="falsch")
    assert versuche and uebergeben == []


def test_ohne_schalter_gilt_das_ad_kennwort_nicht(listener):
    senden, versuche, werte, uebergeben = listener
    werte["NOTBETRIEB_SMTP"] = False
    with pytest.raises(smtplib.SMTPAuthenticationError):
        senden(_mail())
    assert versuche == [] and uebergeben == []


def test_vor_dem_notfall_nur_probegruppe(listener):
    senden, _, werte, uebergeben = listener
    werte["NOTBETRIEB_PROBEGRUPPE"] = []
    with pytest.raises(smtplib.SMTPDataError) as fehler:
        senden(_mail())
    assert fehler.value.smtp_code == 550
    assert uebergeben == [] and _lokal("a@firma.at", "sentitems") == []
    werte["NOTBETRIEB_AUSGERUFEN"] = True
    senden(_mail())
    assert uebergeben


def test_nur_mit_senderecht(listener):
    senden, _, _, uebergeben = listener
    for von in ("team@firma.at", "b@firma.at", "Zwei <a@firma.at>, b@firma.at"):   # nur Vollzugriff / fremd / zwei
        with pytest.raises(smtplib.SMTPDataError) as fehler:
            senden(_mail(von=von), von="a@firma.at", an=["kunde@extern.de"])
        fehler.value.smtp_error.decode("ascii")          # RFC 5321: nur ASCII (Outlook zeigte „fÃ¼r“)
        if von == "team@firma.at":
            assert fehler.value.smtp_error == b"5.7.1 Kein Senderecht fuer team@firma.at"
    assert uebergeben == []
    # „Im Auftrag“: From bleibt, Sender = die angemeldete Person — auch wenn das
    # Programm einen anderen Sender mitschickt.
    m = _mail(von="chef@firma.at", an="kunde@extern.de")
    m["Sender"] = "jemand@extern.de"
    senden(m, von="chef@firma.at", an=["kunde@extern.de"])
    (_, _, inhalt), = uebergeben
    kopf = inhalt.split(b"\r\n\r\n", 1)[0]
    assert b"Sender: a@firma.at" in kopf and b"jemand@extern.de" not in kopf


def test_ohne_direktversand_nichts_nach_aussen(listener, monkeypatch):
    import direktversand
    senden, _, _, uebergeben = listener
    monkeypatch.setattr(direktversand, "aktiv", lambda: False)
    with pytest.raises(smtplib.SMTPDataError) as fehler:
        senden(_mail())
    # 550 vorab, nicht 451 aus der Zustellung — sonst versucht das Programm es endlos neu.
    assert fehler.value.smtp_code == 550
    assert uebergeben == [] and _lokal("b@firma.at", "inbox") == []


def test_append_der_gesendet_kopie_ohne_doppel(listener, monkeypatch):
    """Outlook/iOS legen nach dem Senden per IMAP APPEND unter „Gesendet“ ab.
    Das Gateway hat die Kopie schon — angenommen, aber nicht doppelt."""
    import asyncio
    import imaplib
    import threading
    import notbetrieb_anmeldung
    import notbetrieb_imap as ni
    import notbetrieb_rechte
    senden, _, werte, _ = listener
    werte["NOTBETRIEB_IMAP"] = True
    with notbetrieb._conn() as conn:
        conn.execute("INSERT INTO ordner (postfach, id, eltern, name, wk) VALUES "
                     "('a@firma.at','ord-s','root','Gesendete Elemente','sentitems')")
    monkeypatch.setattr(notbetrieb_anmeldung, "anmelden", lambda n, k, q="": {
        "ok": True, "benutzer": n, "postfach": "a@firma.at", "zugriff": notbetrieb_rechte.zugriff("a@firma.at")})
    senden(_mail())
    schleife = asyncio.new_event_loop()
    server = schleife.run_until_complete(ni.starten(None, 0, "127.0.0.1", ohne_tls_nur_test=True))
    t = threading.Thread(target=schleife.run_forever, daemon=True)
    t.start()
    try:
        c = imaplib.IMAP4("127.0.0.1", server.sockets[0].getsockname()[1], timeout=10)
        c.login("a.ad@firma.local", "x")
        gross = _mail().as_bytes() + b"x" * (200 * 1024)      # größer als ein Anmelde-Literal
        assert c.append('"Gesendete Elemente"', "(\\Seen)", None, gross)[0] == "OK"
        assert len(_lokal("a@firma.at", "sentitems")) == 1
        fremd = _mail().as_bytes().replace(b"outlook-1@", b"nie-gesendet@")
        assert c.append('"Gesendete Elemente"', None, None, fremd)[0] == "NO"
        assert c.append("INBOX", None, None, _mail().as_bytes())[0] == "NO"
        # Nach dem Rückspielen trägt die Zeile die Ordner-Kennung und gilt als
        # Exchange-Mail — Outlook legt später (oder erneut) ab und bekam bis
        # dahin NO: eigene Kopie behalten (doppelt), 800CCCD2 bei jedem Abgleich.
        with notbetrieb._conn() as conn:
            conn.execute("UPDATE mails SET ordner='ord-s', herkunft='exo' WHERE postfach='a@firma.at' "
                         "AND ordner='sentitems'")
        assert c.append('"Gesendete Elemente"', "(\\Seen)", None, _mail().as_bytes())[0] == "OK"
        with notbetrieb._conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM mails WHERE postfach='a@firma.at' "
                                "AND ordner IN ('ord-s','sentitems')").fetchone()[0] == 1
        c.logout()
    finally:
        schleife.call_soon_threadsafe(server.close)
        schleife.call_soon_threadsafe(schleife.stop)
        t.join(timeout=5)
