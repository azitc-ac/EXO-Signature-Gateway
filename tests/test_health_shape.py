"""`/health` liefert die Felder, die der Bypass-Wächter braucht — und 503, wenn
der SMTP-Listener nicht bindet (dann muss der Wächter nicht in den Rumpf schauen).

Der Listener-Check ist eine echte Socket-Probe; im Test wird sie gestellt.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

pytest.importorskip("starlette.testclient", reason="httpx wird für TestClient benötigt")


@pytest.fixture
def client():
    from starlette.testclient import TestClient
    from webui import app as wa
    with TestClient(wa.app) as c:
        yield c


def test_health_ok_wenn_listener_bindet(client, monkeypatch):
    import health_check
    monkeypatch.setattr(health_check, "_smtp_listener_ok", lambda: True)
    r = client.get("/health")
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "ok"
    assert d["smtp_listener"] is True
    for feld in ("service", "echo", "graph_token", "reinject_mode", "version"):
        assert feld in d, f"/health ohne Feld {feld}"


def test_health_degraded_wenn_listener_weg(client, monkeypatch):
    import health_check
    monkeypatch.setattr(health_check, "_smtp_listener_ok", lambda: False)
    r = client.get("/health")
    assert r.status_code == 503
    d = r.json()
    assert d["status"] == "degraded"
    assert d["smtp_listener"] is False
    assert "echo" in d, "das echo-Token muss auch im 503-Rumpf stehen (Abnahme)"


def test_graph_token_steuert_den_status_nicht(client, monkeypatch):
    """§9.3: ein fehlendes Graph-Token macht /health NICHT degraded — nur der
    Listener zählt für den Status; graph_token wird bloß berichtet."""
    import health_check
    monkeypatch.setattr(health_check, "_smtp_listener_ok", lambda: True)
    import graph_client
    monkeypatch.setattr(graph_client, "cached_token_valid", lambda: False)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["graph_token"] is False
