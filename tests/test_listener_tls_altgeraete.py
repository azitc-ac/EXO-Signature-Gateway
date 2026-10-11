"""Relay-Geräte bekommen am Listener TLS 1.0/1.1 und statisches RSA.

ANLASS (2026-10-11)
-------------------
Der Listener nutzte Pythons Vorgaben. Unter OpenSSL 3 heisst das: kein TLS
1.0/1.1, und unter OpenSSL 3.5 (im Abbild) keine Suite mit statischem RSA. Ein
Relay-Gerät, das STARTTLS versucht — angeboten wird es —, scheiterte am
Handshake und fiel in der Regel nicht auf Klartext zurück. Dass STARTTLS für
Relay-Geräte keine Pflicht ist (test_relay_tls.py), half ihm damit nicht.
Gemessen am SMTP-Relay, das denselben Unterbau hat.

Echte Handshakes gegen baue_listener_kontext(). Die Gegenrichtung gehört dazu:
Exchange und moderne Clients bekommen weiter TLS 1.3 bzw. Forward Secrecy, und
ausgehende Verbindungen bleiben streng.
"""
import socket
import ssl
import threading
import warnings
from pathlib import Path

import pytest

import smtp_cert
from test_smtp_sni import _mk_cert

APP = Path(__file__).resolve().parent.parent / "app"


def _handshake(server_ctx: ssl.SSLContext, client_ctx: ssl.SSLContext,
               sni: str | None = None) -> tuple[str, str]:
    a, b = socket.socketpair()

    def server():
        try:
            with server_ctx.wrap_socket(a, server_side=True) as s:
                s.recv(1)
        except (ssl.SSLError, OSError):
            pass      # der Client meldet den Fehler, der zählt
        finally:
            a.close()

    th = threading.Thread(target=server, daemon=True)
    th.start()
    try:
        with client_ctx.wrap_socket(b, server_hostname=sni) as c:
            ergebnis = (c.version(), c.cipher()[0])
            c.send(b"x")
        return ergebnis
    finally:
        b.close()
        th.join(timeout=5)


def _client(ciphers: str | None = None, hoechstens=None) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        if ciphers:
            ctx.set_ciphers(ciphers)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)   # TLSv1 ist absichtlich
            if hoechstens is not None:
                ctx.minimum_version = ssl.TLSVersion.TLSv1
                ctx.maximum_version = hoechstens
    except (ssl.SSLError, ValueError) as e:
        pytest.skip(f"lokales OpenSSL kann diesen Client nicht bauen: {e}")
    if not ctx.get_ciphers():
        pytest.skip("lokales OpenSSL bietet für diesen Client keine Suite an")
    return ctx


@pytest.fixture
def listener(tmp_path):
    return smtp_cert.baue_listener_kontext(
        [_mk_cert(tmp_path, "sig", "sig.firma.de", ["sig.firma.de"])])


def test_hp_geraet_nur_tls_rsa_with_aes_128_cbc_sha256(listener):
    client = _client("AES128-SHA256:@SECLEVEL=0", ssl.TLSVersion.TLSv1_2)
    assert _handshake(listener, client) == ("TLSv1.2", "AES128-SHA256")


@pytest.mark.parametrize("version, name", [
    (ssl.TLSVersion.TLSv1, "TLSv1"),
    (ssl.TLSVersion.TLSv1_1, "TLSv1.1"),
])
def test_tls_1_0_und_1_1(listener, version, name):
    client = _client("DEFAULT:@SECLEVEL=0", version)
    assert _handshake(listener, client)[0] == name


def test_tls_1_0_nur_statisches_rsa(listener):
    client = _client("AES128-SHA:@SECLEVEL=0", ssl.TLSVersion.TLSv1)
    assert _handshake(listener, client) == ("TLSv1", "AES128-SHA")


