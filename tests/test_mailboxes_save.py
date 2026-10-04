"""Postfächer-Speichern baut den Eintrag neu auf und ersetzt MAILBOX_CONFIG ganz.

Die Invariante: Felder, die NICHT aus der Postfächer-Tabelle stammen, sondern
anderswo gesetzt werden (Abwesenheits-Dashboard, Self-Service) — Kalender-Automatik,
Ankündigung, Vorlagenfreigabe — müssen ein Speichern der Tabelle ÜBERLEBEN. Vor dem
Carryover-Fix wurden sie bei jedem Postfach-Speichern stillschweigend gelöscht.
"""
from __future__ import annotations

import asyncio

import config
import settings_store
from webui.routen import mailboxes as mbroute


def _run(coro):
    return asyncio.run(coro)


def test_postfach_speichern_bewahrt_abwesenheits_felder(monkeypatch):
    vorher = {"a@x.de": {
        "sig": True, "smime": False, "use_policy": True,
        "ooo_calendar": True,
        "oof_announce": True, "oof_announce_mode": "tage", "oof_announce_x": 7,
        "oof_announce_privat": False, "oof_announce_extern": True,
        "self_templates": True,
    }}
    gespeichert: dict = {}

    def fake_list(_arg):
        return []       # EXO nicht erreichbar → E-Mail-Key-Fallback (Key bleibt a@x.de)

    async def fake_auto(neu):
        return {}

    store = {"MAILBOX_CONFIG": vorher, "USER_BOOKINGS": {}}
    monkeypatch.setattr(mbroute.settings_store, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(mbroute.settings_store, "get_all", lambda: {})
    monkeypatch.setattr(mbroute.settings_store, "update", lambda patch: gespeichert.update(patch))
    import exo_mailboxes
    monkeypatch.setattr(exo_mailboxes, "list_mailboxes", fake_list)
    monkeypatch.setattr(mbroute, "_auto_enrollment_anstossen", fake_auto)

    # Die Tabelle schickt NUR ihre eigenen Felder (keine Abwesenheits-Felder).
    body = {"mailboxes": [{"email": "a@x.de", "sig": True, "smime": False,
                           "use_policy": True}], "update_dg": False}
    r = _run(mbroute.api_save_mailboxes(body))
    assert r["ok"] is True

    neu = gespeichert["MAILBOX_CONFIG"]["a@x.de"]
    assert neu["ooo_calendar"] is True
    assert neu["oof_announce"] is True and neu["oof_announce_mode"] == "tage"
    assert neu["oof_announce_x"] == 7 and neu["oof_announce_privat"] is False
    assert neu["oof_announce_extern"] is True
    assert neu["self_templates"] is True


# ── Wahl-Slots (Signatur, Antwort-Signatur, Abwesenheit) seit v1.9.113 ─────────

def _speichern(monkeypatch, vorher: dict, zeile: dict) -> dict:
    gespeichert: dict = {}

    async def fake_auto(neu):
        return {}
    store = {"MAILBOX_CONFIG": vorher, "USER_BOOKINGS": {}}
    monkeypatch.setattr(mbroute.settings_store, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(mbroute.settings_store, "get_all", lambda: {})
    monkeypatch.setattr(mbroute.settings_store, "update", lambda patch: gespeichert.update(patch))
    import exo_mailboxes
    monkeypatch.setattr(exo_mailboxes, "list_mailboxes", lambda _a: [])
    monkeypatch.setattr(mbroute, "_auto_enrollment_anstossen", fake_auto)
    r = _run(mbroute.api_save_mailboxes({"mailboxes": [zeile], "update_dg": False}))
    assert r["ok"] is True
    return gespeichert["MAILBOX_CONFIG"]["a@x.de"]


def test_wahl_wird_auch_bei_use_policy_true_gespeichert(monkeypatch):
    """Vorher wurden Signatur/Abwesenheit bei use_policy=true verworfen — eine
    eigene Wahl ging nur mit use_policy=false und kostete Banner/Disclaimer."""
    neu = _speichern(monkeypatch, {}, {
        "email": "a@x.de", "sig": True, "smime": False, "use_policy": True,
        "template": "Meine", "min_template": "", "oof_template": "Urlaub",
        "banner_template": "Eingefroren"})
    assert neu["template"] == "Meine"
    assert neu["min_template"] == "", "„keine“ ist eine Wahl und muss stehen bleiben"
    assert neu["oof_template"] == "Urlaub"
    assert "banner_template" not in neu, "Richtlinien-Slot bei use_policy=true nicht einfrieren"


def test_standard_wahl_entfernt_das_feld(monkeypatch):
    neu = _speichern(monkeypatch,
                     {"a@x.de": {"sig": True, "use_policy": False, "template": "Alt"}},
                     {"email": "a@x.de", "sig": True, "smime": False, "use_policy": False,
                      "template": "__standard__", "min_template": "__standard__",
                      "oof_template": "__standard__"})
    for feld in ("template", "min_template", "oof_template"):
        assert feld not in neu, f"{feld}: Standard heisst „kein eigenes Feld“"


def test_fehlende_angabe_behaelt_die_bisherige_wahl(monkeypatch):
    """Schickt eine Anfrage die Wahl-Felder nicht mit, bleibt die (z.B. im
    Self-Service getroffene) Wahl stehen, statt zu verschwinden."""
    neu = _speichern(monkeypatch,
                     {"a@x.de": {"sig": True, "use_policy": True,
                                 "template": "Selbst", "oof_template": ""}},
                     {"email": "a@x.de", "sig": True, "smime": False, "use_policy": True})
    assert neu["template"] == "Selbst"
    assert neu["oof_template"] == ""
