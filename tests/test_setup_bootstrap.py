"""Ersteinrichtungs-Seite (Port 80, vor dem ersten Zertifikat) in main.py.

Die schlanke Bootstrap-Seite muss dieselben drei TLS-Wege anbieten wie der
Setup-Assistent: Let's Encrypt HTTP-01, PFX-Import und DNS-01 (manuell). Sonst
landet ein Betreiber, der Port 80 nicht öffnen will, in einer Sackgasse.

⚠️ Der PFX-Import ruft bei Erfolg _schedule_self_restart() — im Test wird der
Neustart abgefangen, sonst beendet os._exit() den Testprozess.
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

import main  # noqa: E402


def _pfx(host: str, password: bytes = b"") -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    jetzt = _dt.datetime.now(_dt.timezone.utc)          # kein festes Datum (Zeitbombe)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(jetzt - _dt.timedelta(days=1))
            .not_valid_after(jetzt + _dt.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), False)
            .sign(key, hashes.SHA256()))
    enc = (serialization.BestAvailableEncryption(password) if password
           else serialization.NoEncryption())
    return pkcs12.serialize_key_and_certificates(b"t", key, cert, None, enc)


# ── Die Seite bietet alle drei Wege ─────────────────────────────────────────────

def test_bootstrap_seite_bietet_alle_drei_wege():
    seite = main._setup_page(hostname="sig.example.com", email="a@b.de").decode()
    for marker in [
        "Wo soll das TLS-Zertifikat herkommen?",
        "1 · Let's Encrypt über HTTP", "2 · Vorhandenes Zertifikat",
        "3 · Let's Encrypt über DNS-01",
        'name="action" value="letsencrypt"', 'name="action" value="pfx"',
        'name="action" value="dns01-start"', 'enctype="multipart/form-data"',
        'accept=".pfx,.p12"', 'name="pfx_force"',
    ]:
        assert marker in seite, f"Bootstrap-Seite fehlt: {marker}"
    # Alle drei Wege sind <details>-Kästen, Weg 1 standardmäßig offen.
    assert seite.count("<details") == 3
    assert seite.count("<details open>") == 1


def test_bootstrap_dns01_record_block_klappt_sektion_auf():
    block = main._dns01_record_block("_acme-challenge.h.de", "TXTWERT123")
    seite = main._setup_page(hostname="h.de", dns01_block=block).decode()
    assert "_acme-challenge.h.de" in seite and "TXTWERT123" in seite
    assert 'name="action" value="dns01-finish"' in seite
    assert "<details open>" in seite          # DNS-01-Bereich automatisch offen


def test_bootstrap_seite_maskiert_html():
    seite = main._setup_page(hostname='"><script>boom', email="").decode()
    assert "<script>boom" not in seite
    assert "&lt;script&gt;" in seite


# ── Multipart-Parser ────────────────────────────────────────────────────────────

def test_multipart_parser_liefert_felder_und_binaerdatei():
    b = "GRENZE"
    body = (
        f'--{b}\r\nContent-Disposition: form-data; name="hostname"\r\n\r\nsig.example.com\r\n'
        f'--{b}\r\nContent-Disposition: form-data; name="pfx_file"; filename="c.pfx"\r\n'
        f'Content-Type: application/octet-stream\r\n\r\n'
    ).encode() + b"\x00\x01\xff\xfe binaer \x00" + f"\r\n--{b}--\r\n".encode()
    felder, dateien = main._parse_multipart(f"multipart/form-data; boundary={b}", body)
    assert felder["hostname"] == "sig.example.com"
    assert dateien["pfx_file"] == b"\x00\x01\xff\xfe binaer \x00"   # Bytes unversehrt


# ── PFX-Import über den Bootstrap-Weg ────────────────────────────────────────────

@pytest.fixture
def _kein_neustart(monkeypatch):
    aufrufe = []
    monkeypatch.setattr(main, "_schedule_self_restart", lambda: aufrufe.append(1))
    return aufrufe


def test_bootstrap_pfx_import_schreibt_cert_und_key_600(tmp_path, monkeypatch, _kein_neustart):
    import config
    monkeypatch.setattr(config, "SMTP_TLS_CERT", str(tmp_path / "cert.pem"))
    monkeypatch.setattr(config, "SMTP_TLS_KEY", str(tmp_path / "key.pem"))
    msg = main._bootstrap_import_pfx("gw.example.com", _pfx("gw.example.com"), "")
    assert "class=\"ok\"" in msg                       # Erfolgsmeldung
    assert _kein_neustart == [1]                        # Neustart wurde ausgelöst
    key = tmp_path / "key.pem"
    assert (tmp_path / "cert.pem").read_bytes().startswith(b"-----BEGIN CERTIFICATE")
    assert (key.stat().st_mode & 0o777) == 0o600


def test_bootstrap_pfx_mit_passwort(tmp_path, monkeypatch, _kein_neustart):
    import config
    monkeypatch.setattr(config, "SMTP_TLS_CERT", str(tmp_path / "cert.pem"))
    monkeypatch.setattr(config, "SMTP_TLS_KEY", str(tmp_path / "key.pem"))
    msg = main._bootstrap_import_pfx("gw.example.com", _pfx("gw.example.com", b"geheim"), "geheim")
    assert "class=\"ok\"" in msg and _kein_neustart == [1]


def test_bootstrap_pfx_falscher_host_lehnt_ab_und_startet_nicht_neu(tmp_path, monkeypatch, _kein_neustart):
    import config
    monkeypatch.setattr(config, "SMTP_TLS_CERT", str(tmp_path / "cert.pem"))
    monkeypatch.setattr(config, "SMTP_TLS_KEY", str(tmp_path / "key.pem"))
    msg = main._bootstrap_import_pfx("anderer.example.com", _pfx("gw.example.com"), "")
    assert "class=\"err\"" in msg
    assert _kein_neustart == []                         # KEIN Neustart bei Fehler
    assert not (tmp_path / "cert.pem").exists()         # nichts geschrieben


def test_bootstrap_pfx_ohne_datei(monkeypatch, _kein_neustart):
    msg = main._bootstrap_import_pfx("gw.example.com", b"", "")
    assert "class=\"err\"" in msg and _kein_neustart == []


def test_bootstrap_pfx_ohne_hostname(monkeypatch, _kein_neustart):
    msg = main._bootstrap_import_pfx("", _pfx("gw.example.com"), "")
    assert "class=\"err\"" in msg and _kein_neustart == []


def test_bootstrap_pfx_mismatch_ohne_uebergehen_blockt(tmp_path, monkeypatch, _kein_neustart):
    import config
    monkeypatch.setattr(config, "SMTP_TLS_CERT", str(tmp_path / "cert.pem"))
    monkeypatch.setattr(config, "SMTP_TLS_KEY", str(tmp_path / "key.pem"))
    msg = main._bootstrap_import_pfx("anderer.example.com", _pfx("gw.example.com"), "")
    assert "class=\"err\"" in msg and _kein_neustart == []
    assert not (tmp_path / "cert.pem").exists()


def test_bootstrap_pfx_mismatch_mit_uebergehen(tmp_path, monkeypatch, _kein_neustart):
    import config
    monkeypatch.setattr(config, "SMTP_TLS_CERT", str(tmp_path / "cert.pem"))
    monkeypatch.setattr(config, "SMTP_TLS_KEY", str(tmp_path / "key.pem"))
    msg = main._bootstrap_import_pfx("anderer.example.com", _pfx("gw.example.com"), "",
                                     allow_mismatch=True)
    assert "class=\"ok\"" in msg and _kein_neustart == [1]      # importiert + Neustart
    assert (tmp_path / "cert.pem").exists()


# ── DNS-01: Fehlerpfade ohne Netz ───────────────────────────────────────────────

def test_bootstrap_dns01_start_ohne_hostname():
    msg, block = main._bootstrap_dns01_start("", "a@b.de", False)
    assert "class=\"err\"" in msg and block == ""
