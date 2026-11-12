"""Gesendete Elemente: Suchverzug darf kein Duplikat stehen lassen.

ANLASS (12.11.2026, VM v1.11.58, Graph++): sendMail-Kopie um :41 angelegt, die
$filter-Suche um :50 fand nur das Original. Der Einzel-Element-Zweig patchte es
und meldete „fertig“ — danach standen Original und signierte Kopie nebeneinander.

Erwartet: Ein Element → nachbessern, aber weiter suchen; taucht die Kopie im
nächsten Durchgang auf, wird das Original gelöscht und die Kopie bleibt.
"""
import asyncio

import graph_client
import handler


class _Resp:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._d = data or {}
        self.text = ""
        self.is_success = status < 400

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def _postfach(durchgaenge, geloescht, gepatcht):
    """Jeder GET liefert den nächsten Stand der Gesendeten Elemente."""
    staende = iter(durchgaenge)

    class C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return _Resp(200, {"value": [dict(x) for x in next(staende)]})

        async def patch(self, url, headers=None, json=None):
            gepatcht.append(url.rsplit("/", 1)[-1])
            return _Resp(200)

        async def delete(self, url, headers=None):
            geloescht.append(url.rsplit("/", 1)[-1])
            return _Resp(204)
    return C


ORIG = {"id": "orig", "createdDateTime": "2026-11-12T14:18:37Z"}
KOPIE = {"id": "kopie", "createdDateTime": "2026-11-12T14:18:41Z"}


def _lauf(monkeypatch, durchgaenge):
    geloescht, gepatcht = [], []

    async def tok():
        return "T"

    async def schlafen(_s):
        return None
    monkeypatch.setattr(graph_client, "_acquire_token_async", tok)
    monkeypatch.setattr(graph_client.httpx, "AsyncClient", _postfach(durchgaenge, geloescht, gepatcht))
    monkeypatch.setattr(handler.asyncio, "sleep", schlafen)
    asyncio.run(handler._cleanup_sent_item("a@firma.de", "<m@x>", "<p>sig</p>"))
    return geloescht, gepatcht


def test_kopie_erscheint_verspaetet(monkeypatch):
    geloescht, gepatcht = _lauf(monkeypatch, [[ORIG], [ORIG, KOPIE]])
    assert geloescht == ["orig"]                    # Original weg, Kopie bleibt
    assert gepatcht == ["orig"]                     # im ersten Durchgang nachgebessert


def test_ohne_kopie_bleibt_das_nachgebesserte_original(monkeypatch):
    geloescht, gepatcht = _lauf(monkeypatch, [[ORIG]] * len(handler._AUFRAEUM_TAKT))
    assert geloescht == []
    assert gepatcht == ["orig"] * len(handler._AUFRAEUM_TAKT)


def test_beide_sofort_da(monkeypatch):
    geloescht, gepatcht = _lauf(monkeypatch, [[KOPIE, ORIG]])
    assert geloescht == ["orig"] and gepatcht == []
