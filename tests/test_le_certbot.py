"""le_certbot: Ausstellen und Erneuern aus EINER Quelle.

Kernvertrag (der behobene Bug): Der Renew nutzt DIESELBEN certbot-Verzeichnisse
wie die Ausstellung (unter DATA_DIR, NICHT die root-eigenen Default-Pfade) und
übernimmt ein tatsächlich erneuertes Zertifikat an den Listener-Pfad. Bricht das
wieder auseinander, muss hier ein Test fehlschlagen.
"""
import datetime
import shutil
from pathlib import Path

import pytest

import config
import le_certbot


class _FakeProc:
    def __init__(self, rc):
        self.returncode = rc
        self.stdout = ""
        self.stderr = ""


def _cert(days: int) -> tuple[bytes, bytes]:
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sig.test")])
    now = datetime.datetime.now(datetime.timezone.utc)      # relativ, keine Zeitbombe
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=days))
            .sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM,
                              serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()))


def _write(cert_path: Path, key_path: Path, days: int) -> None:
    c, k = _cert(days)
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(c)
    key_path.write_bytes(k)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "SMTP_TLS_CERT", str(tmp_path / "certs" / "cert.pem"))
    monkeypatch.setattr(config, "SMTP_TLS_KEY", str(tmp_path / "certs" / "key.pem"))
    return tmp_path


def _managed():
    r = le_certbot._dirs()["config"] / "renewal"
    r.mkdir(parents=True, exist_ok=True)
    (r / "gateway.conf").write_text("[renewalparams]\nauthenticator = webroot\n")


def test_ausstellen_und_erneuern_nutzen_dieselben_verzeichnisse(env, monkeypatch):
    """Anti-Drift-Wache: beide Wege geben certbot dieselben --config/-work/-logs-
    dirs UNTER DATA_DIR. Der ursprüngliche Bug war, dass der Renew sie wegließ."""
    calls = []
    monkeypatch.setattr(le_certbot.subprocess, "run",
                        lambda cmd, **kw: calls.append(list(cmd)) or _FakeProc(1))
    le_certbot.ausstellen("sig.test", "a@test")
    _managed()
    le_certbot.erneuern()
    assert len(calls) == 2, "ausstellen UND erneuern müssen certbot je einmal rufen"

    def val(cmd, flag):
        return cmd[cmd.index(flag) + 1]

    dd = le_certbot._dirs()
    for cmd in calls:
        assert val(cmd, "--config-dir") == str(dd["config"])
        assert val(cmd, "--work-dir") == str(dd["work"])
        assert val(cmd, "--logs-dir") == str(dd["logs"])
        assert str(dd["config"]).startswith(str(env))     # DATA_DIR, nicht /etc/letsencrypt


def test_erneuern_unterdrueckt_zufallsverzoegerung(env, monkeypatch):
    """certbot legt im non-interactive-Modus sonst bis zu ~8 Min Zufallsverzögerung
    VOR die Erneuerung, die das Subprozess-Timeout auffrisst (live: „random delay
    of 172s" bei 180s Timeout → nie erneuert). --no-random-sleep-on-renew ist
    Pflicht; ohne die Flag kehrt der Timeout-Bug zurück."""
    captured = []
    monkeypatch.setattr(le_certbot.subprocess, "run",
                        lambda cmd, **kw: captured.append(list(cmd)) or _FakeProc(1))
    _managed()
    le_certbot.erneuern()
    assert captured, "certbot renew wurde nicht aufgerufen"
    assert "--no-random-sleep-on-renew" in captured[0]


def test_erneuern_ohne_konfig_ist_not_managed(env, monkeypatch):
    called = []
    monkeypatch.setattr(le_certbot.subprocess, "run",
                        lambda *a, **k: called.append(1) or _FakeProc(0))
    status, info = le_certbot.erneuern()
    assert status == "not_managed"
    assert not called, "ohne Renewal-Konfig darf certbot gar nicht erst laufen"


def test_erneuern_uebernimmt_neues_zert_und_setzt_600(env, monkeypatch):
    _managed()
    _write(Path(config.SMTP_TLS_CERT), Path(config.SMTP_TLS_KEY), days=5)     # bald ab
    live = le_certbot._live()
    _write(live / "fullchain.pem", live / "privkey.pem", days=89)             # frisch erneuert
    monkeypatch.setattr(le_certbot.subprocess, "run", lambda *a, **k: _FakeProc(0))
    status, info = le_certbot.erneuern()
    assert status == "renewed"
    # Das servierte Zert IST jetzt das Live-Zert (Übernahme passiert)
    assert Path(config.SMTP_TLS_CERT).read_bytes() == (live / "fullchain.pem").read_bytes()
    assert Path(config.SMTP_TLS_KEY).read_bytes() == (live / "privkey.pem").read_bytes()
    assert oct(Path(config.SMTP_TLS_KEY).stat().st_mode & 0o777) == "0o600"


def test_erneuern_unchanged_wenn_live_nicht_neuer(env, monkeypatch):
    _managed()
    _write(Path(config.SMTP_TLS_CERT), Path(config.SMTP_TLS_KEY), days=40)
    live = le_certbot._live()
    live.mkdir(parents=True, exist_ok=True)
    shutil.copy2(config.SMTP_TLS_CERT, live / "fullchain.pem")   # certbot hat NICHT erneuert
    shutil.copy2(config.SMTP_TLS_KEY, live / "privkey.pem")
    vorher = Path(config.SMTP_TLS_CERT).read_bytes()
    monkeypatch.setattr(le_certbot.subprocess, "run", lambda *a, **k: _FakeProc(0))
    status, info = le_certbot.erneuern()
    assert status == "unchanged"
    assert Path(config.SMTP_TLS_CERT).read_bytes() == vorher     # nichts überschrieben


def test_erneuern_meldet_fehler_bei_rc_ungleich_null(env, monkeypatch):
    _managed()
    _write(Path(config.SMTP_TLS_CERT), Path(config.SMTP_TLS_KEY), days=5)
    monkeypatch.setattr(le_certbot.subprocess, "run", lambda *a, **k: _FakeProc(1))
    status, info = le_certbot.erneuern()
    assert status == "error"
