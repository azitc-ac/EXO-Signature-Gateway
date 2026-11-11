"""„Kalender jetzt lesen“ und nachvollziehbarer Abgleich (v1.11.26).

ANLASS (11.11.2026): Ein Betreiber fragte, wann eine Abwesenheit aus einem
Abwesend-Termin gesetzt wird. Der Abgleich läuft alle 10 Minuten, den Kalender
liest er aber nur alle OOO_CALENDAR_REFRESH_HOURS (Vorgabe 6 h) — ein neuer
Termin wirkte also erst nach bis zu gut sechs Stunden, ohne dass das irgendwo
stand.
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import abwesenheit  # noqa: E402


@pytest.fixture
def anlage(monkeypatch):
    jetzt = datetime.now(timezone.utc)
    state = {"a@firma.at": {"kal": {"ts": jetzt.isoformat(), "events": [], "v": abwesenheit._KAL_STAND}},
             "b@firma.at": {"kal": {"ts": (jetzt - timedelta(hours=2)).isoformat(), "events": [],
                                    "v": abwesenheit._KAL_STAND}}}
    werte = {"OOO_CALENDAR_REFRESH_HOURS": 6, "MAILBOX_CONFIG": {}, "_OOO_LAST": {"ts": jetzt.isoformat()}}
    monkeypatch.setattr(abwesenheit.settings_store, "get", lambda k, d=None: werte.get(k, d))
    monkeypatch.setattr(abwesenheit, "zentral_aktiv", lambda: True)
    monkeypatch.setattr(abwesenheit, "kalender_lesen", lambda: True)
    monkeypatch.setattr(abwesenheit, "_aktive_postfaecher",
                        lambda cfg: [("a@firma.at", {}), ("b@firma.at", {})])
    monkeypatch.setattr(abwesenheit, "_state", lambda: state)
    monkeypatch.setattr(abwesenheit, "_state_speichern", lambda s: None)
    monkeypatch.setattr(abwesenheit, "_persist_last", lambda e: None)

    async def token():
        return "tok"
    monkeypatch.setattr(abwesenheit.graph_client, "_acquire_token_async", token)
    gelesen = []

    async def graph_lesen(upn, tok):
        gelesen.append(upn)
        return []
    monkeypatch.setattr(abwesenheit, "_kalender_oof_events", graph_lesen)

    async def setzen(upn, _u, _mb, _cfg, tok, st):
        await abwesenheit.kalender_events(upn, tok, st)
        return abwesenheit.UNVERAENDERT
    monkeypatch.setattr(abwesenheit, "setze_fuer_postfach", setzen)
    return {"state": state, "gelesen": gelesen, "jetzt": jetzt}


def test_normaler_lauf_nutzt_den_cache(anlage):
    asyncio.run(abwesenheit.poll_alle())
    assert anlage["gelesen"] == [], "frischer Cache — kein Graph-Aufruf"


def test_jetzt_lesen_liest_jeden_kalender_neu(anlage):
    ergebnis = asyncio.run(abwesenheit.poll_alle(kalender_frisch=True))
    assert sorted(anlage["gelesen"]) == ["a@firma.at", "b@firma.at"]
    assert ergebnis["unveraendert"] == 2


def test_stand_nennt_aeltesten_kalender_und_spaetestens(anlage):
    st = abwesenheit.abgleich_stand()
    aeltester = datetime.fromisoformat(st["kalender_aeltester"])
    assert abs((anlage["jetzt"] - timedelta(hours=2)) - aeltester) < timedelta(seconds=1)
    assert datetime.fromisoformat(st["kalender_spaetestens"]) == aeltester + timedelta(hours=6)
    assert st["takt_min"] == 10 and st["kalender_refresh_h"] == 6


def test_route_jetzt_lesen(anlage, monkeypatch):
    from fastapi.testclient import TestClient
    from webui import deps
    from webui.app import app
    app.dependency_overrides[deps._require_admin] = lambda: "admin"
    try:
        c = TestClient(app)
        r = c.post("/api/abwesenheit/jetzt-lesen")
        assert r.status_code == 200 and r.json()["ok"], r.text
        assert sorted(anlage["gelesen"]) == ["a@firma.at", "b@firma.at"]
        monkeypatch.setattr(abwesenheit, "zentral_aktiv", lambda: False)
        assert c.post("/api/abwesenheit/jetzt-lesen").status_code == 409
    finally:
        app.dependency_overrides.pop(deps._require_admin, None)
