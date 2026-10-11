"""Einzelvorgänge hinter den Routing-Zahlen („was war Sache").

ANLASS (2026-10-12): Die Tabelle „Durch das Gateway geroutet" zeigte z.B.
„21 Fehler" an einem Tag, ohne dass irgendwo stand, welche Mail, an wen und
warum. Die Tagessummen bleiben; daneben hält `routing_ereignis` jeden Vorgang
fest — Umschlagdaten, Ergebnis, Fehlertext und eine Kennung, an der
Wiederholungen DERSELBEN Mail erkennbar sind (die Warteschlange versucht nach
Exchange-Kadenz erneut, und jeder Versuch zählt in der Summe mit).
"""
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import relay_stats


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setattr(relay_stats, "DB_PATH", tmp_path / "relay_stats.db")
    return relay_stats


MAIL = b"From: a@zarenko.net\r\n\r\nkorpus"


def test_fehler_mit_text_absender_und_empfaengern(db):
    db.merke_routing("onprem", len(MAIL), ok=False, absender="A@Zarenko.net",
                     empfaenger=["x@contoso.de", "Y@contoso.de"], inhalt=MAIL,
                     fehler_text="SMTPRecipientsRefused: 550 5.1.1 unbekannt")
    e = db.routing_ereignisse()["ereignisse"]
    assert len(e) == 1
    assert e[0]["ok"] is False
    assert e[0]["fehler"] == "SMTPRecipientsRefused: 550 5.1.1 unbekannt"
    assert e[0]["absender"] == "a@zarenko.net"
    assert e[0]["empfaenger"] == "x@contoso.de, y@contoso.de"
    assert e[0]["bytes"] == len(MAIL) and e[0]["ziel"] == "onprem"


def test_erfolg_speichert_keinen_fehlertext(db):
    db.merke_routing("onprem", 10, ok=True, inhalt=MAIL, fehler_text="egal")
    assert db.routing_ereignisse()["ereignisse"][0]["fehler"] == ""


def test_wiederholung_derselben_mail_zaehlt_versuche(db):
    for _ in range(3):
        db.merke_routing("onprem", 10, ok=False, inhalt=MAIL, fehler_text="Timeout")
    db.merke_routing("onprem", 10, ok=True, inhalt=b"andere Mail")
    e = db.routing_ereignisse()["ereignisse"]
    kennung = {x["kennung"] for x in e if not x["ok"]}
    assert len(kennung) == 1 and len(next(iter(kennung))) == 12
    assert [x["versuch"] for x in e if not x["ok"]] == [3, 2, 1]     # neueste zuerst
    assert [x["versuch"] for x in e if x["ok"]] == [1]


def test_kennung_verraet_den_inhalt_nicht(db):
    db.merke_routing("onprem", 10, ok=True, inhalt=MAIL)
    zeile = db.routing_ereignisse()["ereignisse"][0]
    assert b"korpus".hex() not in zeile["kennung"] and "korpus" not in str(zeile)


def test_filter_nach_ziel_und_nur_fehler(db):
    db.merke_routing("onprem", 1, ok=True, inhalt=b"1")
    db.merke_routing("onprem", 1, ok=False, inhalt=b"2", fehler_text="x")
    db.merke_routing("anderes", 1, ok=False, inhalt=b"3", fehler_text="y")
    assert len(db.routing_ereignisse("onprem")["ereignisse"]) == 2
    nur = db.routing_ereignisse("onprem", nur_fehler=True)["ereignisse"]
    assert [x["fehler"] for x in nur] == ["x"]


def test_versuch_zaehlt_auch_bei_nur_fehler_ueber_alle(db):
    """Filtert man auf Fehler, darf die Versuchszählung nicht neu anfangen."""
    db.merke_routing("onprem", 1, ok=False, inhalt=MAIL, fehler_text="a")
    db.merke_routing("onprem", 1, ok=True, inhalt=MAIL)
    db.merke_routing("onprem", 1, ok=False, inhalt=MAIL, fehler_text="b")
    nur = db.routing_ereignisse(nur_fehler=True)["ereignisse"]
    assert [(x["fehler"], x["versuch"]) for x in nur] == [("b", 3), ("a", 1)]


def test_seit_nennt_den_aeltesten_vorgang(db):
    assert db.routing_ereignisse()["seit"] == ""
    db.merke_routing("onprem", 1, ok=True, inhalt=b"1")
    assert db.routing_ereignisse()["seit"].startswith("20")


def test_aufraeumen_loescht_alte_einzelvorgaenge_aber_nicht_summen(db, monkeypatch):
    db.merke_routing("onprem", 1, ok=False, inhalt=b"1", fehler_text="alt")
    echt = db._jetzt
    monkeypatch.setattr(db, "_jetzt", lambda: echt() + timedelta(days=db.EREIGNIS_TAGE + 1))
    db.aufraeumen()
    monkeypatch.setattr(db, "_jetzt", echt)
    assert db.routing_ereignisse()["ereignisse"] == []
    assert db.routing_statistik(400)["gesamt_fehler"] == 1


def test_reinject_reicht_fehlertext_und_umschlag_durch(monkeypatch, tmp_path):
    """Der echte Routing-Zweig in reinject.send() muss Text und Umschlag liefern —
    sonst steht in der Liste nur „Fehler"."""
    import reinject
    import settings_store
    import exo_mailboxes
    import sende_identitaeten
    monkeypatch.setattr(relay_stats, "DB_PATH", tmp_path / "relay_stats.db")
    monkeypatch.setattr(exo_mailboxes, "known_addresses", set)
    monkeypatch.setattr(sende_identitaeten, "ist_identitaets_adresse", lambda a: False)
    monkeypatch.setattr(settings_store, "reinject_mode", lambda: "smtp")
    werte = {"RELAY_TARGETS": {"onprem": {"host": "ex.local"}},
             "DOMAIN_ROUTES": {"contoso.de": "onprem"}, "IDENT_DELIVER_VIA_GRAPH": False}
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))

    def _scheitert(z, mf, rt, cb):
        raise ConnectionRefusedError("[Errno 111] Connection refused")
    monkeypatch.setattr(reinject, "_relay_to_target", _scheitert)
    monkeypatch.setattr(reinject, "_send_smtp", lambda *a: None)
    monkeypatch.setattr(reinject, "_fail_delivery", lambda *a, **k: None)

    reinject.send("erika@zarenko.net", ["a@contoso.de"], MAIL)

    e = relay_stats.routing_ereignisse()["ereignisse"]
    assert len(e) == 1
    assert e[0]["fehler"] == "ConnectionRefusedError: [Errno 111] Connection refused"
    assert e[0]["absender"] == "erika@zarenko.net" and e[0]["empfaenger"] == "a@contoso.de"
    assert e[0]["kennung"]


@pytest.fixture
def client():
    from starlette.testclient import TestClient
    from webui import app as wa
    with TestClient(wa.app) as c:
        yield c


def test_schnittstelle_verlangt_admin(client):
    r = client.get("/api/relay/routing-ereignisse", follow_redirects=False)
    assert r.status_code in (401, 403, 303, 307)
