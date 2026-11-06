"""Örtliche Admin-Anmeldung: EINE Prüfung, auch für die Einrichtungsseite auf Port 80.

Anlass (v1.10.74): Die Einrichtungsseite (Port 80, vor dem ersten TLS-Zertifikat)
nahm Hostname, Let's-Encrypt-Bestellung, PFX-Upload und DNS-01 ohne jede
Anmeldung an. Wer die IP einer frischen Installation fand, konnte vor dem
Betreiber ein Zertifikat für einen fremden Namen setzen. Daneben stand die
Kennwortprüfung dreimal im Code, je verschieden gedrosselt (/setup gar nicht).

Geprüft wird:
1. jede Kennwortprüfung läuft über webui.deps.admin_anmeldung,
2. admin_anmeldung: richtig, falsch, gedrosselt,
3. die Einrichtungsseite ändert ohne Anmeldung nichts und stößt nichts an,
4. jedes ihrer Formulare trägt die Anmeldefelder.
"""
import re
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import login_drossel  # noqa: E402
import main  # noqa: E402
from webui import deps  # noqa: E402


@pytest.fixture
def umgebung(monkeypatch):
    """Kennwort 'richtig-12345', kein gespeicherter Hash, leere Drossel."""
    gespeichert = {}
    monkeypatch.setattr(deps.config, "WEBUI_PASSWORD", "richtig-12345")
    monkeypatch.setattr(deps.settings_store, "get", lambda k, *a, **kw: gespeichert.get(k))
    monkeypatch.setattr(deps.settings_store, "update", lambda d: gespeichert.update(d))
    monkeypatch.setattr(login_drossel, "_FEHLER", {})
    return gespeichert


# ── 1. Eine Stelle ─────────────────────────────────────────────────────────────

def test_kennwort_wird_nur_in_admin_anmeldung_geprueft():
    funde = []
    for p in sorted((WURZEL / "app").rglob("*.py")):
        for nr, zeile in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if "_check_password(" in zeile and "def _check_password" not in zeile:
                funde.append(f"{p.relative_to(WURZEL)}:{nr}")
    assert len(funde) == 1 and funde[0].startswith("app/webui/deps.py"), (
        "Kennwort ausserhalb von admin_anmeldung geprüft — dort fehlen Drossel und "
        "Protokoll:\n" + "\n".join(funde))
    quelle = (WURZEL / "app/webui/deps.py").read_text(encoding="utf-8")
    rumpf = quelle[quelle.index("def admin_anmeldung"):quelle.index("# ── Anmeldung")]
    assert "_check_password(" in rumpf


# ── 2. admin_anmeldung ────────────────────────────────────────────────────────

def test_richtig_und_falsch(umgebung):
    assert deps.admin_anmeldung("admin", "richtig-12345", "198.51.100.1", "Test") == (True, 0.0)
    assert deps.admin_anmeldung("admin", "admin", "198.51.100.1", "Test") == (False, 0.0)
    assert deps.admin_anmeldung("fremd", "richtig-12345", "198.51.100.1", "Test") == (False, 0.0)
    assert deps.admin_anmeldung("admin", "", "198.51.100.1", "Test") == (False, 0.0)


def test_drossel_greift_nach_fehlversuchen(umgebung):
    for _ in range(login_drossel._FREI + 1):
        deps.admin_anmeldung("admin", "falsch", "198.51.100.2", "Test")
    ok, warte = deps.admin_anmeldung("admin", "richtig-12345", "198.51.100.2", "Test")
    assert not ok and warte > 0, "gedrosselt muss auch das richtige Kennwort warten lassen"


# ── 3. Einrichtungsseite ──────────────────────────────────────────────────────

