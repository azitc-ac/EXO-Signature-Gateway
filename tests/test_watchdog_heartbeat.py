"""Heartbeat-Endpunkt des Bypass-Wächters: Token-Prüfung, Zustands-Ablage, Größe.

Der Wächter meldet sich per POST mit `X-Watchdog-Token`. Falscher Token → 401
ohne Details; richtiger → Zustand landet in data/watchdog_state.json; ein zu
großer Rumpf → 413.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

pytest.importorskip("starlette.testclient", reason="httpx wird für TestClient benötigt")


@pytest.fixture
def anlage(monkeypatch, tmp_path):
    """Gesetztes Token (Hash), Zustandsdatei im tmp; Settings-Schreibweg gestubbt."""
    from starlette.testclient import TestClient
    from webui import app as wa
    from webui import deps
    from webui.routen import waechter

    werte = {"WATCHDOG_TOKEN_HASH": deps._hash_password("gutes-token"),
             "WATCHDOG_ENABLED": True, "WATCHDOG_KIND": "cron"}
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))
    monkeypatch.setattr(waechter, "_STATE", tmp_path / "watchdog_state.json")
    with TestClient(wa.app) as c:
        yield c, waechter


def test_falsches_token_401_ohne_details(anlage):
    c, waechter = anlage
    r = c.post("/api/watchdog/heartbeat", json={"bypass_active": True},
               headers={"X-Watchdog-Token": "falsch"})
    assert r.status_code == 401
    assert "bypass" not in r.text.lower()          # keine Zustands-Details preisgeben
    assert not waechter._STATE.exists()             # nichts geschrieben


def test_fehlendes_token_401(anlage):
    c, _ = anlage
    assert c.post("/api/watchdog/heartbeat", json={}).status_code == 401


def test_richtiges_token_schreibt_zustand(anlage):
    c, waechter = anlage
    r = c.post("/api/watchdog/heartbeat",
               json={"bypass_active": True, "fails": 3, "oks": 0, "healthy": False},
               headers={"X-Watchdog-Token": "gutes-token"})
    assert r.status_code == 200
    st = waechter.zustand()
    assert st["bypass_active"] is True
    assert st["fails"] == 3
    assert st["last_seen"]                          # Zeitstempel gesetzt


def test_zu_grosser_rumpf_413(anlage):
    c, _ = anlage
    r = c.post("/api/watchdog/heartbeat", content=b"x" * 2000,
               headers={"X-Watchdog-Token": "gutes-token",
                        "Content-Type": "application/json"})
    assert r.status_code == 413


def test_token_rotate_braucht_admin(anlage):
    c, _ = anlage
    # Ohne Sitzung/Basic → keine Verwaltung → nicht 200.
    r = c.post("/api/watchdog/token/rotate")
    assert r.status_code in (401, 403)
