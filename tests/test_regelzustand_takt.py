"""Unabhängige Prüfung des Signatur-Regelzustands (scheduler._regelzustand_pruefen).

Sie erkennt einen hängenden Bypass auch bei totem Wächter. Bis v1.12.192 lief
sie nur im Tageslauf, und jedes Speichern der Postfachliste verwarf ihr
Ergebnis (waechter_regel.regel_umgeschaltet) — bis zu 24 h ohne Absicherung.
"""
from __future__ import annotations

import json

import scheduler
import waechter_state


def _zustand(monkeypatch, tmp_path, d: dict):
    monkeypatch.setattr(waechter_state, "PFAD", tmp_path / "watchdog_state.json")
    waechter_state.PFAD.write_text(json.dumps(d))


def test_stuendlich(monkeypatch, tmp_path):
    _zustand(monkeypatch, tmp_path, {"rule_checked": "2026-11-28T00:00:00Z"})
    assert scheduler._regelzustand_faellig(59 * 60) is False
    assert scheduler._regelzustand_faellig(61 * 60) is True


def test_nach_zuruecksetzen_bald(monkeypatch, tmp_path):
    _zustand(monkeypatch, tmp_path, {"rule_state": "", "rule_checked": ""})
    assert scheduler._regelzustand_faellig(5 * 60) is False      # nicht im selben Atemzug
    assert scheduler._regelzustand_faellig(11 * 60) is True


def test_nicht_mehr_im_tageslauf():
    import inspect
    assert "pruefe_und_merke" not in inspect.getsource(scheduler._run_daily)
    assert "_regelzustand_faellig(" in inspect.getsource(scheduler._loop)


def test_nur_mit_waechter(monkeypatch):
    import waechter_regel
    aufrufe = []
    monkeypatch.setattr(waechter_regel, "pruefe_und_merke", lambda: aufrufe.append(1))
    monkeypatch.setattr(scheduler.settings_store, "get", lambda k, d=None: False)
    scheduler._regelzustand_pruefen()
    assert aufrufe == []
    monkeypatch.setattr(scheduler.settings_store, "get",
                        lambda k, d=None: True if k == "WATCHDOG_ENABLED" else d)
    scheduler._regelzustand_pruefen()
    assert aufrufe == [1]
