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
    r = asyncio.run(graph_client.teams_katalog(APP_ID))
    assert (r["zustand"], r["version"], r["eingereicht"]) == ("ok", "1.10.0", "2.0.0")
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


# ── „Im Teams-Katalog aktualisieren“ (delegiert, AppCatalog.ReadWrite.All) ──

def _katalog_app(*defs):
    return [{"id": KATALOG_ID,
             "appDefinitions": [{"version": v, "publishingState": z} for v, z in defs]}]


KATALOG_ID = "11111111-2222-3333-4444-555555555555"   # id der teamsApp, ≠ externalId


def _graph_schreibend(monkeypatch, lesungen, post_status=204):
    """Jedes GET liefert die nächste Lesung; POST zeichnet Adresse und Rumpf auf."""
    aufrufe = {"get": [], "post": []}

    class _Antwort:
        def __init__(self, status, value=None):
            self.status_code, self._v, self.text = status, value, "x"

        def json(self):
            return {"value": self._v or []}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def get(self, url, headers=None):
            aufrufe["get"].append((url, headers))
            status, value = lesungen.pop(0)
            return _Antwort(status, value)

        async def post(self, url, headers=None, content=None):
            aufrufe["post"].append((url, headers, content))
            return _Antwort(post_status)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return aufrufe


def test_aktualisieren_laedt_an_die_katalog_id_und_liest_neu(monkeypatch):
    aufrufe = _graph_schreibend(monkeypatch, [
        (200, _katalog_app(("1.11.0", "published"))),
        (200, _katalog_app(("1.11.0", "published"), ("1.12.45", "published"))),
    ])
    r = asyncio.run(graph_client.teams_katalog_aktualisieren("DELEG", APP_ID, b"ZIP"))
    assert r == {"ok": True, "msg": "", "version": "1.12.45"}
    url, kopf, rumpf = aufrufe["post"][0]
    assert url.endswith(f"/appCatalogs/teamsApps/{KATALOG_ID}/appDefinitions")
    assert kopf["Content-Type"] == "application/zip" and kopf["Authorization"] == "Bearer DELEG"
    assert rumpf == b"ZIP"
    assert len(aufrufe["get"]) == 2                      # Erfolg = neu gelesen


def test_nicht_im_katalog_laedt_nichts_hoch(monkeypatch):
    aufrufe = _graph_schreibend(monkeypatch, [(200, [])])
    r = asyncio.run(graph_client.teams_katalog_aktualisieren("T", APP_ID, b"ZIP"))
    assert not r["ok"] and "Integrierte Apps" in r["msg"] and aufrufe["post"] == []


def test_nur_eingereicht_ist_kein_erfolg(monkeypatch):
    """Ohne Admin-Rolle landet die Fassung als „submitted“ — veröffentlicht ist sie nicht."""
    _graph_schreibend(monkeypatch, [
        (200, _katalog_app(("1.11.0", "published"))),
        (200, _katalog_app(("1.11.0", "published"), ("1.12.45", "submitted"))),
    ])
    r = asyncio.run(graph_client.teams_katalog_aktualisieren("T", APP_ID, b"ZIP"))
    assert not r["ok"] and "eingereicht" in r["msg"] and r["version"] == "1.11.0"


def test_403_nennt_die_rolle(monkeypatch):
    _graph_schreibend(monkeypatch, [(200, _katalog_app(("1.11.0", "published")))], post_status=403)
    r = asyncio.run(graph_client.teams_katalog_aktualisieren("T", APP_ID, b"ZIP"))
    assert not r["ok"] and "Teams-Administrator" in r["msg"] and "app catalogs" in r["msg"]


def test_aktualisieren_nur_mit_uuid(monkeypatch):
    aufrufe = _graph_schreibend(monkeypatch, [])
    r = asyncio.run(graph_client.teams_katalog_aktualisieren("T", "x' or 1 eq 1", b"ZIP"))
    assert not r["ok"] and aufrufe == {"get": [], "post": []}


def test_nachlauf_laedt_dasselbe_paket_wie_der_download(monkeypatch):
    """In den Katalog geht, was der Download liefert — mit der Adresse vom Klick."""
    from webui.routen import addin, anmeldung
    gesehen = {}

    async def fake(token, externe_id, paket):
        gesehen.update(token=token, id=externe_id, paket=paket)
        return {"ok": True, "msg": "", "version": "9.9.9"}
    monkeypatch.setattr(graph_client, "teams_katalog_aktualisieren", fake)
    res = asyncio.run(anmeldung.nachlauf(
        "teams_katalog", {"extra": {"base": "https://gw.firma.at"}}, "DELEG"))
    paket, m = addin.teams_app_paket("https://gw.firma.at")
    assert res["ok"] and res["version"] == "9.9.9"
    assert gesehen["token"] == "DELEG" and gesehen["id"] == m["id"]
    import io, json, zipfile
    a = zipfile.ZipFile(io.BytesIO(gesehen["paket"])).read("manifest.json")
    assert json.loads(a) == json.loads(zipfile.ZipFile(io.BytesIO(paket)).read("manifest.json"))
    assert "teams_katalog" in anmeldung.NACHLAUF_ABLAEUFE      # auch per Einfügen abschliessbar


def test_anmeldung_fragt_appcatalog_readwrite_an():
    """AppCatalog.Submit erlaubt nur Einreichen — gemessen 16.11.2026: 403
    beim Veröffentlichen, auch als Global Admin. Genau diese eine Berechtigung."""
    import pkce
    assert pkce.TEAMS_KATALOG_SCOPES == ["https://graph.microsoft.com/AppCatalog.ReadWrite.All"]
