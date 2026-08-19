"""Beide Schreibwege der Oberfläche prüfen gleich streng.

ANLASS (19.08.2026): Es gibt zwei allgemeine Endpunkte, die Einstellungen
schreiben — `POST /settings` (Formular) und `POST /api/settings/partial`
(einzelne Felder aus dem Betrieb). Der erste filterte gegen
`settings_store.DEFAULTS`, der zweite schrieb ungeprüft, was ihm geschickt
wurde; im Code stand als Begründung, der Aufrufer sei ohnehin angemeldet.

Das ist kein Argument für Beliebigkeit. Zwei Wege zur selben Datei mit
unterschiedlicher Strenge sind keine Entscheidung, sondern ein Versehen — und
der laxere gewinnt, weil er der bequemere ist. Praktisch liess sich jeder
erfundene Schlüssel dauerhaft in `settings.json` schreiben; aufgeräumt wird nur,
was in `OBSOLETE_KEYS` steht, und dort steht nur, was jemand kannte.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import settings_store  # noqa: E402


# ── Die gemeinsame Funktion ──────────────────────────────────────────────────

def test_unbekannte_schluessel_kommen_nicht_durch():
    clean, verworfen = settings_store.nur_bekannte(
        {"SMIME_AUTO_ENROLL": True, "VOELLIG_ERFUNDEN": "x"})
    assert clean == {"SMIME_AUTO_ENROLL": True}
    assert verworfen == ["VOELLIG_ERFUNDEN"]


def test_verworfenes_wird_gemeldet_nicht_geschluckt():
    """⚠️ Ein Tippfehler sieht sonst aus wie „gespeichert, wirkt aber nicht"."""
    _clean, verworfen = settings_store.nur_bekannte({"SMIE_TAG_SIGNED": "x"})
    assert verworfen == ["SMIE_TAG_SIGNED"]


def test_leere_und_kaputte_eingaben_werfen_nicht():
    assert settings_store.nur_bekannte({}) == ({}, [])
    assert settings_store.nur_bekannte(None) == ({}, [])
    assert settings_store.nur_bekannte([1, 2]) == ({}, [])


# ── Beide Endpunkte, echte Aufrufe ───────────────────────────────────────────

pytest.importorskip("starlette.testclient", reason="httpx wird für TestClient benötigt")


@pytest.fixture
def klient(monkeypatch):
    """Verwaltungssitzung; Schreibvorgänge werden aufgezeichnet statt ausgeführt."""
    from starlette.testclient import TestClient
    from webui import app as wa
    from webui.routen import settings as sr

    geschrieben = []
    monkeypatch.setattr(sr.settings_store, "update", lambda d: geschrieben.append(dict(d)))
    wa.app.dependency_overrides[wa._require_admin] = lambda: "testadmin"
    with TestClient(wa.app) as c:
        yield c, geschrieben
    wa.app.dependency_overrides.clear()


WEGE = ["/settings", "/api/settings/partial"]


@pytest.mark.parametrize("weg", WEGE)
def test_bekannte_einstellung_kommt_an(klient, weg):
    c, geschrieben = klient
    antwort = c.post(weg, json={"SMIME_AUTO_ENROLL": True})
    assert antwort.status_code == 200, antwort.text[:200]
    assert geschrieben == [{"SMIME_AUTO_ENROLL": True}]


@pytest.mark.parametrize("weg", WEGE)
def test_erfundener_schluessel_landet_nirgends(klient, weg):
    """⚠️ Der Kern: BEIDE Wege müssen gleich streng sein."""
    c, geschrieben = klient
    c.post(weg, json={"SMIME_AUTO_ENROLL": True, "ERFUNDEN_XYZ": "hallo"})
    assert geschrieben, "gar nichts geschrieben"
    assert "ERFUNDEN_XYZ" not in geschrieben[0], f"{weg} schreibt Unbekanntes"


@pytest.mark.parametrize("weg", WEGE)
def test_nur_unbekanntes_schreibt_gar_nichts(klient, weg):
    c, geschrieben = klient
    c.post(weg, json={"ERFUNDEN_XYZ": "hallo"})
    assert not any(g for g in geschrieben), "Unbekanntes wurde geschrieben"
