"""Config-Endpunkt des Bypass-Wächters: Variante wählen + scharf stellen.

Invarianten:
  - Variante nur 'azure' oder 'cron' (sonst 400).
  - Aktivieren geht NUR mit gewählter Variante UND gesetztem Token — sonst liefe
    ein Wächter ins Leere (400). Deaktivieren geht immer.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

pytest.importorskip("starlette.testclient", reason="httpx wird für TestClient benötigt")


@pytest.fixture
def anlage(monkeypatch):
    from starlette.testclient import TestClient
    from webui import app as wa
    from webui import deps
    import settings_store

    werte = {"WATCHDOG_TOKEN_HASH": "", "WATCHDOG_KIND": "", "WATCHDOG_ENABLED": False}
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))
    monkeypatch.setattr(settings_store, "update", lambda upd: werte.update(upd))
    wa.app.dependency_overrides[deps._require_admin] = lambda: "tester"
    try:
        with TestClient(wa.app) as c:
            yield c, werte
    finally:
        wa.app.dependency_overrides.clear()


def test_ungueltige_variante_400(anlage):
    c, werte = anlage
    r = c.post("/api/watchdog/config", json={"kind": "irgendwas"})
    assert r.status_code == 400
    assert werte["WATCHDOG_KIND"] == ""          # nichts gespeichert


def test_variante_speichern(anlage):
    c, werte = anlage
    r = c.post("/api/watchdog/config", json={"kind": "azure"})
    assert r.status_code == 200 and r.json()["kind"] == "azure"
    assert werte["WATCHDOG_KIND"] == "azure"


def test_aktivieren_ohne_variante_400(anlage):
    c, werte = anlage
    werte["WATCHDOG_TOKEN_HASH"] = "hash"        # Token da, aber keine Variante
    r = c.post("/api/watchdog/config", json={"enabled": True})
    assert r.status_code == 400
    assert werte["WATCHDOG_ENABLED"] is False


def test_aktivieren_ohne_token_400(anlage):
    c, werte = anlage
    werte["WATCHDOG_KIND"] = "cron"              # Variante da, aber kein Token
    r = c.post("/api/watchdog/config", json={"enabled": True})
    assert r.status_code == 400
    assert werte["WATCHDOG_ENABLED"] is False


def test_aktivieren_mit_variante_und_token(anlage):
    c, werte = anlage
    werte["WATCHDOG_KIND"] = "azure"
    werte["WATCHDOG_TOKEN_HASH"] = "hash"
    r = c.post("/api/watchdog/config", json={"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert werte["WATCHDOG_ENABLED"] is True


def test_variante_und_aktivieren_in_einem_aufruf(anlage):
    """kind im selben Request zählt fürs Gate (nicht nur der gespeicherte Wert)."""
    c, werte = anlage
    werte["WATCHDOG_TOKEN_HASH"] = "hash"
    r = c.post("/api/watchdog/config", json={"kind": "cron", "enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True


def test_deaktivieren_immer_moeglich(anlage):
    c, werte = anlage
    werte["WATCHDOG_ENABLED"] = True
    r = c.post("/api/watchdog/config", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert werte["WATCHDOG_ENABLED"] is False


def test_grant_role_lehnt_ungueltige_guids_ab(anlage):
    c, _ = anlage
    r = c.post("/api/watchdog/grant-role",
               json={"watchdog_app_id": "kein-guid", "watchdog_object_id": "auch-nicht"})
    assert r.status_code == 400


def test_start_grants_lehnt_ungueltige_guid_ab(anlage):
    c, _ = anlage
    r = c.get("/api/watchdog/start-grants?mi_object_id=nope")
    assert r.status_code == 400


def test_grant_role_ruft_skript_mit_mi_ids(anlage, monkeypatch):
    """Valide GUIDs → grant_watchdog_role.ps1 wird mit den MI-IDs aufgerufen."""
    c, _ = anlage
    from pathlib import Path as _P
    import subprocess as sp
    import config
    import settings_store
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a: {"TENANT_DOMAIN": "t.onmicrosoft.com",
                                       "CLIENT_ID": "gw-app"}.get(k, ""))
    monkeypatch.setattr(config, "CLIENT_ID", "gw-app", raising=False)
    monkeypatch.setattr(_P, "exists", lambda self: True)
    cap = {}

    class _Proc:
        returncode = 0
        stdout = "GRANT-ROLE-OK\n[OK] Rolle 'Transport Rules' zugewiesen."
        stderr = ""
    monkeypatch.setattr(sp, "run", lambda cmd, **k: cap.update(cmd=cmd) or _Proc())

    app_id = "ffbf6e48-afd7-48ed-8e05-d44c0e99ee58"
    obj_id = "775da29a-df34-4880-afe6-089369a12cde"
    r = c.post("/api/watchdog/grant-role",
               json={"watchdog_app_id": app_id, "watchdog_object_id": obj_id})
    assert r.status_code == 200 and r.json()["ok"] is True
    cmd = cap["cmd"]
    assert "-WatchdogAppId" in cmd and app_id in cmd
    assert "-WatchdogObjectId" in cmd and obj_id in cmd
    assert "grant_watchdog_role.ps1" in " ".join(cmd)
