"""Vorschau mit getrennter schreibender Person (`bearbeiter`).

Im Betrieb füllen handler und Verfassen-Add-in `{{ bearbeiter.* }}` mit der
Person, die „im Auftrag von“ eines geteilten Postfachs schreibt. Die Vorschau
setzte bis v1.12.51 immer den Inhaber ein — eine Vorlage mit getrenntem
Bearbeiter liess sich nur durch echtes Senden prüfen.
"""
import pytest

pytest.importorskip("starlette.testclient", reason="httpx wird für TestClient benötigt")


@pytest.fixture
def client(monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    import config
    import graph_client
    import settings_store
    import signature_engine
    from webui.app import app, _check_auth

    namen = {"info@example.org": "Info-Postfach", "max@example.org": "Max Mustermann"}

    async def nachgebaut(email):
        return graph_client.UserData(displayName=namen.get(email, ""), mail=email)
    monkeypatch.setattr(graph_client, "get_user", nachgebaut)

    for name, inhalt in (
        ("signature.html", "<p>{{ user.displayName }}|{{ bearbeiter.displayName }}"
                           "{% if fuer_postfach %}|AUFTRAG{% endif %}</p>"),
        ("Werbung.html", "<p>B:{{ bearbeiter.displayName }}</p>"),
    ):
        (tmp_path / name).write_text(inhalt, encoding="utf-8")
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    signature_engine._reload_env()

    echt = settings_store.get

    def gefaelscht(schluessel, *a, **kw):
        if schluessel == "MAILBOX_CONFIG":
            return {"info@example.org": {"sig": True, "banner_template": "Werbung"}}
        if schluessel in ("TEMPLATE_POLICIES", "INTERNAL_GROUPS"):
            return {}
        if schluessel in ("CUSTOM_POLICIES", "BANNER_CAMPAIGNS"):
            return []
        return echt(schluessel, *a, **kw)
    monkeypatch.setattr(settings_store, "get", gefaelscht)

    app.dependency_overrides[_check_auth] = lambda: "testadmin"
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()
    signature_engine._reload_env()


def _hole(client, **params):
    from urllib.parse import urlencode
    r = client.get("/api/preview-data?" + urlencode(params))
    assert r.status_code == 200, r.text
    return r.json()


def test_ohne_bearbeiter_schreibt_der_inhaber(client):
    d = _hole(client, email="info@example.org", template="default")
    assert "Info-Postfach|Info-Postfach</p>" in d["html"]
    # Editor-Vorschau: nur die Vorlage, kein Banner des Postfachs (v1.12.110)
    assert d["banner_html"] == ""


def test_bearbeiter_fuellt_die_signatur(client):
    d = _hole(client, email="info@example.org", template="default", bearbeiter="max@example.org")
    assert "Info-Postfach|Max Mustermann|AUFTRAG" in d["html"]


def test_richtlinienergebnis_bearbeiter_banner_bleibt_beim_inhaber(client):
    """Seite Richtlinienergebnis: Signatur mit Bearbeiter, Banner rendert der
    handler ohne `extra` — im Betrieb steht dort der Inhaber."""
    r = client.get("/api/richtlinienergebnis",
                   params={"email": "info@example.org", "bearbeiter": "max@example.org"})
    assert r.status_code == 200, r.text
    neu = next(f for f in r.json()["faelle"] if f["schluessel"] == "neu")
    assert "Info-Postfach|Max Mustermann|AUFTRAG" in neu["html"]
    assert "B:Info-Postfach" in neu["html"]


def test_bearbeiter_auch_auf_der_vorschauseite(client):
    d = _hole(client, email="info@example.org", template="default", banner="Werbung",
              disclaimer="", oof="", explizit="1", bearbeiter="max@example.org")
    assert "|Max Mustermann|AUFTRAG" in d["html"]
    assert "B:Info-Postfach" in d["banner_html"]


def test_inhaber_als_bearbeiter_ist_kein_auftrag(client):
    d = _hole(client, email="info@example.org", template="default",
              bearbeiter="INFO@example.org")
    assert "AUFTRAG" not in d["html"]
