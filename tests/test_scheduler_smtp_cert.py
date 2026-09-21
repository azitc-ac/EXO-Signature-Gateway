"""Das separate SMTP-Listener-Zert fällt unter die Ablaufkontrolle.

Audit-Fund ④: Der Listener serviert ein hinterlegtes separates Zert bevorzugt, es
hat aber keinen Auto-Renew, und Scheduler/Health prüften nur das gemeinsame Zert
→ ein separates lief STILL ab. `_check_smtp_cert` alarmiert jetzt bei nahendem
Ablauf. Der Test schlägt fehl, wenn die Prüfung wieder entfällt.
"""
import datetime

import pytest

import scheduler
import smtp_cert


def _cert_pem(days: int) -> bytes:
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mail.example")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("mail.example")]), False)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM)


@pytest.fixture
def umgebung(monkeypatch):
    alerts = []
    import notification
    monkeypatch.setattr(notification, "send_le_expiry_alert",
                        lambda domain, days, expiry: alerts.append((domain, days)))
    monkeypatch.setattr(scheduler.settings_store, "get",
                        lambda k, *a, **kw: {"LE_RENEW_DAYS": 14}.get(k))
    return alerts


def test_alarm_wenn_separates_zert_bald_ablaeuft(umgebung, monkeypatch, tmp_path):
    cf = tmp_path / "cert.pem"
    cf.write_bytes(_cert_pem(days=5))
    monkeypatch.setattr(smtp_cert, "aktiv", lambda: True)
    monkeypatch.setattr(smtp_cert, "CERT", cf)
    monkeypatch.setattr(smtp_cert, "info",
                        lambda: {"san": ["mail.example"], "subject": "CN=mail.example"})
    scheduler._check_smtp_cert()
    assert umgebung, "Ablauf-Alarm fürs separate SMTP-Zert erwartet"
    assert umgebung[0][0] == "mail.example" and umgebung[0][1] <= 5


def test_kein_alarm_ohne_separates_zert(umgebung, monkeypatch):
    monkeypatch.setattr(smtp_cert, "aktiv", lambda: False)
    scheduler._check_smtp_cert()
    assert not umgebung


def test_kein_alarm_wenn_noch_lange_gueltig(umgebung, monkeypatch, tmp_path):
    cf = tmp_path / "cert.pem"
    cf.write_bytes(_cert_pem(days=200))
    monkeypatch.setattr(smtp_cert, "aktiv", lambda: True)
    monkeypatch.setattr(smtp_cert, "CERT", cf)
    monkeypatch.setattr(smtp_cert, "info", lambda: {"san": ["mail.example"]})
    scheduler._check_smtp_cert()
    assert not umgebung
