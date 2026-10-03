"""Admin-Dashboard „Abwesenheit": zentrales Setzen pro Nutzer.

Der Kern: Der Admin darf die Adresse frei wählen (anders als der Self-Service),
aber nur für ein VERWALTETES Postfach, und sein Setzen schreibt die
Postfach-Konfig (Kalender/Ankündigung) UND setzt die OOF bei Exchange mit dem
gewählten Status. Die Graph-Nahtstellen sind gemockt.
"""
from __future__ import annotations

import asyncio

import pytest

import abwesenheit
import graph_client
import mailbox_match
import settings_store
from graph_client import UserData
from webui.routen import abwesenheit as dash


def _run(coro):
    return asyncio.run(coro)


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


_MB = {"g1": {"known_addresses": ["a@x.de"], "primary": "a@x.de", "sig": True}}


def _mock(monkeypatch, store, patched):
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(settings_store, "update", lambda patch: store.update(patch))

    async def fake_token():
        return "TOK"

    async def fake_get(email, token):
        return "ok", {"status": "disabled", "internalReplyMessage": "ALT", "externalReplyMessage": "ALT"}

    async def fake_patch(email, token, setting, html, html_extern=None, status=None, start=None, ende=None):
        patched.update({"email": email, "status": status, "html": html, "start": start})
        return {"internalReplyMessage": html, "externalReplyMessage": html}

    async def fake_user(email):
        return UserData(displayName="A", custom={})

    async def fake_render(user, upn, token, tpl, cfg, state, z, ab="", bis=""):
        return ("<p>OOF</p>", "<p>OOF</p>")

    monkeypatch.setattr(graph_client, "_acquire_token_async", fake_token)
    monkeypatch.setattr(graph_client, "get_user", fake_user)
    monkeypatch.setattr(abwesenheit, "_get_setting", fake_get)
    monkeypatch.setattr(abwesenheit, "_patch_setting", fake_patch)
    monkeypatch.setattr(abwesenheit, "render_intern_extern", fake_render)
    monkeypatch.setattr(abwesenheit, "oof_vorlage_fuer", lambda *a: "Firma")
    monkeypatch.setattr(abwesenheit, "_state", lambda: {})
    monkeypatch.setattr(abwesenheit, "_state_speichern", lambda st: None)


def test_admin_setzt_schreibt_konfig_und_patcht(monkeypatch):
    store = {"MAILBOX_CONFIG": {k: dict(v) for k, v in _MB.items()}}
    patched: dict = {}
    _mock(monkeypatch, store, patched)
    body = {"email": "a@x.de", "oof_status": "scheduled",
            "oof_start": "2026-11-01", "oof_ende": "2026-11-05", "ganztaegig": True,
            "ooo_calendar": "1",
            "announce": {"an": True, "mode": "tage", "x": 7, "privat": False, "extern": True}}
    r = _run(dash.api_abwesenheit_postfach_setzen(_Req(body)))
    assert r.status_code == 200
    # Konfig geschrieben:
    eintrag = store["MAILBOX_CONFIG"]["g1"]
    assert eintrag["ooo_calendar"] is True
    assert eintrag["oof_announce_mode"] == "tage" and eintrag["oof_announce_x"] == 7
    assert eintrag["oof_announce_privat"] is False and eintrag["oof_announce_extern"] is True
    # OOF bei Exchange gesetzt, mit dem gewählten Status + Zeitplan:
    assert patched["email"] == "a@x.de" and patched["status"] == "scheduled"
    assert patched["start"]["dateTime"].startswith("2026-11-01")


def test_admin_kalender_vorgabe_entfernt_feld(monkeypatch):
    """`ooo_calendar=""` (Vorgabe) entfernt einen zuvor gesetzten Postfach-Wert."""
    store = {"MAILBOX_CONFIG": {"g1": {**_MB["g1"], "ooo_calendar": True}}}
    _mock(monkeypatch, store, {})
    body = {"email": "a@x.de", "oof_status": "disabled", "ooo_calendar": "",
            "announce": {"an": True}}
    _run(dash.api_abwesenheit_postfach_setzen(_Req(body)))
    assert "ooo_calendar" not in store["MAILBOX_CONFIG"]["g1"]


def test_admin_fremdes_postfach_404(monkeypatch):
    from fastapi import HTTPException
    store = {"MAILBOX_CONFIG": {k: dict(v) for k, v in _MB.items()}}
    _mock(monkeypatch, store, {})
    body = {"email": "fremd@y.de", "oof_status": "disabled", "announce": {}}
    with pytest.raises(HTTPException) as exc:
        _run(dash.api_abwesenheit_postfach_setzen(_Req(body)))
    assert exc.value.status_code == 404
