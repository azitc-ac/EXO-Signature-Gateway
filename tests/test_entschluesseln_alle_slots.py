"""Entschlüsseln mit dem Schlüssel des Zertifikats, an das verschlüsselt wurde.

Nach einer Erneuerung ist der Standard-Slot das NEUE Zertifikat. Partner, die
noch das alte kennen, verschlüsseln weiter daran. Bis v1.12.126 versuchte das
Gateway nur den Standard-Slot — solche Post blieb verschlüsselt. Mit der
automatischen Verlängerung (v1.12.115) und dem Widerruf vor jeder Neuausstellung
ist das kein Randfall mehr."""
import email
import email.policy
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import smime_decrypt  # noqa: E402
import smime_store  # noqa: E402

EMAIL = "max.mustermann@firma.at"


def _slot(basis: Path, standard: bool):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jetzt = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.EMAIL_ADDRESS, EMAIL)])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(jetzt - timedelta(days=1)).not_valid_after(jetzt + timedelta(days=300))
            .sign(key, hashes.SHA256()))
    slot = cert.fingerprint(hashes.SHA256()).hex()[:16]
    d = basis / EMAIL / "certs" / slot
    d.mkdir(parents=True)
    (d / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (d / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                                  serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption()))
    if standard:
        (basis / EMAIL / "default").write_text(slot)
    return d / "cert.pem", slot


def _verschluesselt_an(cert: Path) -> bytes:
    klar = b"Content-Type: text/plain\r\n\r\nGeheimer Inhalt\r\n"
    r = subprocess.run(["openssl", "cms", "-encrypt", "-aes256", str(cert)],
                       input=klar, capture_output=True, check=True)
    return b"From: a@b.example\r\nTo: " + EMAIL.encode() + b"\r\nSubject: x\r\n" + r.stdout


@pytest.fixture
def ablage(tmp_path, monkeypatch):
    monkeypatch.setattr(smime_store, "SMIME_DIR", tmp_path)
    monkeypatch.setattr(smime_store, "schluessel_passwort", lambda: "")
    return tmp_path


def test_mail_an_das_alte_zertifikat_nach_erneuerung(ablage):
    alt, _ = _slot(ablage, standard=False)
    _neu, _ = _slot(ablage, standard=True)          # Erneuerung: neues ist Standard
    klar = smime_decrypt._decrypt_local(_verschluesselt_an(alt), EMAIL)
    assert klar is not None and b"Geheimer Inhalt" in klar


def test_mail_an_das_neue_und_an_ein_widerrufenes(ablage):
    alt, slot_alt = _slot(ablage, standard=True)
    neu, _ = _slot(ablage, standard=False)
    smime_store.widerruf_vermerken(EMAIL, slot_alt, "ersetzt", "test")
    assert b"Geheimer Inhalt" in smime_decrypt._decrypt_local(_verschluesselt_an(neu), EMAIL)
    # Widerrufen heisst nicht signieren — entschlüsseln geht weiter.
    assert b"Geheimer Inhalt" in smime_decrypt._decrypt_local(_verschluesselt_an(alt), EMAIL)


def test_fremdes_zertifikat_bleibt_verschlossen(ablage, tmp_path_factory):
    _slot(ablage, standard=True)
    fremd_basis = tmp_path_factory.mktemp("fremd")
    fremd, _ = _slot(fremd_basis, standard=True)
    assert smime_decrypt._decrypt_local(_verschluesselt_an(fremd), EMAIL) is None
