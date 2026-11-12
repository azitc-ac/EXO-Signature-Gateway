"""PROXY-Protokoll an Port 25: Ursprungsadresse hinter einer Firewall (v1.11.14).

Ein lokal betriebenes Gateway sah Exchange Online nur als Adresse der Firewall
(Source-NAT) und wies jede Mail mit 554 ab. Geprüft am ECHTEN Listener
(main._LenientSMTP auf einem freien Port), nicht an Attrappen:

1. Von einer eingetragenen Adresse gilt die Ursprungsadresse aus dem Vorspann,
2. auch nach STARTTLS (aiosmtpd legt dabei eine neue Sitzung an),
3. eine eingetragene Adresse ohne Vorspann wird nicht bedient,
4. von nicht eingetragenen Adressen wird ein Vorspann NICHT geglaubt,
5. ohne Eintrag bleibt alles wie bisher.
"""
import asyncio
import datetime
import logging
import smtplib
import socket
import ssl
import sys
import time
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import main          # noqa: E402
import smtp_proxy    # noqa: E402
from handler import SignatureHandler   # noqa: E402

URSPRUNG = "52.100.20.30"          # eine Adresse aus Microsofts Bereich


class _Merker:
    handle_PROXY = SignatureHandler.handle_PROXY

    def __init__(self):
        self.peers = []

    async def handle_DATA(self, server, session, envelope):
        self.peers.append(session.peer[0])
        return "250 OK"


