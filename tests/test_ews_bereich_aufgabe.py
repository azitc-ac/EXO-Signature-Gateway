"""Verweigert Exchange lokal dem EWS-Dienstkonto Postfächer, entsteht eine Aufgabe.

Bis v1.12.84 stand ErrorImpersonateUserDenied nur in der Prüfung des einzelnen
Postfachs. Das Gateway kann den Management Scope nicht selbst erweitern; die
Aufgabe nennt den Weg und die betroffenen Postfächer.
"""
import asyncio

import pytest

import abnahme
import aufgaben
import ews_onprem
import health_check
import settings_store


@pytest.fixture
def werte(monkeypatch):
    w = {"EWS_ONPREM_URL": "https://exchange.firma.at/EWS/Exchange.asmx",
         "ABWESENHEIT_AKTIV": True, "MAILBOX_HEALTH": {}}
    alt = settings_store.get
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: w[k] if k in w else alt(k, *a, **kw))
    monkeypatch.setattr(aufgaben, "_eingerichtet", lambda: True)
    monkeypatch.setattr(health_check, "_alt_check", lambda e, n: None)
    aufgaben.vergessen()
    return w


def _pruefe(monkeypatch, code):
    monkeypatch.setattr(ews_onprem, "verbindung_testen",
                        lambda e: {"ok": code is None, "code": code or "", "detail": "x"})
    return asyncio.run(health_check._check_ews_zugriff("max@firma.at"))


def _offen():
    aufgaben.vergessen()
    return any(a["kennung"] == "ews-bereich" for a in aufgaben.offene())


def test_ablehnung_wird_aufgabe(werte, monkeypatch):
    r = _pruefe(monkeypatch, "ErrorImpersonateUserDenied")
    assert r["ews_code"] == "ErrorImpersonateUserDenied"
    werte["MAILBOX_HEALTH"] = {"max@firma.at": {"checks": {"abwesenheit": r}}}
    p = abnahme._ews_bereich()
    assert p["zustand"] == abnahme.OFFEN and "max@firma.at" in p["befund"]
    assert "Scope" in p["zu_tun"]
    assert _offen()


def test_andere_fehler_keine_aufgabe(werte, monkeypatch):
    r = _pruefe(monkeypatch, "ErrorMailboxMoveInProgress")
    assert "ews_code" not in r
    werte["MAILBOX_HEALTH"] = {"max@firma.at": {"checks": {"abwesenheit": r}}}
    assert abnahme._ews_bereich()["zustand"] == abnahme.OK
    assert not _offen()


def test_ohne_ews_entfaellt(werte):
    werte["EWS_ONPREM_URL"] = ""
    assert abnahme._ews_bereich()["zustand"] == abnahme.ENTFAELLT


def test_fehlende_rolle_nennt_rolle(werte, monkeypatch):
    r = _pruefe(monkeypatch, "ErrorImpersonationDenied")
    werte["MAILBOX_HEALTH"] = {"max@firma.at": {"checks": {"abwesenheit": r}}}
    assert "ApplicationImpersonation" in abnahme._ews_bereich()["zu_tun"]
