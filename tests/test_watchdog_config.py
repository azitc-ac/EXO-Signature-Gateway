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
def anlage(monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    from webui import app as wa
    from webui import deps
    import settings_store
    import waechter_register

    werte = {"WATCHDOG_TOKEN_HASH": "", "WATCHDOG_KIND": "", "WATCHDOG_ENABLED": False}
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))
    monkeypatch.setattr(settings_store, "update", lambda upd: werte.update(upd))
    # Register isolieren, damit anzahl()/liste() im Test deterministisch sind.
    monkeypatch.setattr(waechter_register, "PFAD", tmp_path / "watchdog_watchers.json")
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


def test_aktivieren_ohne_waechter_400(anlage):
    """Kein registrierter Wächter und kein Legacy-Token → Aktivieren abgelehnt."""
    c, werte = anlage
    r = c.post("/api/watchdog/config", json={"enabled": True})
    assert r.status_code == 400
    assert werte["WATCHDOG_ENABLED"] is False


def test_aktivieren_mit_legacy_token_ok(anlage):
    """Legacy-Token (vor dem Register) zählt weiterhin als Wächter."""
    c, werte = anlage
    werte["WATCHDOG_TOKEN_HASH"] = "hash"
    r = c.post("/api/watchdog/config", json={"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert werte["WATCHDOG_ENABLED"] is True


def test_aktivieren_mit_registriertem_waechter_ok(anlage, monkeypatch):
    """Ein registrierter Wächter genügt fürs Aktivieren — auch ohne Legacy-Token."""
    c, werte = anlage
    import waechter_register
    from webui import deps
    waechter_register.registrieren(id="wd_x", name="X", kind="azure",
                                   token_hash=deps._hash_password("t"))
    r = c.post("/api/watchdog/config", json={"enabled": True})
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


def _reset_deploy():
    from webui.routen import waechter
    waechter._deploy.update(running=False, ok=None, step="", msg="",
                            principal_id="", host="", app="")


def test_deploy_start_fehlende_felder_400(anlage):
    _reset_deploy()
    c, _ = anlage
    r = c.post("/api/watchdog/deploy/start", json={"subscription_id": "x"})
    assert r.status_code == 400


def test_deploy_start_ungueltige_subscription_guid_400(anlage, monkeypatch):
    _reset_deploy()
    c, _ = anlage
    from webui.routen import waechter
    monkeypatch.setattr(waechter, "keyvault_arm_ok", lambda upn: True)
    r = c.post("/api/watchdog/deploy/start", json={
        "subscription_id": "kein-guid", "resource_group": "rg",
        "location": "westeurope", "app_name": "exo-sig-watchdog"})
    assert r.status_code == 400
    assert "GUID" in r.json()["detail"]


def test_deploy_start_ungueltiger_app_name_400(anlage, monkeypatch):
    _reset_deploy()
    c, _ = anlage
    from webui.routen import waechter
    monkeypatch.setattr(waechter, "keyvault_arm_ok", lambda upn: True)
    r = c.post("/api/watchdog/deploy/start", json={
        "subscription_id": "775da29a-df34-4880-afe6-089369a12cde",
        "resource_group": "rg", "location": "westeurope",
        "app_name": "-ungueltig-"})       # beginnt mit Bindestrich
    assert r.status_code == 400


def test_deploy_start_ohne_azure_zugriff_400(anlage, monkeypatch):
    """Ohne delegierten ARM-Token (kein Azure-Login) → 400, kein Task."""
    _reset_deploy()
    c, _ = anlage
    from webui.routen import waechter
    monkeypatch.setattr(waechter, "keyvault_arm_ok", lambda upn: False)
    r = c.post("/api/watchdog/deploy/start", json={
        "subscription_id": "775da29a-df34-4880-afe6-089369a12cde",
        "resource_group": "rg", "location": "westeurope",
        "app_name": "exo-sig-watchdog"})
    assert r.status_code == 400
    assert waechter._deploy["running"] is False


def test_deploy_start_409_wenn_schon_laeuft(anlage, monkeypatch):
    c, _ = anlage
    from webui.routen import waechter
    waechter._deploy.update(running=True)
    try:
        r = c.post("/api/watchdog/deploy/start", json={
            "subscription_id": "775da29a-df34-4880-afe6-089369a12cde",
            "resource_group": "rg", "location": "westeurope",
            "app_name": "exo-sig-watchdog"})
        assert r.status_code == 409
    finally:
        _reset_deploy()


def test_deploy_start_valide_startet_task(anlage, monkeypatch):
    """Valide Eingabe + Azure-Zugriff → 200, Hintergrund-Orchestrator wird gerufen."""
    _reset_deploy()
    c, _ = anlage
    from webui.routen import waechter
    monkeypatch.setattr(waechter, "keyvault_arm_ok", lambda upn: True)
    monkeypatch.setattr(waechter, "_get_session_user", lambda req: "admin@t.de")
    gerufen = {}

    async def _fake_run(upn, sub, rg, loc, app_name, create_rg):
        gerufen.update(upn=upn, sub=sub, rg=rg, loc=loc, app=app_name, create_rg=create_rg)
    monkeypatch.setattr(waechter, "_run_deploy", _fake_run)
    try:
        r = c.post("/api/watchdog/deploy/start", json={
            "subscription_id": "775da29a-df34-4880-afe6-089369a12cde",
            "resource_group": "rg-neu", "location": "northeurope",
            "app_name": "exo-sig-watchdog", "create_rg": True})
        assert r.status_code == 200 and r.json()["started"] is True
    finally:
        _reset_deploy()
    assert gerufen["sub"] == "775da29a-df34-4880-afe6-089369a12cde"
    assert gerufen["app"] == "exo-sig-watchdog" and gerufen["create_rg"] is True


def test_deploy_status_liefert_zustandsform(anlage):
    _reset_deploy()
    c, _ = anlage
    r = c.get("/api/watchdog/deploy/status")
    assert r.status_code == 200
    d = r.json()
    for k in ("running", "step", "ok", "msg", "principal_id", "host", "app"):
        assert k in d


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
