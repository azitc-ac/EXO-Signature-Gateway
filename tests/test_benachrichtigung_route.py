"""Gateway-eigene Post folgt der Routing-Einstellung für externe Empfänger.

Steht der Rückweg auf smtp und führt die Route einen Empfänger an Exchange
vorbei (Direktversand), darf die Benachrichtigung nicht über Graph gehen:
Graph gibt sie in den Exchange-Transport, die Markierung lässt sie an der
Transportregel vorbei, und Exchange stellt sie über Microsofts Ausgang zu —
den Weg, den die Route umgehen soll (DEV-Tenant: 550 5.7.708).
"""
import base64
from email import message_from_bytes, policy

import pytest

import domain_routing
import loop_detector
import notification
import settings_store


class _KeinGraph:
    def __init__(self, *a, **kw):
        raise AssertionError("Graph darf hier nicht benutzt werden")


@pytest.fixture
def umgebung(monkeypatch):
    gesendet = []
    import reinject
    monkeypatch.setattr(reinject, "send",
                        lambda von, an, roh, **kw: gesendet.append((von, an, roh)))
    monkeypatch.setattr(loop_detector, "marker_wert", lambda: "1-abcdef012345")
    monkeypatch.setattr(loop_detector, "header_name", lambda: "X-Sig-Applied")
    monkeypatch.setattr(notification.graph_client, "_acquire_token", lambda: "t")
    return gesendet


def _route(monkeypatch, modus, ziel):
    monkeypatch.setattr(settings_store, "reinject_mode", lambda: modus)
    monkeypatch.setattr(domain_routing, "route_fuer_empfaenger", lambda a: ziel)


def test_umgeleiteter_empfaenger_geht_ueber_reinject(monkeypatch, umgebung):
    _route(monkeypatch, "smtp", "direkt")
    monkeypatch.setattr(notification.httpx, "Client", _KeinGraph)
    anhang = {"name": "a.txt", "type": "text/plain",
              "data": base64.b64encode(b"Inhalt").decode()}
    assert notification._graph_send("p@partner.example", "Betreff ä", "<p>Hallo</p>",
                                    reply_to="r@firma.example",
                                    sender="alexw@firma.example", attachments=[anhang])
    (von, an, roh), = umgebung
    assert von == "alexw@firma.example" and an == ["p@partner.example"]
    assert b"\r\n" in roh and b"\n" not in roh.replace(b"\r\n", b"")
    m = message_from_bytes(roh, policy=policy.default)
    assert m["X-Sig-Applied"] == "1-abcdef012345"
    assert m["Reply-To"] == "r@firma.example" and m["Subject"] == "Betreff ä"
    assert m["Message-ID"] and m["Date"]
    assert "<p>Hallo</p>" in m.get_body(("html",)).get_content()
    teile = list(m.iter_attachments())
    assert [t.get_filename() for t in teile] == ["a.txt"]
    assert teile[0].get_content() == "Inhalt"


@pytest.mark.parametrize("modus,ziel", [("smtp", "exo"), ("graph", "direkt"),
                                        ("graph_plus", "direkt")])
def test_sonst_bleibt_es_bei_graph(monkeypatch, umgebung, modus, ziel):
    _route(monkeypatch, modus, domain_routing.EXO if ziel == "exo" else ziel)
    gepostet = []

    class _Client:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None, headers=None):
            gepostet.append(url)

            class _A:
                status_code = 202
                text = ""
            return _A()

    monkeypatch.setattr(notification.httpx, "Client", _Client)
    assert notification._graph_send("p@partner.example", "B", "<p>x</p>",
                                    sender="alexw@firma.example")
    assert gepostet and not umgebung
