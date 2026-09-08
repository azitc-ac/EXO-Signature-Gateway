"""Heartbeat-Endpunkt des Bypass-Wächters: Zuordnung (Id/Token), Zustand, Größe.

Ein Wächter meldet sich per POST mit `X-Watchdog-Token` (und optional
`X-Watchdog-Id`). Falscher/unbekannter Token → 401 ohne Details; richtiger →
Zustand landet PRO WÄCHTER im Register; ein zu großer Rumpf → 413. Legacy-Wächter
ohne Id werden per Token-Scan zugeordnet.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

pytest.importorskip("starlette.testclient", reason="httpx wird für TestClient benötigt")


@pytest.fixture
def anlage(monkeypatch, tmp_path):
    """Ein registrierter Wächter (Token 'gutes-token'), Register im tmp."""
    from starlette.testclient import TestClient
    from webui import app as wa
    from webui import deps
    from webui.routen import waechter
    import waechter_register

    monkeypatch.setattr(waechter_register, "PFAD", tmp_path / "watchdog_watchers.json")
    waechter_register.registrieren(id="wd_test", name="Test", kind="cron",
                                   token_hash=deps._hash_password("gutes-token"))
    with TestClient(wa.app) as c:
        yield c, waechter, waechter_register


def test_falsches_token_401_ohne_details(anlage):
    c, _waechter, reg = anlage
    r = c.post("/api/watchdog/heartbeat", json={"bypass_active": True},
               headers={"X-Watchdog-Token": "falsch"})
    assert r.status_code == 401
    assert "bypass" not in r.text.lower()               # keine Zustands-Details preisgeben
    assert not reg.holen("wd_test")["last_seen"]         # nichts geschrieben


def test_fehlendes_token_401(anlage):
    c, _w, _reg = anlage
    assert c.post("/api/watchdog/heartbeat", json={}).status_code == 401


def test_richtiges_token_ohne_id_per_scan(anlage):
    """Legacy-Weg: nur Token, keine Id → Zuordnung per Scan."""
    c, _w, reg = anlage
    r = c.post("/api/watchdog/heartbeat",
               json={"bypass_active": True, "fails": 3, "oks": 0, "healthy": False},
               headers={"X-Watchdog-Token": "gutes-token"})
    assert r.status_code == 200
    e = reg.holen("wd_test")
    assert e["bypass_active"] is True and e["fails"] == 3 and e["last_seen"]


def test_richtiges_token_mit_id(anlage):
    c, _w, reg = anlage
    r = c.post("/api/watchdog/heartbeat", json={"healthy": True},
               headers={"X-Watchdog-Token": "gutes-token", "X-Watchdog-Id": "wd_test"})
    assert r.status_code == 200 and reg.holen("wd_test")["healthy"] is True


def test_richtiges_token_falsche_id_401(anlage):
    """Id angegeben, aber falsch → 401 (kein Scan-Fallback, wenn eine Id da ist)."""
    c, _w, _reg = anlage
    r = c.post("/api/watchdog/heartbeat", json={"healthy": True},
               headers={"X-Watchdog-Token": "gutes-token", "X-Watchdog-Id": "wd_falsch"})
    assert r.status_code == 401


def test_heartbeat_speichert_exo_error(anlage):
    """Meldet der Wächter einen EXO-Fehler, wird er im Wächter-Zustand festgehalten —
    Grundlage fürs Dashboard, das 'lebt, aber nicht handlungsfähig' anzeigt."""
    c, _w, reg = anlage
    r = c.post("/api/watchdog/heartbeat",
               json={"healthy": True, "bypass_active": False,
                     "exo_error": "AADSTS... role not yet effective"},
               headers={"X-Watchdog-Token": "gutes-token"})
    assert r.status_code == 200
    assert "role not yet effective" in reg.holen("wd_test").get("exo_error", "")


def test_zu_grosser_rumpf_413(anlage):
    c, _w, _reg = anlage
    r = c.post("/api/watchdog/heartbeat", content=b"x" * 2000,
               headers={"X-Watchdog-Token": "gutes-token",
                        "Content-Type": "application/json"})
    assert r.status_code == 413


def test_zwei_waechter_stoeren_sich_nicht(anlage):
    """Kern des Mehrwächter-Modells: jeder mit eigenem Token, getrennter Zustand."""
    c, _w, reg = anlage
    from webui import deps
    reg.registrieren(id="wd_zwei", name="Zwei", kind="azure",
                     token_hash=deps._hash_password("token-zwei"))
    c.post("/api/watchdog/heartbeat", json={"bypass_active": True},
           headers={"X-Watchdog-Token": "gutes-token", "X-Watchdog-Id": "wd_test"})
    c.post("/api/watchdog/heartbeat", json={"bypass_active": False, "healthy": True},
           headers={"X-Watchdog-Token": "token-zwei", "X-Watchdog-Id": "wd_zwei"})
    assert reg.holen("wd_test")["bypass_active"] is True
    assert reg.holen("wd_zwei")["bypass_active"] is False
    assert reg.holen("wd_zwei")["healthy"] is True
