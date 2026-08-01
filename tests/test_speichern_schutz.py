"""Speichern darf eine funktionierende Vorlage nie zerstören.

ANLASS (02.08.2026)
Nach dem Speichern einer rückübersetzten Vorlage kam beim Empfänger nur noch
Leerraum an. Zwei Wege führten dorthin, beide unbemerkt:

* Die Bausteinliste war LEER. Gespeichert wurde trotzdem — die Vorlage war weg.
* Die erzeugte Vorlage enthielt `{% else %}` ohne `{% if %}`. Sie ist damit kein
  gültiges Template; Jinja bricht ab und liefert nichts.

Der zweite Fall ist der tückischere: Die Datei ist gefüllt, sieht im Editor
richtig aus, und erst der Empfänger sieht die leere Signatur.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def klient(tmp_path, monkeypatch):
    import config
    import webui.app as wa
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    # NUR über dependency_overrides — ein monkeypatch auf `wa._check_auth`
    # tauscht die Referenz aus, während die Routen noch auf das ursprüngliche
    # Objekt zeigen. Der Übergabewert passte dann zu keinem Depends, und alle
    # Aufrufe endeten in 401.
    wa.app.dependency_overrides[wa._check_auth] = lambda: "test"
    with TestClient(wa.app) as c:
        yield c, tmp_path
    wa.app.dependency_overrides.clear()


def test_leere_bausteinliste_wird_abgelehnt(klient):
    c, verz = klient
    (verz / "probe.html").write_text("<p>bisherige Fassung</p>", encoding="utf-8")
    r = c.post("/api/templates/probe/meta", json={"version": 1, "blocks": []})
    assert r.status_code == 400, r.text
    assert "bisherige Fassung" in (verz / "probe.html").read_text(), \
        "die vorhandene Vorlage wurde trotz Ablehnung überschrieben"


def test_ungueltiges_ergebnis_wird_nicht_geschrieben(klient):
    """Ein Freitext-Baustein kann kaputtes Jinja enthalten — etwa weil beim
    Zurücklesen eine Bedingung zerrissen wurde."""
    c, verz = klient
    (verz / "probe.html").write_text("<p>bisherige Fassung</p>", encoding="utf-8")
    r = c.post("/api/templates/probe/meta", json={
        "version": 1,
        "blocks": [{"type": "freetext", "html": "A{% else %}B"}]})
    assert r.status_code == 400, r.text
    assert "Template" in r.json().get("detail", "")
    assert "bisherige Fassung" in (verz / "probe.html").read_text()


def test_gueltige_vorlage_wird_gespeichert_und_gesichert(klient):
    c, verz = klient
    (verz / "probe.html").write_text("<p>alt</p>", encoding="utf-8")
    r = c.post("/api/templates/probe/meta", json={
        "version": 1,
        "blocks": [{"type": "greeting", "text": "Freundliche Grüße"},
                   {"type": "name_field", "field": "displayName"}]})
    assert r.status_code == 200, r.text
    neu = (verz / "probe.html").read_text()
    assert "Freundliche Grüße" in neu
    # Die bisherige Fassung muss als Sicherung dastehen.
    assert (verz / "probe.html.bak").exists(), "keine Sicherung angelegt"
    assert "<p>alt</p>" in (verz / "probe.html.bak").read_text()
