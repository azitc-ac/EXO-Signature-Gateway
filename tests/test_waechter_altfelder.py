"""Herzschlag-Altfelder in watchdog_state.json (waechter_state.HERZSCHLAG_ALT).

Seit dem Mehrwächter-Register schreibt sie niemand mehr. Bis v1.12.192 wurden
sie trotzdem als Rückfall gelesen: Ein alter Fehlertext oder ein alter „Bypass
aktiv“ hätte für immer im Band gestanden.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import waechter_register as reg
import waechter_state


def _jetzt() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dateien(monkeypatch, tmp_path, alt: dict, register: dict):
    monkeypatch.setattr(waechter_state, "PFAD", tmp_path / "watchdog_state.json")
    monkeypatch.setattr(reg, "PFAD", tmp_path / "watchdog_watchers.json")
    waechter_state.PFAD.write_text(json.dumps(alt))
    reg.PFAD.write_text(json.dumps(register))


ALT = {"last_seen": "2026-09-08T13:05:01Z", "bypass_active": True, "healthy": False,
       "fails": 3, "oks": 0, "exo_error": "ALTER FEHLER",
       "rule_state": "Enabled", "rule_checked": "2026-11-28T00:00:00Z"}


def _register():
    return {"wd_1": {"id": "wd_1", "name": "W", "kind": "azure", "token_hash": "x",
                     "last_seen": _jetzt(), "healthy": True, "bypass_active": False,
                     "fails": 0, "oks": 9, "exo_error": ""}}


def test_band_liest_nur_das_register(monkeypatch, tmp_path):
    from webui import deps
    _dateien(monkeypatch, tmp_path, ALT, _register())
    monkeypatch.setattr(deps.settings_store, "get",
                        lambda k, d=None: True if k == "WATCHDOG_ENABLED" else d)
    assert deps._waechter_gemeldet() is None     # alter Fehler zählt nicht
    assert deps._bypass_gemeldet() is None       # alter „Bypass aktiv“ zählt nicht


def test_start_entfernt_altfelder_und_behaelt_regelzustand(monkeypatch, tmp_path):
    _dateien(monkeypatch, tmp_path, ALT, _register())
    assert reg.migrieren_falls_noetig() is False         # Register war schon da
    rest = json.loads(waechter_state.PFAD.read_text())
    assert not any(k in rest for k in waechter_state.HERZSCHLAG_ALT)
    assert rest["rule_state"] == "Enabled" and rest["rule_checked"]
    assert waechter_state.altfelder_entfernen() is False  # nichts mehr zu tun


def test_uebernahme_liest_altfelder_vor_dem_entfernen(monkeypatch, tmp_path):
    _dateien(monkeypatch, tmp_path, ALT, {})
    reg.PFAD.unlink()
    monkeypatch.setattr("settings_store.get",
                        lambda k, d=None: {"WATCHDOG_TOKEN_HASH": "h", "WATCHDOG_KIND": "azure"}.get(k, d))
    assert reg.migrieren_falls_noetig() is True
    w = reg.liste()[0]
    assert w["last_seen"] == ALT["last_seen"] and w["exo_error"] == "ALTER FEHLER"
    assert "last_seen" not in json.loads(waechter_state.PFAD.read_text())


def test_statusabfrage_liest_nur_das_register(monkeypatch, tmp_path):
    import asyncio
    from webui.routen import waechter as route
    _dateien(monkeypatch, tmp_path, ALT, _register())
    monkeypatch.setattr(route, "_config_hinweise", lambda: [])
    monkeypatch.setattr(route, "_code_stand_sicher", lambda: "")
    r = json.loads(asyncio.run(route.watchdog_status("admin")).body)
    assert r["bypass_active"] is False and r["exo_error"] == ""
