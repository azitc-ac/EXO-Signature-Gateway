"""Anwendungsberechtigungen: Soll (setup_wizard-Specs) gegen Ist (Rollen im Token).

Der Punkt: „beantragt" ist nicht „erteilt". Der Status muss aus dem TOKEN kommen,
und das Beheben darf NUR Rollen erteilen — kein neues Secret, kein neues Zertifikat
(der volle Setup-Lauf erneuert beides bei jedem Aufruf).
"""
from __future__ import annotations

import asyncio
import base64
import json

import berechtigungen
import settings_store
import setup_wizard


def _jwt(roles):
    teil = base64.urlsafe_b64encode(json.dumps({"roles": roles}).encode()).decode().rstrip("=")
    return f"x.{teil}.y"


def test_status_aus_dem_token(monkeypatch):
    monkeypatch.setattr(berechtigungen, "graph_rollen", lambda: {"Mail.Send", "User.Read.All"})
    monkeypatch.setattr(berechtigungen, "exo_rollen", lambda: {"Exchange.ManageAsApp"})
    st = {b["name"]: b["erteilt"] for b in berechtigungen.status()}
    assert st["Mail.Send"] is True
    assert st["MailboxSettings.ReadWrite"] is False
    assert st["Exchange.ManageAsApp"] is True and st["IMAP.AccessAsApp"] is False
    alle = {s["name"] for s in setup_wizard._GRAPH_PERMISSION_SPECS + setup_wizard._EXO_PERMISSION_SPECS}
    assert set(st) == alle, "Status muss genau die beantragten Rollen zeigen"


def test_ohne_token_ist_nichts_bekannt_statt_fehlt(monkeypatch):
    monkeypatch.setattr(berechtigungen, "graph_rollen", lambda: None)
    monkeypatch.setattr(berechtigungen, "exo_rollen", lambda: None)
    assert {b["erteilt"] for b in berechtigungen.status()} == {None}


def test_nur_filtert(monkeypatch):
    monkeypatch.setattr(berechtigungen, "graph_rollen", lambda: set())
    monkeypatch.setattr(berechtigungen, "exo_rollen", lambda: set())
    namen = [b["name"] for b in berechtigungen.status(["Calendars.Read", "MailboxSettings.ReadWrite"])]
    assert namen == ["MailboxSettings.ReadWrite", "Calendars.Read"]


def test_token_rollen_liest_jwt():
    assert berechtigungen.token_rollen(_jwt(["A", "B"])) == {"A", "B"}
    assert berechtigungen.token_rollen("kein-jwt") is None


def test_nachziehen_erteilt_nur_rollen(monkeypatch):
    aufrufe = []

    async def gh(methode, url, token, json=None):
        aufrufe.append((methode, url.split("?")[0].rsplit("/", 2)[-2:], json))
        if methode == "get" and "/applications" in url:
            return {"value": [{"id": "obj1"}]}
        if methode == "get" and "/servicePrincipals" in url and "appId eq" in url:
            return {"value": [{"id": "sp1"}]}
        if methode == "get":
            return {"value": [{"id": "res"}]}
        if methode == "post" and "appRoleAssignments" in url and json["appRoleId"] == setup_wizard._GRAPH_PERMISSIONS[0]["id"]:
            raise RuntimeError("Permission being assigned already exists on the object")
        return {}
    monkeypatch.setattr(setup_wizard, "_gh", gh)
    monkeypatch.setattr(setup_wizard._gc, "reset_msal_app", lambda: None)
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: {"CLIENT_ID": "cid"}.get(k, d))
    res = asyncio.run(setup_wizard.berechtigungen_nachziehen("tok"))
    assert res == {"ok": True, "fehler": []}, "bereits erteilt ist kein Fehler"
    urls = " ".join("/".join(u) for _, u, _ in aufrufe)
    assert "addPassword" not in urls, "kein neues Secret"
    assert "keyCredentials" not in json.dumps([j for _, _, j in aufrufe if j]), "kein neues Zertifikat"
    erteilt = [j["appRoleId"] for m, _, j in aufrufe if m == "post"]
    soll = [p["id"] for p in setup_wizard._GRAPH_PERMISSIONS + setup_wizard._EXO_PERMISSIONS]
    assert erteilt == soll


def test_nachziehen_meldet_echte_fehler(monkeypatch):
    async def gh(methode, url, token, json=None):
        if methode == "get":
            return {"value": [{"id": "x"}]}
        if methode == "post":
            raise RuntimeError("Authorization_RequestDenied")
        return {}
    monkeypatch.setattr(setup_wizard, "_gh", gh)
    monkeypatch.setattr(setup_wizard._gc, "reset_msal_app", lambda: None)
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: {"CLIENT_ID": "cid"}.get(k, d))
    res = asyncio.run(setup_wizard.berechtigungen_nachziehen("tok"))
    assert res["ok"] is False and res["fehler"]
