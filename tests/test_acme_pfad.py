"""Der ACME-Challenge-Handler auf Port 80 darf nicht aus dem webroot ausbrechen.

ANLASS: `BaseHTTPRequestHandler` normalisiert `..` nicht. Ohne Containment-Prüfung
liesse `/.well-known/acme-challenge/../../../settings.json` das Auslesen von
Geheimnissen zu (settings.json trägt CLIENT_SECRET, SSO-Secret, S/MIME-Passwort).
Port 80 ist dauerhaft offen, der Challenge-Zweig läuft vor der HTTPS-Umleitung.
Der Test schlägt fehl, wenn die Auflösung wieder naiv (`webroot / pfad`) wird.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import main  # noqa: E402


def test_legitime_challenge_wird_ausgeliefert(tmp_path):
    webroot = tmp_path / "acme-webroot"
    (webroot / ".well-known" / "acme-challenge").mkdir(parents=True)
    (webroot / ".well-known" / "acme-challenge" / "tok123").write_text("erlaubt")
    ziel = main._acme_challenge_datei(webroot, "/.well-known/acme-challenge/tok123")
    assert ziel is not None and ziel.read_text() == "erlaubt"


def test_traversal_auf_settings_wird_abgewiesen(tmp_path):
    webroot = tmp_path / "acme-webroot"
    (webroot / ".well-known" / "acme-challenge").mkdir(parents=True)
    (tmp_path / "settings.json").write_text("SECRET")            # liegt AUSSERHALB webroot
    # 3× hoch aus …/acme-challenge landet in tmp_path → ausserhalb webroot
    assert main._acme_challenge_datei(
        webroot, "/.well-known/acme-challenge/../../../settings.json") is None
    assert main._acme_challenge_datei(
        webroot, "/.well-known/acme-challenge/../../../../etc/hostname") is None


def test_verzeichnis_ist_keine_datei(tmp_path):
    webroot = tmp_path / "acme-webroot"
    (webroot / ".well-known" / "acme-challenge").mkdir(parents=True)
    assert main._acme_challenge_datei(webroot, "/.well-known/acme-challenge/") is None
