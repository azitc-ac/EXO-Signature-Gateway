"""Separates SMTP-Listener-Zertifikat: Validierung, Aktivierung, Rückfall.

Kernvertrag: ein Zert wird NUR gespeichert, wenn es lesbar ist UND der Schlüssel
dazugehört — sonst startet der Listener beim nächsten Neustart ohne TLS.
"""
import datetime

import pytest

import smtp_cert


def _selfsigned(cn="test.example.net"):
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    # Relative Gültigkeit (kein hartkodiertes Datum — sonst „Zeitbombe",
    # siehe tests/test_keine_zeitbomben.py).
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(cn)]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    return cert_pem, key_pem


def _pfx(cn="pfx.example.net", password=b"geheim"):
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import (
        pkcs12, BestAvailableEncryption, NoEncryption)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(cn)]), critical=False)
            .sign(key, hashes.SHA256()))
    enc = BestAvailableEncryption(password) if password else NoEncryption()
    return pkcs12.serialize_key_and_certificates(b"test", key, cert, None, enc)


@pytest.fixture(autouse=True)
def _sauber():
    smtp_cert.entfernen()
    yield
    smtp_cert.entfernen()


def test_speichern_und_info():
    assert not smtp_cert.aktiv()
    assert smtp_cert.pfade() is None
    c, k = _selfsigned("mail.example.net")
    smtp_cert.speichern(c, k)
    assert smtp_cert.aktiv()
    assert smtp_cert.pfade() == (str(smtp_cert.CERT), str(smtp_cert.KEY))
    info = smtp_cert.info()
    assert info["override"] is True
    assert "mail.example.net" in info["subject"]
    assert "mail.example.net" in info["san"]
    assert info["not_after"][:4].isdigit()      # ISO-Datum, Jahr voran


def test_schluessel_wird_600_geschrieben():
    c, k = _selfsigned()
    smtp_cert.speichern(c, k)
    assert oct(smtp_cert.KEY.stat().st_mode & 0o777) == "0o600"


def test_mismatch_wird_abgelehnt_und_nichts_geschrieben():
    c1, _ = _selfsigned("a.example.net")
    _, k2 = _selfsigned("b.example.net")     # fremder Schlüssel
    with pytest.raises(ValueError, match="gehören nicht zusammen"):
        smtp_cert.speichern(c1, k2)
    assert not smtp_cert.aktiv()              # Validierung VOR dem Schreiben


def test_kaputtes_pem_wird_abgelehnt():
    with pytest.raises(ValueError):
        smtp_cert.speichern(b"kein zertifikat", b"kein schluessel")
    assert not smtp_cert.aktiv()


def test_pfx_mit_passwort():
    smtp_cert.speichern_pfx(_pfx("mail.example.net", b"pw123"), "pw123")
    info = smtp_cert.info()
    assert info["override"] is True
    assert "mail.example.net" in info["subject"]
    assert oct(smtp_cert.KEY.stat().st_mode & 0o777) == "0o600"


def test_pfx_ohne_passwort():
    smtp_cert.speichern_pfx(_pfx("plain.example.net", None), "")
    assert smtp_cert.aktiv()


def test_pfx_falsches_passwort_wird_abgelehnt():
    with pytest.raises(ValueError):
        smtp_cert.speichern_pfx(_pfx("x.example.net", b"richtig"), "falsch")
    assert not smtp_cert.aktiv()


def test_entfernen_faellt_auf_gemeinsam_zurueck():
    c, k = _selfsigned()
    smtp_cert.speichern(c, k)
    assert smtp_cert.aktiv()
    assert smtp_cert.entfernen() is True
    assert not smtp_cert.aktiv()
    assert smtp_cert.pfade() is None
    assert smtp_cert.info()["override"] is False
    assert smtp_cert.entfernen() is False     # zweites Mal: nichts mehr da
