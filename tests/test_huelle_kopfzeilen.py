"""S/MIME-Hüllen tragen dieselben Kopfzeilen — auch Sender und Thread-Topic.

Gemessen 12.11.2026 im DEV-Tenant: Bei „Senden im Auftrag von“ kam die
signierte Mail ohne Sender-Kopf an; der Empfänger sah nur das geteilte
Postfach, nicht die Person. Geprüft mit ECHTER openssl-Signatur.
"""
import email
import subprocess

import huelle
import smime_signer
import smime_store

ROH = (b"From: Team <team@firma.at>\r\nSender: Max <max@firma.at>\r\n"
       b"To: erika@firma.at\r\nSubject: Test\r\nThread-Topic: Test\r\n"
       b"Thread-Index: AQHZabc=\r\nMessage-ID: <1@firma.at>\r\n"
       b"Date: Thu, 12 Nov 2026 10:00:00 +0000\r\n"
       b"Content-Type: text/plain\r\n\r\nHallo\r\n")


def test_liste_enthaelt_sender_und_unterhaltung():
    for kopf in ("Sender", "Thread-Topic", "Thread-Index", "From", "Subject"):
        assert kopf in huelle.KOPFZEILEN


def test_signierte_huelle_traegt_sender(monkeypatch, tmp_path):
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(key), "-out", str(crt), "-days", "1",
                    "-subj", "/CN=team@firma.at"], check=True, capture_output=True)
    monkeypatch.setattr(smime_store, "get_signing_paths", lambda s, allow_backup=False: (crt, key))
    monkeypatch.setattr(smime_store, "openssl_passin", lambda: ([], None))
    aus = smime_signer.sign(ROH, "team@firma.at")
    assert aus
    m = email.message_from_bytes(aus)
    assert m.get_content_type() == "multipart/signed"
    assert m["Sender"] == "Max <max@firma.at>"
    assert m["Thread-Topic"] == "Test" and m["Thread-Index"] == "AQHZabc="


def test_handler_huelle_bei_verschluesselung():
    import handler
    aussen = email.message_from_bytes(b"Content-Type: application/pkcs7-mime\r\n\r\nx")
    handler._restore_envelope_headers(aussen, email.message_from_bytes(ROH))
    assert aussen["Sender"] == "Max <max@firma.at>" and aussen["From"].startswith("Team")


def test_cms_huelle_nutzt_dieselbe_liste():
    from pathlib import Path
    quelle = (Path(huelle.__file__).parent / "cms_sign.py").read_text(encoding="utf-8")
    assert "huelle.KOPFZEILEN" in quelle


def test_protokollstufe_wirkt_sofort(monkeypatch):
    import logging
    import log_manager
    import settings_store
    wurzel = logging.getLogger()
    vorher = wurzel.level
    try:
        monkeypatch.setattr(settings_store, "get", lambda k, d=None: "DEBUG" if k == "LOG_LEVEL" else d)
        assert log_manager.stufe_anwenden() == "DEBUG" and wurzel.level == logging.DEBUG
        assert log_manager.stufe_anwenden("quatsch") == "INFO" and wurzel.level == logging.INFO
    finally:
        wurzel.setLevel(vorher)
