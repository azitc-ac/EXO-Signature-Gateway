"""Teams-App bereitgestellt (TEAMS_APP_AKTIV): Fassung im Teams-Katalog.

Bis v1.10.1 ließ sich nicht prüfen, ob das hochgeladene Teams-Paket angekommen
ist — Graph verlangt dafür AppCatalog.Read.All. Die Rolle wird nur beantragt,
wenn die Teams-App bereitgestellt ist (Least Privilege).
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

import graph_client
import settings_store
import setup_wizard

APP_ID = "ab760ce4-510f-5777-bdfd-fb7fa4edcd32"
KAT_ID = "e12dae10-5a57-4817-b79d-dfbec5348930"       # AppCatalog.Read.All


def _graph(monkeypatch, status, value=None):
    urls = []

    class _Antwort:
        status_code = status
        text = "x"

        def json(self):
            return {"value": value or []}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def get(self, url, headers=None):
            urls.append(url)
            return _Antwort()

    async def tok():
        return "T"
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(graph_client, "_acquire_token_async", tok)
    return urls


def _app(*defs):
    return [{"appDefinitions": [{"version": v, "publishingState": s} for v, s in defs]}]


def test_hoechste_veroeffentlichte_fassung_zahlenweise(monkeypatch):
    """„1.9.156“ ist als Text größer als „1.10.0“ — verglichen wird als Zahl.
    Eine eingereichte, nicht veröffentlichte Fassung zählt nicht."""
    urls = _graph(monkeypatch, 200, _app(("1.9.156", "published"), ("1.10.0", "published"),
                                          ("2.0.0", "submitted")))
    assert asyncio.run(graph_client.teams_katalog(APP_ID)) == {"zustand": "ok", "version": "1.10.0"}
    assert APP_ID in urls[0] and "appCatalogs/teamsApps" in urls[0]


@pytest.mark.parametrize("status,value,zustand", [
    (403, None, "recht_fehlt"),
    (200, [], "nicht_gefunden"),
    (200, _app(("1.0.0", "submitted")), "nicht_gefunden"),
    (500, None, "fehler"),
])
def test_zustaende(monkeypatch, status, value, zustand):
    _graph(monkeypatch, status, value)
    assert asyncio.run(graph_client.teams_katalog(APP_ID))["zustand"] == zustand


def test_nur_eine_uuid_geht_in_den_filter(monkeypatch):
    urls = _graph(monkeypatch, 200, [])
    r = asyncio.run(graph_client.teams_katalog("x' or 1 eq 1 or id eq 'y"))
    assert r["zustand"] == "fehler" and urls == []


@pytest.mark.parametrize("wert,erwartet", [(None, False), (False, False), (True, True)])
def test_recht_nur_wenn_bereitgestellt(monkeypatch, wert, erwartet):
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: {"TEAMS_APP_AKTIV": wert}.get(k, d))
    assert (KAT_ID in [p["id"] for p in setup_wizard._graph_permissions()]) is erwartet
    spec = next(s for s in setup_wizard._GRAPH_PERMISSION_SPECS if s["id"] == KAT_ID)
    assert spec["name"] == "AppCatalog.Read.All"
    assert settings_store.DEFAULTS["TEAMS_APP_AKTIV"] is False
