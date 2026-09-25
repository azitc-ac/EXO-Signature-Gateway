"""Zentrale Abwesenheitsnotiz (Ansatz B): Zeitraum, Platzhalter, Idempotenz.

Die tragende Invariante ist die IDEMPOTENZ: ein zweiter Poll ohne echte Änderung
darf NICHT erneut PATCHen — sonst setzt Exchange seine „einmal je Absender"-Dedup
zurück und der Empfänger bekommt bei jedem Lauf eine weitere Auto-Antwort. Der
Test schlägt fehl, wenn man diese Prüfung zurückbaut.

Der echte Graph-Zugriff ist an den Nahtstellen gemockt — die Berechtigung
(MailboxSettings.ReadWrite) ist bis zum Admin-Consent nicht erteilt.
"""
from __future__ import annotations

import asyncio

import pytest

import abwesenheit
import config
import graph_client
import settings_store
import signature_engine
from graph_client import UserData


def _run(coro):
    return asyncio.run(coro)


# ── Zeitraum ──────────────────────────────────────────────────────────────────

def test_zeitraum_scheduled():
    s = {"status": "scheduled",
         "scheduledStartDateTime": {"dateTime": "2026-10-01T00:00:00.0000000", "timeZone": "UTC"},
         "scheduledEndDateTime": {"dateTime": "2026-10-10T00:00:00.0000000", "timeZone": "UTC"}}
    assert abwesenheit.zeitraum_text(s) == "vom 01.10.2026 bis 10.10.2026"


def test_zeitraum_unbefristet_leer():
    assert abwesenheit.zeitraum_text({"status": "alwaysEnabled"}) == ""


# ── Platzhalter ───────────────────────────────────────────────────────────────

def test_render_ersetzt_name_und_zeitraum(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    (tmp_path / "Firma.html").write_text("<p>{name} ist weg {zeitraum}.</p>", encoding="utf-8")
    (tmp_path / "Firma.txt").write_text("{name} ist weg {zeitraum}.", encoding="utf-8")
    signature_engine._reload_env()
    html, txt = abwesenheit.render_oof(
        UserData(displayName="Erika", custom={}), "Firma", "vom 01.10.2026 bis 10.10.2026")
    assert "Erika" in html and "vom 01.10.2026 bis 10.10.2026" in html
    assert "{name}" not in html and "{zeitraum}" not in html
    assert txt == "Erika ist weg vom 01.10.2026 bis 10.10.2026."


def test_render_ersetzt_start_und_ende(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    (tmp_path / "Firma.html").write_text("<p>ab {abwesend_ab}, zurück am {abwesend_bis}</p>", encoding="utf-8")
    (tmp_path / "Firma.txt").write_text("ab {abwesend_ab}, zurück am {abwesend_bis}", encoding="utf-8")
    signature_engine._reload_env()
    html, txt = abwesenheit.render_oof(
        UserData(displayName="E", custom={}), "Firma", "", ab="10.09.2026", bis="25.09.2026")
    assert "ab 10.09.2026, zurück am 25.09.2026" in html
    assert txt == "ab 10.09.2026, zurück am 25.09.2026"


def test_start_ende_text():
    s = {"status": "scheduled",
         "scheduledStartDateTime": {"dateTime": "2026-09-10T00:00:00.0000000", "timeZone": "UTC"},
         "scheduledEndDateTime": {"dateTime": "2026-09-25T00:00:00.0000000", "timeZone": "UTC"}}
    assert abwesenheit.start_ende_text(s) == ("10.09.2026", "25.09.2026")
    assert abwesenheit.start_ende_text({"status": "alwaysEnabled"}) == ("", "")


# ── Vorlagenauswahl ───────────────────────────────────────────────────────────

def test_oof_vorlage_aus_policy(monkeypatch):
    store = {"TEMPLATE_POLICIES": {"oof": "Firma"}, "CUSTOM_POLICIES": [], "INTERNAL_GROUPS": {}}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: store.get(k, d))
    assert abwesenheit.oof_vorlage_fuer("a@x.de", {}, {"use_policy": True}) == "Firma"


def test_oof_leer_bei_use_policy_false(monkeypatch):
    store = {"TEMPLATE_POLICIES": {"oof": "Firma"}, "CUSTOM_POLICIES": [], "INTERNAL_GROUPS": {}}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: store.get(k, d))
    assert abwesenheit.oof_vorlage_fuer("a@x.de", {}, {"use_policy": False}) == ""


# ── Ein Postfach normalisieren (gemockte Nahtstellen) ─────────────────────────

def _mock_seams(monkeypatch, setting, calls, canonical):
    async def fake_get(upn, token):
        return "ok", dict(setting)

    async def fake_patch(upn, token, s, html):
        calls["patch"] += 1
        setting["internalReplyMessage"] = canonical["internalReplyMessage"]
        setting["externalReplyMessage"] = canonical["externalReplyMessage"]
        return canonical

    async def fake_user(upn):
        return UserData(displayName="E", custom={})

    monkeypatch.setattr(abwesenheit, "_get_setting", fake_get)
    monkeypatch.setattr(abwesenheit, "_patch_setting", fake_patch)
    monkeypatch.setattr(graph_client, "get_user", fake_user)
    monkeypatch.setattr(abwesenheit, "oof_vorlage_fuer", lambda *a: "Firma")
    monkeypatch.setattr(signature_engine, "render", lambda u, template_name=None: ("<p>OOF</p>", "OOF"))
    monkeypatch.setattr(abwesenheit, "_state_speichern", lambda st: None)


