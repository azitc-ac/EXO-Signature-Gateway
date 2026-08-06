"""Handänderungen am Quelltext müssen in den Baukasten zurückfinden.

DAS WAR DER ZWECK DER RÜCKÜBERSETZUNG
Sie wurde gebaut, damit bestehendes HTML in Bausteine übergeht. Angeboten
wurde sie aber nur bei Vorlagen OHNE Bausteine. Wer eine Baukasten-Vorlage von
Hand nachbesserte — etwa Symbole statt Textbeschriftungen einsetzte —, sah beim
nächsten Öffnen die alten Bausteine und verlor seine Arbeit beim Speichern.

Maßgeblich ist der Zeitstempel: Ist die HTML-Datei neuer als die Bausteindatei,
liegt die Wahrheit im Quelltext.
"""
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def klient(tmp_path, monkeypatch):
    """Die HTML-Route braucht mehr Umgebung als die reinen API-Endpunkte.

    `settings_store.get()` ruft bei leerem Speicher `init()`, und das legt das
    Datenverzeichnis an — im Test `/app`, wo niemand schreiben darf. Deshalb
    hier ein Speicher im Arbeitsspeicher.
    """
    import config
    import settings_store
    import webui.app as wa
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    daten = {"WEBUI_USERNAME": "admin", "CUSTOM_TEMPLATE_VARS": []}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: daten.get(k, d))
    monkeypatch.setattr(settings_store, "get_all", lambda: dict(daten))
    monkeypatch.setattr(settings_store, "update", lambda neu: daten.update(neu))
    wa.app.dependency_overrides[wa._check_auth] = lambda: "test"
    with TestClient(wa.app) as c:
        yield c, tmp_path
    wa.app.dependency_overrides.clear()


def _anlegen(verz, name, html, meta_alt=True):
    (verz / f"{name}.meta.json").write_text('{"version":1,"blocks":[]}', encoding="utf-8")
    time.sleep(1.1)                      # Zeitstempel müssen sich unterscheiden
    (verz / f"{name}.html").write_text(html, encoding="utf-8")
    (verz / f"{name}.txt").write_text("x", encoding="utf-8")
    if not meta_alt:                     # umgekehrte Reihenfolge
        time.sleep(1.1)
        (verz / f"{name}.meta.json").write_text('{"version":1,"blocks":[]}', encoding="utf-8")


def test_neuerer_quelltext_wird_gemeldet(klient):
    c, verz = klient
    _anlegen(verz, "Probe", "<p>von Hand geändert</p>", meta_alt=True)
    r = c.get("/template?name=Probe")
    assert r.status_code == 200
    assert "const QUELLTEXT_NEUER = true" in r.text, (
        "Der Editor erfährt nicht, dass der Quelltext neuer ist — die "
        "Handänderung ginge beim nächsten Speichern verloren.")


def test_aktuelle_bausteine_werden_nicht_gemeldet(klient):
    """Gegenprobe: Sonst käme die Rückfrage bei jedem Öffnen."""
    c, verz = klient
    _anlegen(verz, "Probe", "<p>x</p>", meta_alt=False)
    r = c.get("/template?name=Probe")
    assert "const QUELLTEXT_NEUER = false" in r.text, r.text[:200]


def test_ohne_bausteine_kein_sonderfall(klient):
    """Vorlagen ohne Bausteine laufen weiter über den bisherigen Weg."""
    c, verz = klient
    (verz / "Roh.html").write_text("<p>x</p>", encoding="utf-8")
    r = c.get("/template?name=Roh")
    assert "const QUELLTEXT_NEUER = false" in r.text
    assert "const HAS_META      = false" in r.text
