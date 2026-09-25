"""Vorlagen-Arten (kind): Fundament, Pflicht beim Anlegen, Bewahrung beim Speichern.

Die tragende Invariante ist die Bewahrung: Speichert der Editor eine Meta ohne
`kind`, darf die Vorlage NICHT still auf `signatur` zurückfallen — sonst
verschwände ein zugewiesener Banner aus seinem typgefilterten Dropdown. Der Test
schlägt fehl, wenn man diese Bewahrung (vorlagen.py) zurückbaut.
"""
from __future__ import annotations

import json

import pytest

import config
import signature_engine


# ── Reine Funktionen ──────────────────────────────────────────────────────────

def test_slot_art_zuordnung():
    """min/addin sind Signaturen; banner/disclaimer eigene Arten."""
    assert signature_engine.SLOT_ART["min"] == "signatur"
    assert signature_engine.SLOT_ART["addin"] == "signatur"
    assert signature_engine.SLOT_ART["banner"] == "banner"
    assert signature_engine.SLOT_ART["disclaimer"] == "disclaimer"


def test_ist_bekannte_art():
    assert signature_engine.ist_bekannte_art("banner")
    assert signature_engine.ist_bekannte_art("usermail")
    assert not signature_engine.ist_bekannte_art("quatsch")
    assert not signature_engine.ist_bekannte_art("")


def test_vorlagen_art_faellt_bei_unbekanntem_kind_auf_signatur(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    (tmp_path / "X.html").write_text("<p>x</p>", encoding="utf-8")
    (tmp_path / "X.meta.json").write_text(json.dumps({"kind": "bannner"}), encoding="utf-8")  # Tippfehler
    assert signature_engine.vorlagen_art("X") == "signatur"


def test_templates_nach_art_gruppiert(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    for name, kind in (("Sig1", "signatur"), ("Ban1", "banner"), ("Dis1", "disclaimer")):
        (tmp_path / f"{name}.html").write_text("<p>x</p>", encoding="utf-8")
        (tmp_path / f"{name}.meta.json").write_text(json.dumps({"kind": kind}), encoding="utf-8")
    gruppen = signature_engine.templates_nach_art()
    assert set(gruppen.keys()) == {"signatur", "banner", "disclaimer", "oof"}
    assert "default" in gruppen["signatur"]          # Signaturliste trägt immer default
    assert "Ban1" in gruppen["banner"]
    assert "Dis1" in gruppen["disclaimer"]
    assert "Ban1" not in gruppen["signatur"]          # keine Vermischung


# ── Endpunkte: Anlegen (Pflicht-Typ) und Speichern (Bewahrung) ────────────────

@pytest.fixture(scope="module")
def client(tmp_path_factory):
    pytest.importorskip("starlette.testclient", reason="httpx wird für TestClient benötigt")
    from starlette.testclient import TestClient

    tmp = tmp_path_factory.mktemp("vorlagen-arten")
    tdir = tmp / "templates"
    tdir.mkdir()
    config.TEMPLATE_DIR = str(tdir)

    import settings_store
    settings_store.SETTINGS_FILE = tmp / "settings.json"
    settings_store._data = {}
    settings_store.init()
    settings_store.update({"SETUP_COMPLETE": True})

    for modul, attribut, unterpfad in [
        ("hub_orders", "_DIR", "hub_orders"),
        ("legal_consent", "_DB_PATH", "legal_consent.db"),
        ("mail_audit", "DB_PATH", "mail_audit.db"),
        ("portal_store", "_DB_PATH", "portal.db"),
        ("portal_store", "_BLOB_DIR", "portal"),
        ("smime_store", "SMIME_DIR", "smime"),
        ("smime_store", "RECIPIENT_DIR", "smime/recipients"),
        ("held_mails", "_HELD_DIR", "held_mails"),
    ]:
        m = __import__(modul)
        ziel = tmp / unterpfad
        (ziel.parent if ziel.suffix else ziel).mkdir(parents=True, exist_ok=True)
        setattr(m, attribut, ziel)

    from webui.app import app, _check_auth, _require_admin
    app.dependency_overrides[_check_auth] = lambda: "testadmin"
    app.dependency_overrides[_require_admin] = lambda: "testadmin"
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


def test_anlegen_verlangt_gueltige_art(client):
    assert client.post("/api/templates/OhneArt/create", json={}).status_code == 400
    assert client.post("/api/templates/QuatschArt/create", json={"kind": "quatsch"}).status_code == 400


def test_anlegen_mit_art_setzt_kind(client):
    r = client.post("/api/templates/NeuerBanner/create", json={"kind": "banner"})
    assert r.status_code == 200, r.text
    assert signature_engine.vorlagen_art("NeuerBanner") == "banner"
    assert "NeuerBanner" in signature_engine.list_templates("banner")
    assert "NeuerBanner" not in signature_engine.list_templates("signatur")


def test_speichern_bewahrt_art_wenn_client_sie_weglaesst(client):
    """DIE tragende Invariante: eine Meta ohne kind darf die Art nicht auf
    signatur zurücksetzen."""
    client.post("/api/templates/BleibtBanner/create", json={"kind": "banner"})
    # Speichern OHNE kind (wie ein alter Client), mit gültigem Baustein.
    meta = {"version": 1, "blocks": [{"type": "text", "text": "Hallo"}]}
    r = client.post("/api/templates/BleibtBanner/meta", json=meta)
    assert r.status_code == 200, r.text
    assert signature_engine.vorlagen_art("BleibtBanner") == "banner"  # NICHT signatur


def test_speichern_weist_unbekannte_art_ab(client):
    client.post("/api/templates/MitArt/create", json={"kind": "signatur"})
    meta = {"version": 1, "kind": "quatsch", "blocks": [{"type": "text", "text": "Hallo"}]}
    assert client.post("/api/templates/MitArt/meta", json=meta).status_code == 400