def test_idempotenz_zweiter_lauf_patcht_nicht(monkeypatch):
    setting = {"status": "alwaysEnabled", "internalReplyMessage": "ALT", "externalReplyMessage": "ALT"}
    calls = {"patch": 0}
    canonical = {"internalReplyMessage": "<p>OOF</p>", "externalReplyMessage": "<p>OOF</p>"}
    _mock_seams(monkeypatch, setting, calls, canonical)

    state: dict = {}
    r1 = _run(abwesenheit.setze_fuer_postfach("a@x.de", "a@x.de", {}, {}, "TOK", state))
    assert r1 == abwesenheit.GESETZT
    assert calls["patch"] == 1

    # Zweiter Lauf: aktueller Text == zuletzt gesetzter (kanonisch) → kein PATCH.
    r2 = _run(abwesenheit.setze_fuer_postfach("a@x.de", "a@x.de", {}, {}, "TOK", state))
    assert r2 == abwesenheit.UNVERAENDERT
    assert calls["patch"] == 1  # NICHT erneut gepatcht — DIE Idempotenz-Invariante


def test_nutzer_hat_text_geaendert_wird_neu_gesetzt(monkeypatch):
    setting = {"status": "alwaysEnabled", "internalReplyMessage": "ALT", "externalReplyMessage": "ALT"}
    calls = {"patch": 0}
    canonical = {"internalReplyMessage": "<p>OOF</p>", "externalReplyMessage": "<p>OOF</p>"}
    _mock_seams(monkeypatch, setting, calls, canonical)

    state = {"a@x.de": {"intern": "<p>ANDERS</p>", "extern": "<p>ANDERS</p>"}}
    r = _run(abwesenheit.setze_fuer_postfach("a@x.de", "a@x.de", {}, {}, "TOK", state))
    assert r == abwesenheit.GESETZT
    assert calls["patch"] == 1


def test_disabled_bekommt_trotzdem_den_text(monkeypatch):
    """Auch bei AUSgeschalteter Abwesenheit wird der Firmentext hinterlegt (damit
    er beim Einschalten schon dasteht) — Status bleibt disabled, nichts wird
    versendet."""
    setting = {"status": "disabled", "internalReplyMessage": "", "externalReplyMessage": ""}
    calls = {"patch": 0}
    canonical = {"internalReplyMessage": "<p>OOF</p>", "externalReplyMessage": "<p>OOF</p>"}
    _mock_seams(monkeypatch, setting, calls, canonical)

    state: dict = {}
    r = _run(abwesenheit.setze_fuer_postfach("a@x.de", "a@x.de", {}, {}, "TOK", state))
    assert r == abwesenheit.GESETZT
    assert calls["patch"] == 1                       # Text wurde gesetzt
    assert "a@x.de" in state                          # und gemerkt (Idempotenz)


def test_403_meldet_kein_zugriff(monkeypatch):
    async def fake_get(upn, token):
        return "kein_zugriff", None
    monkeypatch.setattr(abwesenheit, "_get_setting", fake_get)
    monkeypatch.setattr(abwesenheit, "oof_vorlage_fuer", lambda *a: "Firma")
    r = _run(abwesenheit.setze_fuer_postfach("a@x.de", "a@x.de", {}, {}, "T", {}))
    assert r == abwesenheit.KEIN_ZUGRIFF


def test_ohne_vorlage_wird_uebersprungen(monkeypatch):
    monkeypatch.setattr(abwesenheit, "oof_vorlage_fuer", lambda *a: "")
    r = _run(abwesenheit.setze_fuer_postfach("a@x.de", "a@x.de", {}, {}, "T", {}))
    assert r == abwesenheit.KEINE_VORLAGE


# ── Poll-Steuerung ────────────────────────────────────────────────────────────

def test_poll_aus_wenn_deaktiviert(monkeypatch):
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: {"OOO_ENABLED": False}.get(k, d))
    r = _run(abwesenheit.poll_alle())
    assert r["aktiv"] is False


def test_poll_persistiert_letzten_lauf(monkeypatch):
    """Die laufende Zahl (Tagesbericht/Übersicht) speist sich aus _OOO_LAST — der
    Poll muss sie schreiben, mit Bezugsgröße (gesamt) und Zeitstempel."""
    store = {"OOO_ENABLED": True, "MAILBOX_CONFIG": {}}
    captured: dict = {}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(settings_store, "force_update", lambda patch: captured.update(patch))
    r = _run(abwesenheit.poll_alle())
    assert r["aktiv"] is True and r["gesamt"] == 0
    assert "_OOO_LAST" in captured
    assert captured["_OOO_LAST"]["gesamt"] == 0 and "ts" in captured["_OOO_LAST"]


def test_aktive_postfaecher_email_und_guid(monkeypatch):
    cfg = {
        "a@x.de": {"sig": True},                                   # klassisch, aktiv
        "b@x.de": {"sig": False, "smime": False},                 # inaktiv
        "guid-1": {"primary": "c@x.de", "smime": True},           # GUID-Anker, aktiv
        "guid-2": {"primary": "", "sig": True},                   # kein UPN → raus
    }
    out = dict(abwesenheit._aktive_postfaecher(cfg))
    assert "a@x.de" in out and "c@x.de" in out
    assert "b@x.de" not in out
