"""Mit dem Auslieferungs-Kennwort lässt sich nur das Kennwort ändern (v1.10.113).

Bis v1.10.112 meldete `admin` (Vorgabe von WEBUI_PASSWORD im Compose-Weg) voll
an. Assistent und Sammelpunkt wiesen darauf hin, gesperrt war nichts.
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import login_drossel  # noqa: E402
from webui import deps  # noqa: E402
from webui.app import app  # noqa: E402


@pytest.fixture
def stand(monkeypatch):
    gespeichert = {"SETUP_COMPLETE": True}
    monkeypatch.setattr(deps.config, "WEBUI_PASSWORD", "admin")
    monkeypatch.setattr(deps.settings_store, "get", lambda k, *a, **kw: gespeichert.get(k))
    monkeypatch.setattr(deps.settings_store, "update", lambda d: gespeichert.update(d))
    monkeypatch.setattr(login_drossel, "_FEHLER", {})
    gesichert = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    yield gespeichert
    app.dependency_overrides.update(gesichert)


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def test_api_mit_vorgabe_kennwort_gesperrt(stand, client):
    r = client.get("/api/smime/wartend", auth=("admin", "admin"))
    assert r.status_code == 403 and "Auslieferungs-Kennwort" in r.json()["detail"]


def test_seite_mit_vorgabe_kennwort_fuehrt_zu_schritt_1(stand, client):
    r = client.get("/smime", auth=("admin", "admin"), follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/setup#step-1"


def test_kennwort_aendern_bleibt_erreichbar_und_hebt_die_sperre_auf(stand, client):
    r = client.post("/api/setup/change-password", auth=("admin", "admin"),
                    json={"password": "neues-kennwort-123"})
    assert r.status_code == 200, r.text
    assert stand.get("ADMIN_PASSWORD_HASH")
    assert client.get("/api/smime/wartend", auth=("admin", "admin")).status_code == 401
    assert client.get("/api/smime/wartend",
                      auth=("admin", "neues-kennwort-123")).status_code == 200


def test_eigenes_kennwort_aus_der_umgebung_sperrt_nicht(stand, client, monkeypatch):
    monkeypatch.setattr(deps.config, "WEBUI_PASSWORD", "eigenes-kennwort-1")
    assert client.get("/api/smime/wartend",
                      auth=("admin", "eigenes-kennwort-1")).status_code == 200


class _Anfrage:
    class url:
        path = "/api/smime/wartend"


def test_entra_anmeldung_ist_nicht_betroffen(stand):
    deps._vorgabe_kennwort_sperre(_Anfrage(), "chefin@firma.de")       # kein Fehler
    with pytest.raises(deps._KennwortZuerst):
        deps._vorgabe_kennwort_sperre(_Anfrage(), deps.LOKALER_ADMIN)
