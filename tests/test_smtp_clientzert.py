"""Messschalter SMTP_CLIENTZERT_BEOBACHTEN: Client-Zertifikat anfordern, nicht verlangen.

Untersuchung (11.11.2026): Legt Exchange Online beim Einliefern ein
Client-Zertifikat vor? Dann könnte das Gateway Exchange daran statt an der
Quell-IP erkennen. Geprüft am echten Listener:
1. Schalter an, Client MIT Zertifikat → Protokoll nennt es, Mail kommt an,
2. Schalter an, Client OHNE Zertifikat → Mail kommt trotzdem an (nur angefordert),
3. Schalter aus → es wird gar nichts angefordert.
"""
import datetime
import logging
import smtplib
import socket
import ssl
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import main        # noqa: E402
import smtp_cert   # noqa: E402


def _zert(name, aussteller_key=None, aussteller_name=None, ca=False):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Test " + name)])
    jetzt = datetime.datetime.now(datetime.timezone.utc)
    b = (x509.CertificateBuilder().subject_name(subj)
         .issuer_name(aussteller_name or subj).public_key(key.public_key())
         .serial_number(x509.random_serial_number())
         .not_valid_before(jetzt - datetime.timedelta(days=1))
         .not_valid_after(jetzt + datetime.timedelta(days=1))
         .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    return key, b.sign(aussteller_key or key, hashes.SHA256()), subj


def _pem(tmp_path, datei, key, cert):
    from cryptography.hazmat.primitives import serialization
    (tmp_path / f"{datei}.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / f"{datei}.key").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return str(tmp_path / f"{datei}.crt"), str(tmp_path / f"{datei}.key")


class _Merker:
    def __init__(self):
        self.anzahl = 0

    async def handle_DATA(self, server, session, envelope):
        self.anzahl += 1
        return "250 OK"


class _Zeilen(logging.Handler):
    def __init__(self):
        super().__init__()
        self.zeilen = []

    def emit(self, record):
        self.zeilen.append(record.getMessage())


@pytest.fixture
def aufbau(monkeypatch, tmp_path):
    import certifi
    import settings_store
    werte = {"SMTP_CLIENTZERT_BEOBACHTEN": True}
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))
    import smtp_relay
    monkeypatch.setattr(smtp_relay, "ist_relay_quelle", lambda ip: False)
    ca_key, ca_cert, ca_name = _zert("Test-CA", ca=True)
    ca_datei, _ = _pem(tmp_path, "ca", ca_key, ca_cert)
    monkeypatch.setattr(certifi, "where", lambda: ca_datei)   # „öffentliche“ CAs = Test-CA
    srv = _pem(tmp_path, "server", *_zert("localhost")[:2])
    cl_key, cl_cert, _ = _zert("mail.protection.outlook.com", ca_key, ca_name)
    client = _pem(tmp_path, "client", cl_key, cl_cert)
    zeilen = _Zeilen()
    logging.getLogger("main").addHandler(zeilen)
    logging.getLogger("main").setLevel(logging.INFO)

    def starten():
        merker = _Merker()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        ctx = smtp_cert.baue_listener_kontext([srv])
        ctrl = main._LenientController(merker, hostname="127.0.0.1", port=port,
                                       tls_context=ctx, require_starttls=True)
        ctrl.start()
        return ctrl, port, merker, ctx

    yield starten, werte, client, zeilen
    logging.getLogger("main").removeHandler(zeilen)


def _senden(port, client=None):
    k = ssl.create_default_context()
    k.check_hostname = False
    k.verify_mode = ssl.CERT_NONE
    if client:
        k.load_cert_chain(*client)
    c = smtplib.SMTP()
    c._host = "localhost"
    c.connect("127.0.0.1", port)
    c.ehlo("exchange.test")
    c.starttls(context=k)
    c.ehlo("exchange.test")
    c.sendmail("a@firma.at", ["b@extern.de"], b"Subject: x\r\n\r\nx\r\n")
    c.quit()


def test_vorgelegtes_zertifikat_wird_protokolliert(aufbau):
    starten, _w, client, zeilen = aufbau
    ctrl, port, merker, _ = starten()
    try:
        _senden(port, client)
    finally:
        ctrl.stop()
    assert merker.anzahl == 1
    treffer = [z for z in zeilen.zeilen if z.startswith("SMTP-TLS von")]
    assert treffer and "CN=mail.protection.outlook.com" in treffer[0], zeilen.zeilen
    # Seriennummer und Fingerabdruck — nur die machen einen Vergleich eindeutig
    import hashlib
    from cryptography import x509
    cert = x509.load_pem_x509_certificate(Path(client[0]).read_bytes())
    from cryptography.hazmat.primitives.serialization import Encoding
    fp = hashlib.sha1(cert.public_bytes(Encoding.DER)).hexdigest().upper()
    assert f"SHA1={fp}" in treffer[0], treffer[0]
    assert f"Serial={cert.serial_number:X}" in treffer[0], treffer[0]


def test_wurzel_des_exo_clientzertifikats_liegt_bei():
    """Die Wurzel fehlt in certifi; ohne die mitgelieferte Datei scheiterte die
    Prüfung des echten EXO-Zertifikats (Raspi, 11.11.2026)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    datei = WURZEL / "app" / "smtp_clientzert_wurzeln.pem"
    cert = x509.load_pem_x509_certificate(datei.read_bytes())
    assert cert.fingerprint(hashes.SHA1()).hex().upper() == \
        "A8985D3A65E5E5C4B2D7D66D40C6DD2FB19C5436"


def test_ohne_zertifikat_kommt_die_mail_trotzdem_an(aufbau):
    starten, _w, _client, zeilen = aufbau
    ctrl, port, merker, _ = starten()
    try:
        _senden(port)
    finally:
        ctrl.stop()
    assert merker.anzahl == 1
    assert any("kein Client-Zertifikat" in z for z in zeilen.zeilen), zeilen.zeilen


def test_schalter_aus_fordert_nichts_an(aufbau):
    starten, werte, _client, zeilen = aufbau
    werte["SMTP_CLIENTZERT_BEOBACHTEN"] = False
    ctrl, port, merker, ctx = starten()
    try:
        assert ctx.verify_mode == ssl.CERT_NONE
        _senden(port)
    finally:
        ctrl.stop()
    assert merker.anzahl == 1
    assert not [z for z in zeilen.zeilen if z.startswith("SMTP-TLS von")]
