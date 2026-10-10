"""Ziel nach der Anmeldung: nur Pfade auf diesem Server, und es geht nicht verloren.

ANLASS (2026-10-11, Outlook-Testlauf): /self → Anmeldeseite → „Mit Microsoft
anmelden" landete auf "/" statt auf /self. Der Knopf holte die Login-URL über
/api/auth/sso-url, das kein `next` kannte. Beim Nachverfolgen fiel auf, dass
`next` auch ungeprüft benutzt wurde: login.html setzte es aus der Adresszeile
direkt in `location.href`, der Code-Flow-Rückruf leitete es ungeprüft weiter,
der id_token-Rückruf prüfte nur `startswith("/")` (lässt `//fremd` durch).
"""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

pytest.importorskip("starlette.testclient", reason="httpx wird für TestClient benötigt")

from webui.routen import anmeldung  # noqa: E402


@pytest.mark.parametrize("ziel", ["/", "/self", "/abwesenheit?x=1#a", "/addin/auth-complete"])
def test_eigene_pfade_bleiben(ziel):
    assert anmeldung._sicheres_ziel(ziel) == ziel


@pytest.mark.parametrize("ziel", [
    "//fremd.example", "/\\fremd.example", "javascript:alert(1)", "https://fremd.example",
    "fremd.example", "", None, "/a b", "/a\tb", "/a\nb", "/a\x00b", 42,
])
def test_fremde_ziele_werden_zu_wurzel(ziel):
    assert anmeldung._sicheres_ziel(ziel) == "/"


@pytest.fixture
def client():
    from starlette.testclient import TestClient
    from webui import app as wa
    with TestClient(wa.app) as c:
        yield c


@pytest.fixture
def gemerkt(monkeypatch):
    """Fängt das next_url ab, mit dem der Microsoft-Login gestartet wird."""
    seen = {}

    def fake(redirect_uri, next_url="/"):
        seen["next_url"] = next_url
        return "STATE", "https://login.example/authorize"
    monkeypatch.setattr(anmeldung.pkce_mod, "create_implicit_session", fake)
    return seen


def test_sso_url_reicht_ziel_durch(client, gemerkt):
    r = client.get("/api/auth/sso-url", params={"next": "/self"})
    assert r.status_code == 200 and gemerkt["next_url"] == "/self"


def test_sso_url_verwirft_fremdes_ziel(client, gemerkt):
    client.get("/api/auth/sso-url", params={"next": "//fremd.example"})
    assert gemerkt["next_url"] == "/"


def test_login_microsoft_verwirft_fremdes_ziel(client, gemerkt):
    client.get("/auth/login/microsoft", params={"next": "/\\fremd.example"},
               follow_redirects=False)
    assert gemerkt["next_url"] == "/"


def test_anmeldeseite_bekommt_geprueftes_ziel(client):
    seite = client.get("/auth/login", params={"next": "javascript:alert(1)"}).text
    assert 'const ANMELDE_ZIEL = "/";' in seite
    assert "javascript:alert" not in seite
    seite = client.get("/auth/login", params={"next": "/self"}).text
    assert 'const ANMELDE_ZIEL = "/self";' in seite


def test_anmeldeseite_liest_ziel_nicht_selbst_aus_der_adresszeile():
    js = (ROOT / "app/webui/templates/login.html").read_text(encoding="utf-8")
    assert not re.search(r"location\.search\)\.get\(['\"]next", js)
    assert "sso-url?next=" in js


def test_beide_rueckrufe_nutzen_die_pruefung():
    src = (ROOT / "app/webui/routen/anmeldung.py").read_text(encoding="utf-8")
    weiterleitungen = re.findall(r"next_url = (.+)", src)
    assert len(weiterleitungen) == 2, weiterleitungen
    assert all(w.startswith("_sicheres_ziel(") for w in weiterleitungen)
