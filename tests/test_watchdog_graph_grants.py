"""Bypass-Wächter: die zwei GRAPH-Zuweisungen der MI (App-Rolle + Global Reader)
über den delegierten Admin-Token — wie die App-Registrierung, kein neues stehendes
Recht fürs Gateway. Geprüft wird, dass die richtigen Graph-Aufrufe abgesetzt werden
und bereits vorhandene Zuweisungen als Erfolg gelten (idempotent).
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import setup_wizard


def test_grant_graph_roles_setzt_approle_und_global_reader(monkeypatch):
    calls = []

    async def fake_gh(method, url, token, **kw):
        calls.append((method, url, kw.get("json")))
        # Der abschliessende GET holt die AppId der MI (fürs UI-Autofill).
        if method == "get" and "servicePrincipals/" in url:
            return {"appId": "mi-app-id-123"}
        return {}

    async def fake_sp(token, app_id):
        return "exo-sp-id"

    monkeypatch.setattr(setup_wizard, "_gh", fake_gh)
    monkeypatch.setattr(setup_wizard, "_get_sp_id_for_resource", fake_sp)

    res = asyncio.run(setup_wizard.grant_watchdog_graph_roles("tok", "mi-obj-id"))
    assert res["app_role"] and res["global_reader"] and not res["fehler"]
    assert res["app_id"] == "mi-app-id-123"                          # AppId aufgelöst

    approle = [c for c in calls if "appRoleAssignments" in c[1]][0]
    assert "mi-obj-id" in approle[1]                                  # auf der MI
    assert approle[2]["appRoleId"] == "dc50a0fb-09a3-484d-be87-e023b12c6440"  # Exchange.ManageAsApp
    assert approle[2]["resourceId"] == "exo-sp-id"                    # Ressource = EXO

    gr = [c for c in calls if "roleManagement/directory/roleAssignments" in c[1]][0]
    assert gr[2]["roleDefinitionId"] == "f2ef992c-3afb-46b9-b7cf-a126ee74c451"  # Global Reader
    assert gr[2]["principalId"] == "mi-obj-id"


def test_grant_graph_roles_idempotent(monkeypatch):
    async def fake_gh(method, url, token, **kw):
        raise RuntimeError("400 ... appRoleAssignment already exists ...")

    async def fake_sp(token, app_id):
        return "exo-sp-id"

    monkeypatch.setattr(setup_wizard, "_gh", fake_gh)
    monkeypatch.setattr(setup_wizard, "_get_sp_id_for_resource", fake_sp)

    res = asyncio.run(setup_wizard.grant_watchdog_graph_roles("tok", "mi-obj-id"))
    assert res["app_role"] and res["global_reader"]      # „already exists" → Erfolg


def test_grant_graph_roles_meldet_echten_fehler(monkeypatch):
    async def fake_gh(method, url, token, **kw):
        raise RuntimeError("403 Forbidden")

    async def fake_sp(token, app_id):
        return "exo-sp-id"

    monkeypatch.setattr(setup_wizard, "_gh", fake_gh)
    monkeypatch.setattr(setup_wizard, "_get_sp_id_for_resource", fake_sp)

    res = asyncio.run(setup_wizard.grant_watchdog_graph_roles("tok", "mi-obj-id"))
    assert not res["app_role"] and not res["global_reader"]
    assert len(res["fehler"]) == 2
