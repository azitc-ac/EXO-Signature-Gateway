"""get_user cacht NUR das Graph-Profil, nicht die settings-abhängigen Felder.

Grund (2026-10-03): Der OOO-Poll ruft get_user je Postfach je Durchlauf. Ungecacht
ist das je Postfach ein Graph-Call alle 10 min — skaliert nicht auf Tausende. Der
TTL-Cache halbiert die Poll-Last. ⚠️ Er darf aber NUR die Graph-Daten cachen: Admin-
Änderungen an Overrides/Gruppen-/Custom-Variablen müssen sofort greifen, nicht erst
nach Cache-Ablauf.
"""
import asyncio

import graph_client as gc


class _FakeResp:
    status_code = 200
    def __init__(self, data): self._d = data
    def raise_for_status(self): pass
    def json(self): return self._d


class _FakeClient:
    gets = 0
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, url, headers=None):
        _FakeClient.gets += 1
        return _FakeResp({"displayName": "Erika", "mail": "erika@x.de", "businessPhones": []})


def _token(*a, **k):
    async def f(*_a, **_k): return "tok"
    return f


def _setup(monkeypatch, store):
    gc._user_data_cache.clear()
    _FakeClient.gets = 0
    monkeypatch.setattr(gc, "_acquire_token_async", _token())
    monkeypatch.setattr(gc.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(gc.settings_store, "get", lambda k, d=None: store.get(k, d))


def test_profil_wird_gecacht(monkeypatch):
    """Zwei get_user-Aufrufe → nur EIN Graph-Call."""
    _setup(monkeypatch, {})
    u1 = asyncio.run(gc.get_user("erika@x.de"))
    u2 = asyncio.run(gc.get_user("erika@x.de"))
    assert u1.displayName == "Erika" and u2.displayName == "Erika"
    assert _FakeClient.gets == 1, f"erwartete 1 Graph-Call, waren {_FakeClient.gets}"


def test_settings_bleiben_live_trotz_cache(monkeypatch):
    """Nach dem ersten (cachenden) Aufruf eine USER_OVERRIDE setzen → der zweite
    Aufruf spiegelt sie SOFORT, obwohl das Profil aus dem Cache kommt."""
    store = {"USER_OVERRIDES": {}}
    _setup(monkeypatch, store)
    u1 = asyncio.run(gc.get_user("erika@x.de"))
    assert u1.jobTitle == ""                                   # noch kein Override
    store["USER_OVERRIDES"] = {"erika@x.de": {"user.jobTitle": "Chefin"}}
    u2 = asyncio.run(gc.get_user("erika@x.de"))
    assert u2.jobTitle == "Chefin"                             # greift ohne Cache-Ablauf
    assert _FakeClient.gets == 1                               # Profil nur einmal geholt
