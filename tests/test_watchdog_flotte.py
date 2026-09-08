"""Mehrwächter-Endpunkte: Rückbau (azure/cron) + cron-Bundle + ARM-Delete.

- cron entfernen: aus dem Register nehmen, Entfern-Befehle zurückgeben.
- azure entfernen: braucht Azure-Zugriff; löscht die angelegten Ressourcen.
- cron/generate: registriert einen Wächter + liefert eine vorbefüllte watchdog.env.
- delete_resources: RG-Löschen nur bei selbst angelegter RG, sonst Einzelressourcen.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

pytest.importorskip("starlette.testclient", reason="httpx wird für TestClient benötigt")

import watchdog_deploy as wd


@pytest.fixture
def anlage(monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    from webui import app as wa
    from webui import deps
    from webui.routen import waechter
    import waechter_register
    import settings_store

    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a, **kw: {"TENANT_DOMAIN": "t.onmicrosoft.com",
                                             "ADDIN_BASE_URL": "https://sig.example"}.get(k, ""))
    monkeypatch.setattr(waechter_register, "PFAD", tmp_path / "watchdog_watchers.json")
    wa.app.dependency_overrides[deps._require_admin] = lambda: "tester"
    try:
        with TestClient(wa.app) as c:
            yield c, waechter, waechter_register
    finally:
        wa.app.dependency_overrides.clear()


# ── Rückbau ────────────────────────────────────────────────────────────────

def test_remove_unbekannt_404(anlage):
    c, _w, _reg = anlage
    r = c.post("/api/watchdog/remove", json={"id": "gibtsnicht"})
    assert r.status_code == 404


def test_remove_cron_gibt_befehle(anlage):
    c, _w, reg = anlage
    reg.registrieren(id="wd_c", name="cron1", kind="cron", token_hash="h")
    r = c.post("/api/watchdog/remove", json={"id": "wd_c"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] and d["kind"] == "cron" and "systemctl" in d["befehle"]
    assert reg.holen("wd_c") is None                 # aus dem Register genommen


def test_remove_azure_ohne_zugriff_400_behaelt_eintrag(anlage, monkeypatch):
    c, waechter, reg = anlage
    monkeypatch.setattr(waechter, "keyvault_arm_ok", lambda upn: False)
    reg.registrieren(id="wd_az", name="az1", kind="azure", token_hash="h",
                     azure={"subscription": "s", "resource_group": "rg", "app_name": "app"})
    r = c.post("/api/watchdog/remove", json={"id": "wd_az"})
    assert r.status_code == 400 and "Azure-Zugriff" in r.json()["detail"]
    assert reg.holen("wd_az") is not None            # NICHT entfernt, Ressourcen stehen noch


def test_remove_azure_loescht_und_entfernt(anlage, monkeypatch):
    c, waechter, reg = anlage
    monkeypatch.setattr(waechter, "keyvault_arm_ok", lambda upn: True)
    monkeypatch.setattr(waechter, "_get_session_user", lambda req: "admin@t.de")
    import keyvault
    monkeypatch.setattr(keyvault, "get_user_arm_token", lambda upn: "tok")
    gesehen = {}

    async def _fake_delete(sub, rg, app, storage, plan, created_rg, token):
        gesehen.update(sub=sub, rg=rg, app=app, created_rg=created_rg)
        return True, "ok"
    monkeypatch.setattr(wd, "delete_resources", _fake_delete)
    reg.registrieren(id="wd_az", name="az1", kind="azure", token_hash="h",
                     azure={"subscription": "s1", "resource_group": "rg1", "app_name": "app1",
                            "storage": "st1", "plan": "pl1", "created_rg": True})
    r = c.post("/api/watchdog/remove", json={"id": "wd_az"})
    assert r.status_code == 200 and r.json()["ok"]
    assert gesehen["sub"] == "s1" and gesehen["created_rg"] is True
    assert reg.holen("wd_az") is None


def test_remove_azure_ohne_metadaten_nur_register(anlage, monkeypatch):
    """Migrierter Legacy-Azure-Wächter ohne Rückbau-Daten: nur aus dem Register."""
    c, waechter, reg = anlage
    monkeypatch.setattr(waechter, "keyvault_arm_ok", lambda upn: True)
    reg.registrieren(id="wd_legacy", name="alt", kind="azure", token_hash="h")  # azure={} leer
    r = c.post("/api/watchdog/remove", json={"id": "wd_legacy"})
    assert r.status_code == 200 and r.json().get("nur_register") is True
    assert reg.holen("wd_legacy") is None


# ── cron-Generator ───────────────────────────────────────────────────────────

def test_cron_generate_registriert_und_liefert_env(anlage):
    c, _w, reg = anlage
    r = c.post("/api/watchdog/cron/generate", json={"name": "host2"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] and d["id"].startswith("wd_") and d["token"]
    assert "WATCHDOG_TOKEN=" + d["token"] in d["env"]
    assert "WATCHDOG_ID=" + d["id"] in d["env"]
    assert "EXO_ORGANIZATION=t.onmicrosoft.com" in d["env"]
    e = reg.holen(d["id"])
    assert e and e["kind"] == "cron" and e["name"] == "host2"


# ── delete_resources (ARM) ───────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _fake_arm(calls):
    async def arm(method, url, token, body=None, timeout=60):
        calls.append((method, url))
        return _Resp(202)
    return arm


def test_delete_resources_created_rg_loescht_nur_rg(monkeypatch):
    calls = []
    monkeypatch.setattr(wd, "_arm", _fake_arm(calls))
    ok, msg = asyncio.run(wd.delete_resources("s", "rg", "app", "st", "pl", True, "tok"))
    assert ok, msg
    assert len(calls) == 1 and calls[0][0] == "delete" and "resourceGroups/rg?" in calls[0][1]


def test_delete_resources_bestehende_rg_einzeln_in_reihenfolge(monkeypatch):
    calls = []
    monkeypatch.setattr(wd, "_arm", _fake_arm(calls))
    ok, msg = asyncio.run(wd.delete_resources("s", "rg", "app", "st", "pl", False, "tok"))
    assert ok, msg
    urls = [u for _, u in calls]
    assert len(urls) == 3
    assert "sites/app" in urls[0] and "serverfarms/pl" in urls[1] and "storageAccounts/st" in urls[2]
    # die RG selbst wird NIE angefasst, wenn sie nicht selbst angelegt wurde
    assert not any(u.rstrip("?").endswith("resourceGroups/rg") for u in urls)