def test_altgeraet_auch_am_zweiten_zert_per_sni(tmp_path):
    """Mit mehreren Zerts tauscht der SNI-Callback den Kontext — auch der
    zweite muss das Altgerät bedienen."""
    ctx = smtp_cert.baue_listener_kontext([
        _mk_cert(tmp_path, "sig", "sig.firma.de", ["sig.firma.de"]),
        _mk_cert(tmp_path, "mail", "*.firma.de", ["*.firma.de"]),
    ])
    client = _client("AES128-SHA:@SECLEVEL=0", ssl.TLSVersion.TLSv1)
    assert _handshake(ctx, client, sni="mx.firma.de") == ("TLSv1", "AES128-SHA")


def _mk_ec_cert(tmp_path, cn):
    """EC-Zertifikat, wie certbot es seit 2.0 standardmässig ausstellt."""
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .sign(key, hashes.SHA256()))
    cp, kp = tmp_path / "ec-cert.pem", tmp_path / "ec-key.pem"
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                     serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    return str(cp), str(kp)


def test_ec_zert_tls_1_0_ja_statisches_rsa_nein(tmp_path):
    """Mit einem EC-Zertifikat geht TLS 1.0 über ECDHE-ECDSA; ein Gerät, das
    NUR statisches RSA kann, bleibt draussen — dafür braucht es ein
    RSA-Zertifikat. Hält die im Changelog genannte Grenze fest."""
    ctx = smtp_cert.baue_listener_kontext([_mk_ec_cert(tmp_path, "sig.firma.de")])
    version, suite = _handshake(ctx, _client("DEFAULT:@SECLEVEL=0", ssl.TLSVersion.TLSv1))
    assert version == "TLSv1" and suite.startswith("ECDHE-ECDSA-"), suite
    with pytest.raises(ssl.SSLError):
        _handshake(ctx, _client("AES128-SHA256:@SECLEVEL=0", ssl.TLSVersion.TLSv1_2))


def test_moderner_client_bekommt_tls_1_3(listener):
    assert _handshake(listener, _client())[0] == "TLSv1.3"


def test_client_der_alles_kann_bekommt_forward_secrecy(listener):
    """Bietet ein Client bis TLS 1.2 auch die alten Suites an, entscheidet die
    Serverpräferenz — ECDHE mit AEAD, nicht kRSA/CBC. So verhält sich Exchange."""
    version, suite = _handshake(listener, _client("ALL:@SECLEVEL=0", ssl.TLSVersion.TLSv1_2))
    assert version == "TLSv1.2"
    assert suite.startswith("ECDHE-") and ("GCM" in suite or "CHACHA20" in suite), suite


def test_keine_null_oder_anonymen_suites(listener):
    namen = [c["name"] for c in listener.get_ciphers()]
    assert namen
    assert not [n for n in namen if "NULL" in n or n.startswith(("ADH-", "AECDH-"))
                or "RC4" in n or "DES" in n], namen
    assert listener.options & ssl.OP_CIPHER_SERVER_PREFERENCE


def test_scheiternde_lockerung_kostet_den_listener_nicht_das_zert(tmp_path, monkeypatch):
    """Geht die Lockerung schief, bleibt es bei den Vorgaben — der Listener
    startet trotzdem MIT Zertifikat (sonst liefe er ohne STARTTLS)."""
    monkeypatch.setattr(smtp_cert, "LISTENER_CIPHERS", "GIBTESNICHT")
    ctx = smtp_cert.baue_listener_kontext(
        [_mk_cert(tmp_path, "sig", "sig.firma.de", ["sig.firma.de"])])
    assert ctx is not None
    assert _handshake(ctx, _client())[0] == "TLSv1.3"


def test_lockerung_nur_am_listener():
    """Ausgehende Verbindungen (Smarthost, 587, Graph, ACME) dürfen weder die
    Stufe senken noch eine eigene Liste setzen."""
    treffer = sorted(p.name for p in APP.rglob("*.py")
                     if "SECLEVEL" in p.read_text(encoding="utf-8", errors="replace"))
    assert treffer == ["smtp_cert.py"], treffer
