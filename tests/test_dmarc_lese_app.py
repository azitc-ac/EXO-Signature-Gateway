"""DMARC-Lese-App: eigener Lebenszyklus über „Fehlende Berechtigungen erteilen“.

Mit aktiver Postfach-Eingrenzung (ApplicationAccessPolicy) ist eine M365-Gruppe
für die Haupt-App unlesbar; eine zweite App nur mit Group-Conversation.Read.All
ist von der Policy nicht erfasst. Anlegen, Zertifikat erneuern und Löschen
laufen über dieselben Wege wie bei der Haupt-App.
"""
from __future__ import annotations

import asyncio

import pytest

import dmarc_lese_app as la
import settings_store
import setup_wizard


@pytest.fixture
def nb(monkeypatch):
    d = {"DMARC_POSTFACH": "dmarc@firma.at", "APP_ACCESS_POLICY_ENABLED": True,
         "_DMARC_ZIEL": {"adresse": "dmarc@firma.at", "art": "gruppe"}, "GATEWAY_NAME": "Gateway Muster"}
    monkeypatch.setattr(settings_store, "get", lambda k, v=None: d.get(k, v))
    monkeypatch.setattr(settings_store, "force_update", lambda p: d.update(p))
    monkeypatch.setattr(setup_wizard, "_auth_zert_der", lambda: b"DER")
    monkeypatch.setattr(setup_wizard, "_zert_zeitraum", lambda der: ("2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z"))
    return d


class _Entra:
    """Attrappe für setup_wizard._gh: merkt Aufrufe, kennt angelegte Apps."""
    def __init__(self):
        self.aufrufe, self.apps, self.sps, self.keys = [], {}, {}, []

    async def __call__(self, method, url, token, **kw):
        self.aufrufe.append((method, url.split("?")[0].replace(la._GRAPH, "")))
        pfad = url.replace(la._GRAPH, "")
        if method == "post" and pfad == "/applications":
            assert kw["json"]["requiredResourceAccess"][0]["resourceAccess"] == [
                {"id": la.ROLLE_ID, "type": "Role"}], "nur die eine Rolle"
            self.apps["obj1"] = "cid1"
            return {"id": "obj1", "appId": "cid1"}
        if method == "get" and pfad.startswith("/applications?"):
            if "displayName" in pfad:
                return {"value": []}
            return {"value": [{"id": o} for o, c in self.apps.items() if c in pfad]}
        if method == "patch" and pfad.startswith("/applications/"):
            self.keys.append(pfad.split("/")[2])
            return {}
        if method == "get" and pfad.startswith("/servicePrincipals?"):
            if "cid1" in pfad:
                return {"value": [{"id": s} for s in self.sps]}
            return {"value": [{"id": "graph-sp"}]}
        if method == "post" and pfad == "/servicePrincipals":
            self.sps["sp1"] = kw["json"]["appId"]
            return {"id": "sp1"}
        if method == "post" and pfad.endswith("/appRoleAssignments"):
            assert kw["json"]["appRoleId"] == la.ROLLE_ID and kw["json"]["principalId"] == "sp1"
            return {}
        if method == "delete":
            self.apps.pop(pfad.split("/")[2], None)
            return {}
        raise AssertionError(f"unerwartet: {method} {pfad}")


def test_anlegen_ist_vollstaendig_und_wiederholbar(nb, monkeypatch):
    e = _Entra()
    monkeypatch.setattr(setup_wizard, "_gh", e)
    assert asyncio.run(la.abgleichen("adm")) == []
    assert nb[la.KEY] == {"client_id": "cid1", "objekt_id": "obj1"}
    assert ("post", "/applications") in e.aufrufe and ("post", "/servicePrincipals") in e.aufrufe
    assert e.keys == ["obj1"], "Zertifikat muss an die Lese-App"
    assert any(a[1].endswith("/appRoleAssignments") for a in e.aufrufe)
    e.aufrufe.clear()
    assert asyncio.run(la.abgleichen("adm")) == []
    assert ("post", "/applications") not in e.aufrufe, "zweiter Lauf legt nichts neu an"
    assert ("post", "/servicePrincipals") not in e.aufrufe


def test_nicht_mehr_noetig_wird_geloescht(nb, monkeypatch):
    e = _Entra()
    monkeypatch.setattr(setup_wizard, "_gh", e)
    asyncio.run(la.abgleichen("adm"))
    nb["APP_ACCESS_POLICY_ENABLED"] = False          # Eingrenzung aus → Haupt-App liest selbst
    assert la.status_zeile()["benoetigt"] is False   # noch da → als „nicht benötigt“ sichtbar
    assert asyncio.run(la.abgleichen("adm")) == []
    assert ("delete", "/applications/obj1") in e.aufrufe and nb[la.KEY] == {}
    assert la.status_zeile() is None


def test_ohne_eingrenzung_oder_postfach_keine_lese_app(nb, monkeypatch):
    e = _Entra()
    monkeypatch.setattr(setup_wizard, "_gh", e)
    nb["_DMARC_ZIEL"] = {"adresse": "dmarc@firma.at", "art": "postfach"}
    assert not la.noetig() and la.status_zeile() is None
    asyncio.run(la.abgleichen("adm"))
    assert e.aufrufe == []


def test_neues_zertifikat_geht_auch_an_die_lese_app(nb, monkeypatch):
    nb[la.KEY] = {"client_id": "cid1", "objekt_id": "obj-lese"}
    nb["APP_POOL"] = []
    hochgeladen = []

    async def upload(token, obj, der):
        hochgeladen.append(obj)
    monkeypatch.setattr(setup_wizard, "_upload_key_credential", upload)
    asyncio.run(setup_wizard._zert_an_alle_apps("adm", "obj-haupt", b"DER"))
    assert hochgeladen == ["obj-haupt", "obj-lese"]


def test_berechtigungen_nachziehen_gleicht_die_lese_app_ab(nb, monkeypatch):
    nb["CLIENT_ID"] = "haupt"
    gerufen = []

    async def gh(method, url, token, **kw):
        return {"value": [{"id": "x"}]}

    async def abgleich(token):
        gerufen.append(token)
        return []

    async def consent(token, sp):
        return []
    monkeypatch.setattr(setup_wizard, "_gh", gh)
    monkeypatch.setattr(setup_wizard, "_grant_admin_consent", consent)
    monkeypatch.setattr(la, "abgleichen", abgleich)
    assert asyncio.run(setup_wizard.berechtigungen_nachziehen("adm"))["ok"]
    assert gerufen == ["adm"]


def test_hauptapp_verlangt_gruppenrolle_nicht_bei_eingrenzung(nb, monkeypatch):
    import dmarc_berichte as dm
    import test_dmarc_berichte as t
    import graph_client
    monkeypatch.setattr(graph_client, "_acquire_token", lambda: "tok")
    monkeypatch.setattr(dm.httpx, "Client", t._Graph("gruppe", b""))
    dm.ziel_aufloesen()
    assert nb[dm.GRUPPE_KEY] is False
    nb["APP_ACCESS_POLICY_ENABLED"] = False
    dm.ziel_aufloesen()
    assert nb[dm.GRUPPE_KEY] is True
