"""Graph++: Post aus dem Tenant über Graph, Post von außen über IMAP APPEND.

ANLASS (2026-10-11, Outlook-Testlauf T20): Die Abwesenheit war korrekt
eingeschaltet, eine interne Testmail kam an — aber keine Abwesenheitsantwort.
Der Rückweg legte im Modus Graph++ JEDE Post an einen Tenant-Empfänger per
IMAP APPEND in den Ordner, auch rein interne. IMAP APPEND ist kein
Zustellweg: Abwesenheit und Posteingangsregeln laufen dabei nicht.

Jetzt entscheidet der Absender: im Tenant → Graph sendMail (Exchange-
Zustellweg); außerhalb → IMAP APPEND (Graph kann nicht als fremde Adresse
senden). sendMail mit MIME legt immer eine Kopie in „Gesendete Elemente" ab;
der Handler räumt das Duplikat auf (nur_duplikate).
"""
import asyncio

import pytest

import exo_mailboxes
import graph_client
import graph_reinject
import reinject
import settings_store
import smtp_submit

RAW = (b"From: a@t.de\r\nTo: b@t.de\r\nSubject: x\r\nMessage-ID: <m1@t.de>\r\n"
       b"Content-Type: text/plain\r\n\r\nhi\r\n")


@pytest.fixture
def wege(monkeypatch):
    aufrufe = {"graph": [], "imap": [], "fail": []}
    ergebnis = {"graph": True, "imap": True}
    monkeypatch.setattr(settings_store, "reinject_mode", lambda: "graph_plus")
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: d)
    monkeypatch.setattr(exo_mailboxes, "known_addresses", lambda: {"a@t.de", "b@t.de"})

    def graph(frm, rcpts, data):
        aufrufe["graph"].append((frm, list(rcpts)))
        return ergebnis["graph"]

    def imap(rcpt, data):
        aufrufe["imap"].append(rcpt)
        return ergebnis["imap"] and rcpt.endswith("@t.de")

    monkeypatch.setattr(graph_reinject, "send_via_graph_mime", graph)
    monkeypatch.setattr(graph_reinject, "send_via_graph", graph)
    monkeypatch.setattr(smtp_submit, "deliver_inbound_imap", imap)
    monkeypatch.setattr(reinject, "_fail_delivery",
                        lambda frm, rcpts, data, why, q: aufrufe["fail"].append(list(rcpts)))
    return aufrufe, ergebnis


def test_interne_post_geht_ueber_graph_nicht_ueber_imap(wege):
    aufrufe, _ = wege
    weg = reinject.send("a@t.de", ["b@t.de"], RAW)
    assert aufrufe["graph"] == [("a@t.de", ["b@t.de"])]
    assert aufrufe["imap"] == []
    assert weg == reinject.WEG_GRAPH_MIT_KOPIE


def test_post_von_aussen_geht_ueber_imap(wege):
    aufrufe, _ = wege
    weg = reinject.send("kunde@extern.de", ["b@t.de"], RAW)
    assert aufrufe["imap"] == ["b@t.de"]
    assert aufrufe["graph"] == []
    assert weg is None


def test_scheitert_graph_faellt_interne_post_auf_imap_zurueck(wege):
    aufrufe, ergebnis = wege
    ergebnis["graph"] = False
    weg = reinject.send("a@t.de", ["b@t.de"], RAW)
    assert aufrufe["imap"] == ["b@t.de"] and aufrufe["fail"] == []
    assert weg is None                      # keine sendMail-Kopie → nichts aufräumen


def test_scheitern_beide_wege_wird_gemeldet(wege):
    aufrufe, ergebnis = wege
    ergebnis["graph"] = ergebnis["imap"] = False
    reinject.send("a@t.de", ["b@t.de"], RAW)
    assert aufrufe["fail"] == [["b@t.de"]]


# ── Aufräumen „Gesendete Elemente" ───────────────────────────────────────────

class _Resp:
    def __init__(self, data=None, status=200):
        self._d, self.status_code = data or {}, status
        self.is_success = status < 300
        self.text = ""

    def json(self):
        return self._d

    def raise_for_status(self):
        pass


def _client(items, geloescht, gepatcht):
    class C:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return _Resp({"value": items})

        async def delete(self, url, headers=None):
            geloescht.append(url.rsplit("/", 1)[-1])
            return _Resp(status=204)

        async def patch(self, url, headers=None, json=None):
            gepatcht.append(url)
            return _Resp()
    return C


@pytest.fixture
def graph_stub(monkeypatch):
    async def tok():
        return "T"
    monkeypatch.setattr(graph_client, "_acquire_token_async", tok)


def _lauf(monkeypatch, items):
    geloescht, gepatcht = [], []
    monkeypatch.setattr(graph_client.httpx, "AsyncClient", _client(items, geloescht, gepatcht))
    r = asyncio.run(graph_client.cleanup_sent_items("a@t.de", "<m1@t.de>", "",
                                                    nur_duplikate=True))
    return r, geloescht, gepatcht


def test_nur_duplikate_wartet_bei_einem_element(monkeypatch, graph_stub):
    r, geloescht, gepatcht = _lauf(monkeypatch, [{"id": "orig", "createdDateTime": "1"}])
    assert r is False and geloescht == [] and gepatcht == []


def test_nur_duplikate_behaelt_die_sendmail_kopie(monkeypatch, graph_stub):
    r, geloescht, gepatcht = _lauf(monkeypatch, [
        {"id": "kopie", "createdDateTime": "2026-10-11T10:00:05Z"},
        {"id": "orig", "createdDateTime": "2026-10-11T10:00:00Z"},
    ])
    assert r is True and geloescht == ["orig"] and gepatcht == []


def test_handler_raeumt_nach_graph_weg_auf():
    """Der interne Weg ohne Signatur endet vor dem Aufräumen des Hauptwegs —
    er muss das Duplikat selbst entfernen, und nur nach dem Graph-Weg."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "app/handler.py").read_text(encoding="utf-8")
    block = src.split('_audit("internal_only_skip")', 1)[1].split('return "250 OK"', 1)[0]
    assert "reinject.WEG_GRAPH_MIT_KOPIE" in block
    assert "nur_duplikate=True" in block