def _tls_kontext(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    jetzt = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(jetzt - datetime.timedelta(days=1))
            .not_valid_after(jetzt + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    (tmp_path / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "k.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    return ctx


@pytest.fixture
def listener(monkeypatch, tmp_path):
    import settings_store
    werte = {"SMTP_PROXY_QUELLEN": []}
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))
    import smtp_relay
    monkeypatch.setattr(smtp_relay, "ist_relay_quelle", lambda ip: False)
    merker = _Merker()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    ctrl = main._LenientController(merker, hostname="127.0.0.1", port=port,
                                   tls_context=_tls_kontext(tmp_path),
                                   require_starttls=False)
    ctrl.start()
    yield port, werte, merker
    ctrl.stop()


class _MitVorspann(smtplib.SMTP):
    vorspann = b""

    def _get_socket(self, host, port, timeout):
        sock = super()._get_socket(host, port, timeout)
        if self.vorspann:
            sock.sendall(self.vorspann)
        return sock


def _senden(port, vorspann=b"", starttls=False):
    c = _MitVorspann()
    c.vorspann = vorspann
    c._host = "localhost"         # sonst verweigert ssl den leeren server_hostname
    c.connect("127.0.0.1", port)
    c.ehlo("exchange.test")
    if starttls:
        kontext = ssl.create_default_context()
        kontext.check_hostname = False
        kontext.verify_mode = ssl.CERT_NONE
        c.starttls(context=kontext)
        c.ehlo("exchange.test")
    c.sendmail("a@firma.at", ["b@extern.de"], b"Subject: x\r\n\r\nx\r\n")
    c.quit()


V1 = f"PROXY TCP4 {URSPRUNG} 10.99.0.26 40001 25\r\n".encode()


@pytest.mark.parametrize("starttls", [False, True], ids=["klartext", "starttls"])
def test_eingetragener_proxy_liefert_die_ursprungsadresse(listener, starttls):
    port, werte, merker = listener
    werte["SMTP_PROXY_QUELLEN"] = ["127.0.0.1"]
    _senden(port, V1, starttls=starttls)
    assert merker.peers == [URSPRUNG]


def test_eingetragener_proxy_ohne_vorspann_wird_nicht_bedient(listener):
    port, werte, merker = listener
    werte["SMTP_PROXY_QUELLEN"] = ["127.0.0.0/8"]
    with pytest.raises((smtplib.SMTPException, OSError)):
        _senden(port)
    assert merker.peers == []


def _proxy_warnungen(caplog):
    return [s for s in caplog.records
            if s.levelno == logging.WARNING and "ohne gültigen PROXY-Vorspann" in s.getMessage()]


def test_fehlender_vorspann_steht_mit_abhilfe_im_protokoll(listener, caplog):
    """Exchange sieht nur „Connection closed by remote host“ (12.11.2026) — das
    Protokoll muss Ursache und Abhilfe nennen."""
    caplog.set_level(logging.INFO)
    port, werte, _merker = listener
    werte["SMTP_PROXY_QUELLEN"] = ["127.0.0.0/8"]
    with pytest.raises((smtplib.SMTPException, OSError)):
        _senden(port)
    for _ in range(50):                      # connection_lost läuft im Listener-Thread
        if _proxy_warnungen(caplog):
            break
        time.sleep(0.05)
    zeilen = _proxy_warnungen(caplog)
    assert len(zeilen) == 1
    assert "127.0.0.1" in zeilen[0].getMessage()
    assert "Vorgeschaltete Firewall (PROXY-Protokoll)" in zeilen[0].getMessage()


@pytest.mark.parametrize("starttls", [False, True], ids=["klartext", "starttls"])
def test_mit_vorspann_keine_proxy_warnung(listener, caplog, starttls):
    caplog.set_level(logging.INFO)
    port, werte, merker = listener
    werte["SMTP_PROXY_QUELLEN"] = ["127.0.0.1"]
    _senden(port, V1, starttls=starttls)
    time.sleep(0.3)
    assert merker.peers == [URSPRUNG]
    assert _proxy_warnungen(caplog) == []


def test_vorspann_von_fremder_adresse_wird_nicht_geglaubt(listener):
    port, werte, merker = listener
    werte["SMTP_PROXY_QUELLEN"] = ["10.99.0.1"]       # nicht der Testclient
    try:
        _senden(port, V1)
    except (smtplib.SMTPException, OSError):
        pass
    assert URSPRUNG not in merker.peers


def test_ohne_eintrag_wie_bisher(listener):
    port, _werte, merker = listener
    _senden(port)
    assert merker.peers == ["127.0.0.1"]


@pytest.mark.parametrize("eingabe,erwartet", [
    ("10.99.0.1", ["10.99.0.1/32"]),
    ("10.99.0.1, 10.99.0.0/24\n10.99.0.1", ["10.99.0.1/32", "10.99.0.0/24"]),
    (["2001:db8::1"], ["2001:db8::1/128"]),
    ("", []),
])
def test_normalisieren(eingabe, erwartet):
    assert smtp_proxy.normalisieren(eingabe) == erwartet


@pytest.mark.parametrize("eingabe", ["10.99.0", "firewall.local", "10.99.0.1/40"])
def test_normalisieren_lehnt_unsinn_ab(eingabe):
    with pytest.raises(ValueError, match="Keine gültige"):
        smtp_proxy.normalisieren(eingabe)


def test_speichern_lehnt_ungueltige_angabe_ab(monkeypatch):
    from fastapi.testclient import TestClient
    import settings_store
    from webui import deps
    from webui.app import app
    gespeichert = {}
    monkeypatch.setattr(settings_store, "update", lambda d: gespeichert.update(d))
    app.dependency_overrides[deps._require_admin] = lambda: "admin"
    try:
        c = TestClient(app)
        r = c.post("/settings", json={"SMTP_PROXY_QUELLEN": ["10.99.0"]})
        assert r.status_code == 400 and "10.99.0" in r.json()["error"]
        assert "SMTP_PROXY_QUELLEN" not in gespeichert
        r = c.post("/settings", json={"SMTP_PROXY_QUELLEN": ["10.99.0.1"]})
        assert r.status_code == 200 and gespeichert["SMTP_PROXY_QUELLEN"] == ["10.99.0.1/32"]
    finally:
        app.dependency_overrides.pop(deps._require_admin, None)
