"""Aktivierungszeitpunkt je Postfach und die Marke „wirkt in bis zu 3 h“.

Die Transportregel (FromMemberOf) erfasst ein neues Mitglied der
Verteilerliste erst nach bis zu ~3 Stunden (gemessen 17.11.2026, zwei
Tenants), obwohl die Mitgliedsliste schon nach Minuten stimmt. Damit ein frisch
aktiviertes Postfach in dieser Zeit nicht „kaputt“ wirkt, merkt sich das
Speichern den Übergang „nicht aktiv → aktiv“ (`aktiviert_seit`) und die
Postfachliste zeigt `regel_wartet`.

Invarianten:
  * nur der ÜBERGANG setzt den Zeitpunkt — erneutes Speichern behält ihn,
  * aus → an setzt ihn neu,
  * Bestand ohne Feld gilt als lange aktiv (keine Marke, auch nicht nach
    erneutem Speichern),
  * Umschlüsseln E-Mail → ExchangeGuid ist kein Übergang.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import exo_mailboxes
from webui.routen import mailboxes as mbroute

ADR = "max.mustermann@firma.at"
GUID = "11111111-2222-3333-4444-555555555555"
ALT = "2026-01-01T08:00:00+00:00"


def _speichern(monkeypatch, vorher: dict, zeile: dict, exo: list | None = None) -> dict:
    gespeichert: dict = {}

    async def fake_auto(neu):
        return {}
    store = {"MAILBOX_CONFIG": vorher, "USER_BOOKINGS": {}}
    monkeypatch.setattr(mbroute.settings_store, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(mbroute.settings_store, "get_all", lambda: {})
    monkeypatch.setattr(mbroute.settings_store, "update", lambda patch: gespeichert.update(patch))
    monkeypatch.setattr(exo_mailboxes, "mit_hybrid", lambda *_a: list(exo or []))
    monkeypatch.setattr(exo_mailboxes, "hybrid_aktiv", lambda: False)
    monkeypatch.setattr(mbroute, "_auto_enrollment_anstossen", fake_auto)
    r = asyncio.run(mbroute.api_save_mailboxes({"mailboxes": [zeile], "update_dg": False}))
    assert r["ok"] is True
    return gespeichert["MAILBOX_CONFIG"]


def _frisch(wert: str) -> bool:
    t = datetime.fromisoformat(wert)
    return abs(datetime.now(timezone.utc) - t) < timedelta(minutes=1)


def test_uebergang_setzt_zeitpunkt(monkeypatch):
    cfg = _speichern(monkeypatch, {}, {"email": ADR, "sig": True})
    assert _frisch(cfg[ADR]["aktiviert_seit"])
    assert mbroute.regel_wartet(cfg[ADR]) is True


def test_nur_smime_ist_auch_ein_uebergang(monkeypatch):
    cfg = _speichern(monkeypatch, {}, {"email": ADR, "sig": False, "smime": True})
    assert _frisch(cfg[ADR]["aktiviert_seit"])


def test_erneutes_speichern_behaelt_zeitpunkt(monkeypatch):
    vorher = {ADR: {"sig": True, "smime": False, "aktiviert_seit": ALT}}
    cfg = _speichern(monkeypatch, vorher, {"email": ADR, "sig": True, "smime": True})
    assert cfg[ADR]["aktiviert_seit"] == ALT


def test_aus_dann_an_setzt_neu(monkeypatch):
    # Eintrag mit beidem aus (z. B. vom Abwesenheits-Dashboard angelegt) = nicht aktiv
    vorher = {ADR: {"sig": False, "smime": False, "aktiviert_seit": ALT}}
    cfg = _speichern(monkeypatch, vorher, {"email": ADR, "sig": True})
    assert cfg[ADR]["aktiviert_seit"] != ALT and _frisch(cfg[ADR]["aktiviert_seit"])


def test_bestand_ohne_feld_bleibt_ohne_marke(monkeypatch):
    vorher = {ADR: {"sig": True, "smime": False}}
    cfg = _speichern(monkeypatch, vorher, {"email": ADR, "sig": True})
    assert "aktiviert_seit" not in cfg[ADR]
    assert mbroute.regel_wartet(cfg[ADR]) is False


def test_umschluesseln_auf_guid_ist_kein_uebergang(monkeypatch):
    vorher = {ADR: {"sig": True, "smime": False, "aktiviert_seit": ALT}}
    exo = [{"guid": GUID, "primary": ADR, "addresses": [ADR], "display_name": "Max",
            "type": "UserMailbox"}]
    cfg = _speichern(monkeypatch, vorher, {"email": ADR, "sig": True}, exo)
    assert cfg[GUID]["aktiviert_seit"] == ALT


def test_regel_wartet_grenzen():
    jetzt = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    def e(delta):
        return {"sig": True, "aktiviert_seit": (jetzt - delta).isoformat()}
    assert mbroute.regel_wartet(e(timedelta(minutes=5)), jetzt) is True
    assert mbroute.regel_wartet(e(timedelta(hours=2, minutes=59)), jetzt) is True
    assert mbroute.regel_wartet(e(timedelta(hours=3)), jetzt) is False
    assert mbroute.regel_wartet({"sig": False, "aktiviert_seit": jetzt.isoformat()}, jetzt) is False
    assert mbroute.regel_wartet({"sig": True, "aktiviert_seit": "kaputt"}, jetzt) is False
    assert mbroute.regel_wartet({"sig": True}, jetzt) is False


@pytest.fixture
def api(monkeypatch):
    store: dict = {"USER_BOOKINGS": {}, "MAILBOX_HEALTH": {}}
    monkeypatch.setattr(mbroute.settings_store, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(exo_mailboxes, "hybrid_eingeschaltet", lambda: False)
    monkeypatch.setattr(exo_mailboxes, "hybrid_aktiv", lambda: False)
    monkeypatch.setattr(mbroute._pol, "standards", lambda *_a: {})
    return store


def test_api_liefert_zustand(api, monkeypatch):
    frisch = datetime.now(timezone.utc).isoformat(timespec="seconds")
    neu, alt, bestand = "neu@firma.at", "alt@firma.at", "bestand@firma.at"
    api["MAILBOX_CONFIG"] = {
        neu: {"sig": True, "aktiviert_seit": frisch},
        alt: {"sig": True, "aktiviert_seit": ALT},
        bestand: {"sig": True},
    }
    # neu und alt kennt EXO, bestand nicht (zweite Schleife der API)
    monkeypatch.setattr(exo_mailboxes, "mit_hybrid", lambda *_a: [
        {"primary": neu, "display_name": "Neu", "type": "UserMailbox"},
        {"primary": alt, "display_name": "Alt", "type": "UserMailbox"}])
    r = asyncio.run(mbroute.api_get_mailboxes(refresh=False))
    z = {m["email"]: m for m in r["mailboxes"]}
    assert z[neu]["regel_wartet"] is True and z[neu]["aktiviert_seit"] == frisch
    assert z[alt]["regel_wartet"] is False
    assert z[bestand]["regel_wartet"] is False and z[bestand]["aktiviert_seit"] is None

    api["MAILBOX_CONFIG"] = {bestand: {"sig": True, "aktiviert_seit": frisch}}
    r = asyncio.run(mbroute.api_get_mailboxes(refresh=False))
    z = {m["email"]: m for m in r["mailboxes"]}
    assert z[bestand]["regel_wartet"] is True, "auch Einträge, die EXO nicht liefert"


def test_oberflaeche_zeigt_marke_aus_dem_serverzustand():
    html = (Path(__file__).parent.parent / "app/webui/templates/mailboxes.html").read_text(
        encoding="utf-8")
    assert "if (mb.regel_wartet)" in html
    assert 'class="mb-marke" data-marke="wartet"' in html
    assert "5–15 Min" not in html, "veraltete Zeitangabe für Mitglieder der Verteilerliste"
    # Jeder Zustandswert, den das JS setzt, braucht eine helle UND eine dunkle Regel
    import re
    werte = set(re.findall(r'data-marke="(\w+)"', html.split("<script", 1)[1]))
    assert werte == {"wartet"}
    for w in werte:
        assert f'.mb-marke[data-marke="{w}"] {{' in html
        assert f'[data-theme="dark"] .mb-marke[data-marke="{w}"]' in html


def test_hybrid_ohne_wirksame_option_keine_marke(api, monkeypatch):
    """Hybrid-Eintrag, Option aus: nicht in der Verteilerliste — keine Wartemarke."""
    frisch = datetime.now(timezone.utc).isoformat(timespec="seconds")
    api["MAILBOX_CONFIG"] = {GUID: {"sig": True, "hybrid": True, "primary": ADR,
                                    "known_addresses": [ADR], "aktiviert_seit": frisch}}
    monkeypatch.setattr(exo_mailboxes, "mit_hybrid", lambda *_a: [])
    r = asyncio.run(mbroute.api_get_mailboxes(refresh=False))
    (z,) = r["mailboxes"]
    assert z["type"] == "hybrid" and z["regel_wartet"] is False
