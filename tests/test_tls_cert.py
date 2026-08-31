"""TLS-Zertifikat-Import (PFX) und DNS-01-CSR.

Der Import ist sicherheitsrelevant: Er schreibt den privaten Schlüssel des
Listeners. Geprüft wird, dass der Schlüssel mit Rechten 600 landet, ein
Zertifikat gegen den Hostnamen geprüft wird (Wildcard eingeschlossen) und der
DNS-01-CSR den richtigen Namen trägt.
"""
import datetime as _dt
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

from cryptography import x509                                        # noqa: E402
from cryptography.x509.oid import NameOID                           # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization    # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec           # noqa: E402
from cryptography.hazmat.primitives.serialization import pkcs12    # noqa: E402

import tls_cert  # noqa: E402


def _selfsigned(host: str):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    # Gültigkeit relativ zu jetzt — kein fest verdrahtetes Datum (Zeitbombe).
    jetzt = _dt.datetime.now(_dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(jetzt - _dt.timedelta(days=1))
            .not_valid_after(jetzt + _dt.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), False)
            .sign(key, hashes.SHA256()))
    return key, cert


def _pfx(host: str, password: bytes = b"") -> bytes:
    key, cert = _selfsigned(host)
    enc = (serialization.BestAvailableEncryption(password) if password
           else serialization.NoEncryption())
    return pkcs12.serialize_key_and_certificates(b"t", key, cert, None, enc)


# ── host_matches ──────────────────────────────────────────────────────────────

def test_host_matches_exakt_und_wildcard():
    assert tls_cert.host_matches("gw.example.com", ["gw.example.com"])
    assert tls_cert.host_matches("gw.example.com", ["*.example.com"])
    assert not tls_cert.host_matches("a.b.example.com", ["*.example.com"])  # nur eine Ebene
    assert not tls_cert.host_matches("gw.example.com", ["other.example.com"])
    assert not tls_cert.host_matches("", ["gw.example.com"])


# ── install_pfx ───────────────────────────────────────────────────────────────

def test_install_pfx_schreibt_cert_und_key_600(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "SMTP_TLS_CERT", str(tmp_path / "cert.pem"))
    monkeypatch.setattr(config, "SMTP_TLS_KEY", str(tmp_path / "key.pem"))
    info = tls_cert.install_pfx(_pfx("gw.example.com"), "", "gw.example.com")
    assert "gw.example.com" in info["hostnames"]
    assert (tmp_path / "cert.pem").read_bytes().startswith(b"-----BEGIN CERTIFICATE")
    key = tmp_path / "key.pem"
    assert key.read_bytes().startswith(b"-----BEGIN PRIVATE KEY")
    assert (key.stat().st_mode & 0o777) == 0o600


def test_install_pfx_mit_passwort(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "SMTP_TLS_CERT", str(tmp_path / "cert.pem"))
    monkeypatch.setattr(config, "SMTP_TLS_KEY", str(tmp_path / "key.pem"))
    info = tls_cert.install_pfx(_pfx("gw.example.com", b"geheim"), "geheim", "gw.example.com")
    assert "gw.example.com" in info["hostnames"]


def test_install_pfx_lehnt_falschen_hostnamen_ab(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "SMTP_TLS_CERT", str(tmp_path / "cert.pem"))
    monkeypatch.setattr(config, "SMTP_TLS_KEY", str(tmp_path / "key.pem"))
    with pytest.raises(ValueError):
        tls_cert.install_pfx(_pfx("gw.example.com"), "", "anderer.example.com")
    assert not (tmp_path / "cert.pem").exists()  # nichts geschrieben


# ── DNS-01 CSR ────────────────────────────────────────────────────────────────

def test_dns01_csr_traegt_den_hostnamen():
    import tls_acme_dns
    key = ec.generate_private_key(ec.SECP256R1())
    csr_der = tls_acme_dns._build_csr(key, "gw.example.com")
    csr = x509.load_der_x509_csr(csr_der)
    san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert "gw.example.com" in san.value.get_values_for_type(x509.DNSName)
