"""Kalender-Automatik nimmt eine von ihr gesetzte Abwesenheit zurück (v1.11.33).

Gemeldet am 11.11.2026 (VM): Abwesend-Termin angelegt → „Kalender jetzt lesen“
setzte die Abwesenheit. Termin gelöscht → die Abwesenheit blieb. Die
Automatik kannte nur das Einschalten (Status aus + Termin → geplant).
"""
import asyncio
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import abwesenheit  # noqa: E402

UPN = "erika@firma.at"
A = {"dateTime": "2026-11-20T00:00:00.0000000", "timeZone": "UTC"}
B = {"dateTime": "2026-11-21T00:00:00.0000000", "timeZone": "UTC"}
C = {"dateTime": "2026-11-25T08:00:00.0000000", "timeZone": "UTC"}
D = {"dateTime": "2026-11-26T17:00:00.0000000", "timeZone": "UTC"}
AUTO_AB = abwesenheit._fenster_schluessel(A, B)


@pytest.fixture
def lauf(monkeypatch):
    zustand = {"setting": None, "events": [], "patches": [], "fehler": None}
    werte = {"OOO_CALENDAR_REFRESH_HOURS": 6}
    monkeypatch.setattr(abwesenheit.settings_store, "get", lambda k, d=None: werte.get(k, d))
    monkeypatch.setattr(abwesenheit, "oof_vorlage_fuer", lambda *a: "oof")
    monkeypatch.setattr(abwesenheit, "_kalender_auto_an", lambda cfg: True)
    monkeypatch.setattr(abwesenheit, "kalender_lesen", lambda: True)

    async def get_setting(upn, token):
        return "ok", dict(zustand["setting"])
    monkeypatch.setattr(abwesenheit, "_get_setting", get_setting)

    async def get_user(upn):
        return abwesenheit.graph_client.UserData(mail=upn)
    monkeypatch.setattr(abwesenheit.graph_client, "get_user", get_user)

    async def rendern(*a, **k):
        return "TEXT", "TEXT"
    monkeypatch.setattr(abwesenheit, "render_intern_extern", rendern)

    async def kalender(upn, token):
        if zustand["fehler"]:
            raise zustand["fehler"]
        return list(zustand["events"])
    monkeypatch.setattr(abwesenheit, "_kalender_oof_events", kalender)

    async def patch(upn, token, setting, html, html_extern=None, status=None, start=None, ende=None):
        zustand["patches"].append({"status": status, "start": start, "ende": ende})
        return {"internalReplyMessage": html, "externalReplyMessage": html_extern}
    monkeypatch.setattr(abwesenheit, "_patch_setting", patch)

    def ausfuehren(state):
        return asyncio.run(abwesenheit.setze_fuer_postfach(UPN, UPN, {}, {}, "tok", state))
    return zustand, ausfuehren


def _geplant(start=A, ende=B):
    return {"status": "scheduled", "scheduledStartDateTime": start, "scheduledEndDateTime": ende,
            "internalReplyMessage": "TEXT", "externalReplyMessage": "TEXT"}


def _state(auto_win=AUTO_AB):
    return {UPN: {"auto_win": auto_win, "render": "TEXT", "intern": "TEXT", "extern": "TEXT"}}


def test_termin_geloescht_abwesenheit_aus(lauf):
    zustand, ausfuehren = lauf
    zustand["setting"], zustand["events"] = _geplant(), []
    state = _state()
    assert ausfuehren(state) == abwesenheit.GESETZT
    assert zustand["patches"][-1]["status"] == "disabled"
    assert "auto_win" not in state[UPN]


def test_termin_noch_da_nichts_tun(lauf):
    zustand, ausfuehren = lauf
    zustand["setting"], zustand["events"] = _geplant(), [{"start": A, "end": B}]
    assert ausfuehren(_state()) == abwesenheit.UNVERAENDERT
    assert zustand["patches"] == []


def test_termin_verschoben_folgt_dem_neuen(lauf):
    zustand, ausfuehren = lauf
    zustand["setting"], zustand["events"] = _geplant(), [{"start": C, "end": D}]
    state = _state()
    ausfuehren(state)
    p = zustand["patches"][-1]
    assert (p["status"], p["start"], p["ende"]) == ("scheduled", C, D)
    assert state[UPN]["auto_win"] == abwesenheit._fenster_schluessel(C, D)


def test_von_hand_geaendert_bleibt(lauf):
    zustand, ausfuehren = lauf
    zustand["setting"], zustand["events"] = _geplant(A, D), []      # Ende von Hand verlegt
    assert ausfuehren(_state()) == abwesenheit.UNVERAENDERT
    assert zustand["patches"] == []


def test_kalender_403_nimmt_nichts_zurueck(lauf):
    zustand, ausfuehren = lauf
    zustand["setting"], zustand["fehler"] = _geplant(), PermissionError("403")
    state = _state()
    assert ausfuehren(state) == abwesenheit.UNVERAENDERT
    assert zustand["patches"] == []
    assert state[UPN]["kal"]["fehler"] == "403", "403 zwischengespeichert, aber markiert"


def test_einschalten_wie_bisher(lauf):
    zustand, ausfuehren = lauf
    zustand["setting"] = {"status": "disabled", "internalReplyMessage": "TEXT",
                          "externalReplyMessage": "TEXT"}
    zustand["events"] = [{"start": A, "end": B}]
    state = _state(auto_win=None)
    ausfuehren(state)
    assert zustand["patches"][-1]["status"] == "scheduled"
    assert state[UPN]["auto_win"] == AUTO_AB


def test_exchange_nachkommastellen_gelten_als_gleich():
    s = _geplant({"dateTime": "2026-11-20T00:00:00", "timeZone": "UTC"},
                 {"dateTime": "2026-11-21T00:00:00.0000000", "timeZone": "UTC"})
    assert abwesenheit._von_uns_geplant(s, AUTO_AB)
    s["scheduledStartDateTime"]["timeZone"] = "W. Europe Standard Time"
    assert not abwesenheit._von_uns_geplant(s, AUTO_AB)