@pytest.fixture
def bestellungen(monkeypatch):
    aufrufe = []
    monkeypatch.setattr(main, "_bootstrap_letsencrypt", lambda *a: aufrufe.append("le") or "")
    monkeypatch.setattr(main, "_bootstrap_import_pfx", lambda *a: aufrufe.append("pfx") or "")
    monkeypatch.setattr(main, "_bootstrap_dns01_start", lambda *a: (aufrufe.append("dns") or "", ""))
    monkeypatch.setattr(main, "_bootstrap_dns01_finish", lambda *a: aufrufe.append("dnsfertig") or "")
    return aufrufe


def _formular(**felder) -> bytes:
    import urllib.parse
    return urllib.parse.urlencode(felder).encode()


def _multipart(felder: dict) -> tuple[str, bytes]:
    grenze = "xyzGRENZE"
    teile = []
    for k, v in felder.items():
        teile.append(f'--{grenze}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n')
    teile.append(f'--{grenze}\r\nContent-Disposition: form-data; name="pfx_file"; filename="a.pfx"\r\n'
                 f'Content-Type: application/octet-stream\r\n\r\nPFX\r\n--{grenze}--\r\n')
    return f"multipart/form-data; boundary={grenze}", "".join(teile).encode()


FORM = "application/x-www-form-urlencoded"


@pytest.mark.parametrize("aktion", ["letsencrypt", "dns01-start", "dns01-finish"])
@pytest.mark.parametrize("anmeldung", [{}, {"admin_user": "admin", "admin_pass": "admin"}])
def test_ohne_gueltige_anmeldung_aendert_nichts(umgebung, bestellungen, aktion, anmeldung):
    seite = main._bootstrap_post(FORM, _formular(action=aktion, hostname="boese.example",
                                                 email="x@boese.example", **anmeldung),
                                 "203.0.113.9", Path("/tmp")).decode()
    assert bestellungen == []
    assert "PUBLIC_HOSTNAME" not in umgebung and "LE_EMAIL" not in umgebung
    assert "Kennwort der Weboberfläche falsch" in seite


def test_pfx_ohne_anmeldung_aendert_nichts(umgebung, bestellungen):
    ctype, rumpf = _multipart({"action": "pfx", "hostname": "boese.example"})
    main._bootstrap_post(ctype, rumpf, "203.0.113.9", Path("/tmp"))
    assert bestellungen == [] and "PUBLIC_HOSTNAME" not in umgebung


def test_mit_anmeldung_wird_bestellt(umgebung, bestellungen):
    main._bootstrap_post(FORM, _formular(action="letsencrypt", hostname="sig.example",
                                         email="a@example.org", admin_user="admin",
                                         admin_pass="richtig-12345"),
                         "203.0.113.10", Path("/tmp"))
    assert bestellungen == ["le"]
    assert umgebung["PUBLIC_HOSTNAME"] == "sig.example"
    ctype, rumpf = _multipart({"action": "pfx", "hostname": "sig.example",
                               "admin_user": "admin", "admin_pass": "richtig-12345"})
    main._bootstrap_post(ctype, rumpf, "203.0.113.10", Path("/tmp"))
    assert bestellungen == ["le", "pfx"]


# ── 4. Jedes Formular fragt nach der Anmeldung ─────────────────────────────────

def test_jedes_formular_hat_anmeldefelder():
    html = (main._setup_page().decode() + main._dns01_pending_block()
            + main._dns01_record_block("_acme-challenge.x", "wert"))
    formulare = re.findall(r"<form\b.*?</form>", html, re.S)
    assert len(formulare) == 5, len(formulare)
    for f in formulare:
        assert 'name="admin_user"' in f and 'name="admin_pass"' in f, f[:120]


def test_anonymer_aufruf_zeigt_keine_gespeicherte_email():
    quelle = (WURZEL / "app/main.py").read_text(encoding="utf-8")
    get = quelle[quelle.index("def do_GET"):quelle.index("def do_POST")]
    assert "LE_EMAIL" not in get and "PUBLIC_HOSTNAME" not in get
