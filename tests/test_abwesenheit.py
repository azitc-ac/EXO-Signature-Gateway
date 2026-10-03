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


def test_zeitraum_eintaegig_wird_geglaettet():
    """Start und Ende am selben Tag → „am X" statt „vom X bis X"."""
    s = {"status": "scheduled",
         "scheduledStartDateTime": {"dateTime": "2026-09-30T11:30:00.0000000", "timeZone": "UTC"},
         "scheduledEndDateTime": {"dateTime": "2026-09-30T20:00:00.0000000", "timeZone": "UTC"}}
    assert abwesenheit.zeitraum_text(s) == "am 30.09.2026"


def test_fenster_texte_eintaegig():
    s = {"dateTime": "2026-09-30T11:30:00", "timeZone": "UTC"}
    e = {"dateTime": "2026-09-30T20:00:00", "timeZone": "UTC"}
    z, ab, bis = abwesenheit._fenster_texte(s, e)
    assert z == "am 30.09.2026" and ab == "30.09.2026" and bis == "30.09.2026"


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


def test_render_platzhalter_als_jinja(tmp_path, monkeypatch):
    """Vereinheitlichte Syntax: {{ zeitraum }} / {{ name }} funktionieren wie
    Template-Variablen (nicht nur die {..}-Kurzform)."""
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    (tmp_path / "J.html").write_text("<p>{{ name }} ist weg {{ zeitraum }}</p>", encoding="utf-8")
    (tmp_path / "J.txt").write_text("{{ name }} ist weg {{ zeitraum }}", encoding="utf-8")
    signature_engine._reload_env()
    html, txt = abwesenheit.render_oof(
        UserData(displayName="Erika", custom={}), "J", "am 30.09.2026")
    assert "Erika ist weg am 30.09.2026" in html
    assert txt == "Erika ist weg am 30.09.2026"


def test_nachricht_als_absatz_entfernt_tabelle():
    """Einspaltige Baukasten-Tabelle → <p>-Absatz (Outlook kollabiert sonst die
    breitenlose Zelle auf 24pt). Inline-Links bleiben erhalten."""
    html = ('<table cellpadding="0" cellspacing="0" border="0" '
            'style="font-family:Calibri;font-size:11pt;color:#1f2937;border-collapse:collapse">\n'
            '  <tr><td style="padding:0">Ich bin weg. Kontakt: '
            '<a href="mailto:x@y.de">x@y.de</a>.</td></tr>\n</table>')
    out = abwesenheit._nachricht_als_absatz(html)
    assert "<table" not in out and "<td" not in out
    assert out.startswith("<p ")
    assert '<a href="mailto:x@y.de">x@y.de</a>' in out      # Inline-Auszeichnung bleibt
    assert "font-family:Calibri" in out and "font-size:11pt" in out


def test_nachricht_als_absatz_laesst_verschachteltes_unveraendert():
    """Zwei/verschachtelte Tabellen: konservativ unverändert lassen (nicht zerlegen)."""
    html = ('<table style="x"><tr><td>A</td></tr></table>'
            '<table style="y"><tr><td>B</td></tr></table>')
    assert abwesenheit._nachricht_als_absatz(html) == html


def test_render_oof_erzeugt_absatz_statt_tabelle(tmp_path, monkeypatch):
    """End-to-End: eine Baukasten-Tabellen-Vorlage wird als Absatz gerendert."""
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    (tmp_path / "T.html").write_text(
        '<table cellpadding="0" cellspacing="0" border="0" '
        'style="font-family:Calibri;font-size:11pt;color:#1f2937;border-collapse:collapse">\n'
        '  <tr><td style="padding:0">Ich bin {{ zeitraum }} nicht da.</td></tr>\n</table>',
        encoding="utf-8")
    (tmp_path / "T.txt").write_text("Ich bin {{ zeitraum }} nicht da.", encoding="utf-8")
    signature_engine._reload_env()
    html, _ = abwesenheit.render_oof(
        UserData(displayName="Erika", custom={}), "T", "am 30.09.2026")
    assert "<table" not in html and "<td" not in html
    assert "Ich bin am 30.09.2026 nicht da." in html


def test_period_en_varianten():
    assert abwesenheit._period_en("01.10.2026", "05.10.2026") == "from 01.10.2026 to 05.10.2026"
    assert abwesenheit._period_en("01.10.2026", "01.10.2026") == "on 01.10.2026"   # Ein-Tages
    assert abwesenheit._period_en("", "05.10.2026") == "until 05.10.2026"
    assert abwesenheit._period_en("01.10.2026", "") == "from 01.10.2026"
    assert abwesenheit._period_en("", "") == ""


def test_render_oof_namensraum(tmp_path, monkeypatch):
    """Neuer oof-Namensraum: {{ oof.name }} / {{ oof.zeitraum }} / {{ oof.period }}."""
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    (tmp_path / "N.html").write_text(
        "<p>{{ oof.name }} — {{ oof.zeitraum }} / {{ oof.period }} "
        "({{ oof.abwesend_ab }}–{{ oof.abwesend_bis }})</p>", encoding="utf-8")
    (tmp_path / "N.txt").write_text("{{ oof.name }} {{ oof.period }}", encoding="utf-8")
    signature_engine._reload_env()
    html, txt = abwesenheit.render_oof(
        UserData(displayName="Erika", custom={}),
        "N", "vom 01.10.2026 bis 05.10.2026", "01.10.2026", "05.10.2026")
    assert "Erika" in html and "vom 01.10.2026 bis 05.10.2026" in html
    assert "from 01.10.2026 to 05.10.2026" in html      # oof.period (Englisch)
    assert "01.10.2026–05.10.2026" in html              # ab/bis
    assert txt == "Erika from 01.10.2026 to 05.10.2026"


def test_render_oof_kurzform_mit_praefix(tmp_path, monkeypatch):
    """Kurzform mit Präfix: {oof.zeitraum} / {oof.period} werden ersetzt."""
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    (tmp_path / "K.html").write_text("<p>{oof.zeitraum} · {oof.period}</p>", encoding="utf-8")
    (tmp_path / "K.txt").write_text("x", encoding="utf-8")
    signature_engine._reload_env()
    html, _ = abwesenheit.render_oof(
        UserData(displayName="E", custom={}), "K",
        "am 01.10.2026", "01.10.2026", "01.10.2026")
    assert "am 01.10.2026 · on 01.10.2026" in html
    assert "{oof." not in html


def test_render_haengt_anhang_an(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    (tmp_path / "K.html").write_text("<p>OOF</p>", encoding="utf-8")
    (tmp_path / "K.txt").write_text("OOF", encoding="utf-8")
    signature_engine._reload_env()
    html, txt = abwesenheit.render_oof(
        UserData(displayName="E", custom={}), "K", "", anhang_html="<p>SIG</p>", anhang_txt="SIG")
    assert html == "<p>OOF</p><p>SIG</p>"
    assert txt == "OOF\nSIG"


def test_oof_anhang_signatur_und_banner(monkeypatch):
    """_oof_anhang hängt nur an, was per Schalter aktiviert ist."""
    store = {"OOO_APPEND_SIGNATURE": True, "OOO_APPEND_BANNER": True,
             "TEMPLATE_POLICIES": {"sig": "Sig", "banner": "Ban"},
             "CUSTOM_POLICIES": [], "INTERNAL_GROUPS": {}}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: store.get(k, d))
    def fake_render(u, template_name=None, extra=None):
        return (f"<sig:{template_name}>", f"sig:{template_name}")
    monkeypatch.setattr(signature_engine, "render", fake_render)
    h, t = abwesenheit._oof_anhang(UserData(custom={}), "a@x.de", {}, {"use_policy": True})
    assert "<sig:Sig>" in h and "<sig:Ban>" in h


def test_oof_anhang_aus_wenn_schalter_aus(monkeypatch):
    store = {"OOO_APPEND_SIGNATURE": False, "OOO_APPEND_BANNER": False,
             "TEMPLATE_POLICIES": {"sig": "Sig"}, "CUSTOM_POLICIES": [], "INTERNAL_GROUPS": {}}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: store.get(k, d))
    h, t = abwesenheit._oof_anhang(UserData(custom={}), "a@x.de", {}, {"use_policy": True})
    assert h == "" and t == ""


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


def test_oof_leer_bei_use_policy_false_ohne_feld(monkeypatch):
    """use_policy=false UND kein Postfach-eigenes oof_template → keine Vorlage."""
    store = {"TEMPLATE_POLICIES": {"oof": "Firma"}, "CUSTOM_POLICIES": [], "INTERNAL_GROUPS": {}}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: store.get(k, d))
    assert abwesenheit.oof_vorlage_fuer("a@x.de", {}, {"use_policy": False}) == ""


def test_oof_vorlage_per_postfach_bei_use_policy_false(monkeypatch):
    """use_policy=false → Postfach-eigenes oof_template gilt (wie template/banner_template);
    die globale Richtlinie wird NICHT aufgezwungen."""
    store = {"TEMPLATE_POLICIES": {"oof": "Firma"}, "CUSTOM_POLICIES": [], "INTERNAL_GROUPS": {}}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: store.get(k, d))
    cfg = {"use_policy": False, "oof_template": "Eigene"}
    assert abwesenheit.oof_vorlage_fuer("a@x.de", {}, cfg) == "Eigene"


# ── Ein Postfach normalisieren (gemockte Nahtstellen) ─────────────────────────

def _mock_seams(monkeypatch, setting, calls, canonical):
    async def fake_get(upn, token):
        return "ok", dict(setting)

    async def fake_patch(upn, token, s, html, html_extern=None, status=None, start=None, ende=None):
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
    monkeypatch.setattr(signature_engine, "render", lambda u, template_name=None, extra=None: ("<p>OOF</p>", "OOF"))
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


def test_vorlagenaenderung_propagiert_trotz_gleichem_exchange_text(monkeypatch):
    """Ändert sich UNSER Render (Vorlage/Signatur/Variable), muss neu gesetzt
    werden — auch wenn Exchange noch exakt den zuletzt gesetzten Text trägt.
    Schlägt fehl, wenn nur „Exchange == zuletzt-gesetzt" verglichen wird."""
    # Exchange trägt den ALTEN Text; state kennt ihn als kanonisch UND als render.
    setting = {"status": "alwaysEnabled",
               "internalReplyMessage": "<p>ALT</p>", "externalReplyMessage": "<p>ALT</p>"}
    calls = {"patch": 0}
    canonical = {"internalReplyMessage": "<p>NEU</p>", "externalReplyMessage": "<p>NEU</p>"}
    _mock_seams(monkeypatch, setting, calls, canonical)
    # signature_engine.render liefert jetzt den NEUEN Text (Vorlage wurde geändert):
    monkeypatch.setattr(signature_engine, "render",
                        lambda u, template_name=None, extra=None: ("<p>NEU</p>", "NEU"))
    state = {"a@x.de": {"intern": "<p>ALT</p>", "extern": "<p>ALT</p>", "render": "<p>ALT</p>"}}
    r = _run(abwesenheit.setze_fuer_postfach("a@x.de", "a@x.de", {}, {}, "TOK", state))
    assert r == abwesenheit.GESETZT
    assert calls["patch"] == 1                       # neu gesetzt, obwohl Exchange==ALT==kanonisch
    assert state["a@x.de"]["render"] == "<p>NEU</p>"  # neuer Render gemerkt


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


def test_fenster_texte():
    s = {"dateTime": "2026-10-01T00:00:00", "timeZone": "UTC"}
    e = {"dateTime": "2026-10-10T00:00:00", "timeZone": "UTC"}
    z, ab, bis = abwesenheit._fenster_texte(s, e)
    assert z == "vom 01.10.2026 bis 10.10.2026" and ab == "01.10.2026" and bis == "10.10.2026"


def test_kalender_auto_aktiviert_bei_disabled(monkeypatch):
    """Ist die Abwesenheit AUS und liegt ein qualifizierender Kalendertermin vor,
    schaltet die Automatik die native Abwesenheit für dessen Fenster ein — und
    respektiert danach ein manuelles Wieder-Ausschalten (kein erneutes Aktivieren
    für dasselbe Fenster)."""
    setting = {"status": "disabled", "internalReplyMessage": "", "externalReplyMessage": ""}
    patch_calls: list = []

    async def fake_get(upn, token):
        return "ok", dict(setting)

    async def fake_patch(upn, token, s, html, html_extern=None, status=None, start=None, ende=None):
        patch_calls.append({"status": status, "start": start, "ende": ende})
        return {"internalReplyMessage": html, "externalReplyMessage": html}

    async def fake_user(upn):
        return UserData(displayName="E", custom={})

    async def fake_events(upn, token):
        return [{"start": {"dateTime": "2026-10-01T00:00:00", "timeZone": "UTC"},
                 "end": {"dateTime": "2026-10-10T00:00:00", "timeZone": "UTC"}, "privat": False}]

    monkeypatch.setattr(abwesenheit, "_get_setting", fake_get)
    monkeypatch.setattr(abwesenheit, "_patch_setting", fake_patch)
    monkeypatch.setattr(graph_client, "get_user", fake_user)
    monkeypatch.setattr(abwesenheit, "_kalender_oof_events", fake_events)
    monkeypatch.setattr(abwesenheit, "oof_vorlage_fuer", lambda *a: "Firma")
    monkeypatch.setattr(signature_engine, "render", lambda u, template_name=None, extra=None: ("<p>OOF</p>", "OOF"))
    monkeypatch.setattr(abwesenheit, "_state_speichern", lambda st: None)
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: {"OOO_CALENDAR_AUTO": True}.get(k, d))

    state: dict = {}
    r = _run(abwesenheit.setze_fuer_postfach("a@x.de", "a@x.de", {}, {}, "T", state))
    assert r == abwesenheit.GESETZT
    assert patch_calls[0]["status"] == "scheduled"
    assert patch_calls[0]["start"]["dateTime"].startswith("2026-10-01")
    assert state["a@x.de"].get("auto_win")   # Fenster gemerkt

    # Nutzer schaltet von Hand wieder aus (Status bleibt disabled, gleiches Fenster)
    patch_calls.clear()
    _run(abwesenheit.setze_fuer_postfach("a@x.de", "a@x.de", {}, {}, "T", state))
    assert all(pc["status"] is None for pc in patch_calls), "darf nicht erneut aktivieren"


def test_kalender_auto_aus_lässt_status(monkeypatch):
    """Ist die Automatik AUS, wird der Status nie verändert (nur Text)."""
    setting = {"status": "disabled", "internalReplyMessage": "", "externalReplyMessage": ""}
    patch_calls: list = []

    async def fake_get(upn, token):
        return "ok", dict(setting)

    async def fake_patch(upn, token, s, html, html_extern=None, status=None, start=None, ende=None):
        patch_calls.append(status)
        return {"internalReplyMessage": html, "externalReplyMessage": html}

    async def fake_user(upn):
        return UserData(displayName="E", custom={})
    monkeypatch.setattr(abwesenheit, "_get_setting", fake_get)
    monkeypatch.setattr(abwesenheit, "_patch_setting", fake_patch)
    monkeypatch.setattr(graph_client, "get_user", fake_user)
    monkeypatch.setattr(abwesenheit, "oof_vorlage_fuer", lambda *a: "Firma")
    monkeypatch.setattr(signature_engine, "render", lambda u, template_name=None, extra=None: ("<p>OOF</p>", "OOF"))
    monkeypatch.setattr(abwesenheit, "_state_speichern", lambda st: None)
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: {"OOO_CALENDAR_AUTO": False}.get(k, d))

    _run(abwesenheit.setze_fuer_postfach("a@x.de", "a@x.de", {}, {}, "T", {}))
    assert patch_calls == [None]   # nur Text, kein Status


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


def test_poll_persistiert_state_einmal_und_verarbeitet_alle(monkeypatch):
    """B1-Skalierung: State wird GENAU EINMAL persistiert (nicht je Postfach — sonst
    N volle settings.json-Schreibvorgänge pro Poll), und alle Postfächer laufen
    (nebenläufig) durch."""
    speichern = []
    async def fake_get(upn, token):
        return "ok", {"status": "disabled", "internalReplyMessage": "", "externalReplyMessage": ""}
    async def fake_patch(upn, token, setting, html, **kw):
        return {"internalReplyMessage": html, "externalReplyMessage": html}
    async def fake_user(upn):
        return UserData(displayName="E", custom={})
    async def fake_token():
        return "TOK"
    monkeypatch.setattr(abwesenheit, "_aktive_postfaecher",
                        lambda cfg: [("a@x.de", {}), ("b@x.de", {}), ("c@x.de", {})])
    monkeypatch.setattr(abwesenheit, "_get_setting", fake_get)
    monkeypatch.setattr(abwesenheit, "_patch_setting", fake_patch)
    monkeypatch.setattr(graph_client, "get_user", fake_user)
    monkeypatch.setattr(graph_client, "_acquire_token_async", fake_token)
    monkeypatch.setattr(abwesenheit, "oof_vorlage_fuer", lambda *a: "Firma")
    monkeypatch.setattr(signature_engine, "render",
                        lambda u, template_name=None, extra=None: ("<p>OOF</p>", "OOF"))
    monkeypatch.setattr(abwesenheit, "_state_speichern", lambda st: speichern.append(1))
    monkeypatch.setattr(abwesenheit, "_persist_last", lambda s: None)
    monkeypatch.setattr(settings_store, "get",
                        lambda k, d=None: {"OOO_ENABLED": True, "MAILBOX_CONFIG": {"x": 1},
                                           "OOO_CALENDAR_AUTO": False}.get(k, d))
    r = _run(abwesenheit.poll_alle())
    assert r["gesamt"] == 3 and r[abwesenheit.GESETZT] == 3
    assert speichern == [1], f"State muss genau EINMAL persistiert werden, war {len(speichern)}×"


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


# ── B2: per-Postfach-Opt-in + Kalender-Cache ──────────────────────────────────

def test_kalender_auto_an_per_postfach_uebersteuert_global(monkeypatch):
    """`ooo_calendar` je Postfach schlägt den Tenant-Default OOO_CALENDAR_AUTO;
    fehlt das Feld, gilt der Default. Schlägt fehl, wenn die Rangfolge kippt."""
    def _mit(global_auto):
        monkeypatch.setattr(settings_store, "get",
                            lambda k, d=None: {"OOO_CALENDAR_AUTO": global_auto}.get(k, d))
    _mit(True)
    assert abwesenheit._kalender_auto_an({}) is True            # kein Feld → Default an
    assert abwesenheit._kalender_auto_an({"ooo_calendar": False}) is False  # Postfach aus
    _mit(False)
    assert abwesenheit._kalender_auto_an({}) is False           # kein Feld → Default aus
    assert abwesenheit._kalender_auto_an({"ooo_calendar": True}) is True    # Postfach an


def test_kalender_cache_vermeidet_zweiten_read(monkeypatch):
    """Der zweite Aufruf innerhalb des Refresh-Fensters liest NICHT erneut bei
    Graph — das ist der Skalierungs-Kern. Schlägt fehl, wenn der Cache entfällt."""
    reads = {"n": 0}

    async def fake_events(upn, token):
        reads["n"] += 1
        return [{"start": {"dateTime": "2026-10-01T00:00:00", "timeZone": "UTC"},
                 "end": {"dateTime": "2026-10-10T00:00:00", "timeZone": "UTC"}, "privat": False}]

    monkeypatch.setattr(abwesenheit, "_kalender_oof_events", fake_events)
    monkeypatch.setattr(settings_store, "get",
                        lambda k, d=None: {"OOO_CALENDAR_REFRESH_HOURS": 6}.get(k, d))
    state: dict = {}
    f1 = _run(abwesenheit.kalender_fenster("a@x.de", "T", state))
    f2 = _run(abwesenheit.kalender_fenster("a@x.de", "T", state))
    assert f1 and f2 and f1[0]["dateTime"].startswith("2026-10-01")
    assert reads["n"] == 1, "zweiter Aufruf hätte den Cache nutzen müssen"
    assert state["a@x.de"]["kal"]["events"][0]["start"]["dateTime"].startswith("2026-10-01")


def test_kalender_cache_refresh_nach_ablauf(monkeypatch):
    """Ist der gecachte Wert älter als OOO_CALENDAR_REFRESH_HOURS, wird neu gelesen."""
    reads = {"n": 0}

    async def fake_events(upn, token):
        reads["n"] += 1
        return []     # keine Termine

    monkeypatch.setattr(abwesenheit, "_kalender_oof_events", fake_events)
    monkeypatch.setattr(settings_store, "get",
                        lambda k, d=None: {"OOO_CALENDAR_REFRESH_HOURS": 6}.get(k, d))
    # Cache mit einem 7h alten Zeitstempel vorbelegen → abgelaufen.
    from datetime import datetime, timezone, timedelta
    alt = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    state = {"a@x.de": {"kal": {"ts": alt, "events": []}}}
    _run(abwesenheit.kalender_fenster("a@x.de", "T", state))
    assert reads["n"] == 1, "abgelaufener Cache hätte neu lesen müssen"


def test_kalender_cache_force_liest_neu(monkeypatch):
    """force=True (Self-Save) liest neu, auch wenn der Cache frisch ist."""
    reads = {"n": 0}

    async def fake_events(upn, token):
        reads["n"] += 1
        return []

    monkeypatch.setattr(abwesenheit, "_kalender_oof_events", fake_events)
    monkeypatch.setattr(settings_store, "get",
                        lambda k, d=None: {"OOO_CALENDAR_REFRESH_HOURS": 6}.get(k, d))
    from datetime import datetime, timezone
    state = {"a@x.de": {"kal": {"ts": datetime.now(timezone.utc).isoformat(), "events": []}}}
    _run(abwesenheit.kalender_fenster("a@x.de", "T", state, force=True))
    assert reads["n"] == 1, "force hätte den frischen Cache umgehen müssen"


def test_kalender_cache_fehler_wird_nicht_gecacht(monkeypatch):
    """Ein echter Lesefehler (Exception) darf den Cache NICHT auf „kein Termin"
    festschreiben — sonst bliebe die Automatik nach einem Netz-Schluckauf für
    Stunden blind. Der Zeitstempel bleibt der alte, der nächste Poll versucht es
    erneut."""
    async def fake_events(upn, token):
        raise RuntimeError("Graph 503")

    monkeypatch.setattr(abwesenheit, "_kalender_oof_events", fake_events)
    monkeypatch.setattr(settings_store, "get",
                        lambda k, d=None: {"OOO_CALENDAR_REFRESH_HOURS": 6}.get(k, d))
    from datetime import datetime, timezone, timedelta
    alt = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    state = {"a@x.de": {"kal": {"ts": alt, "events": []}}}
    r = _run(abwesenheit.kalender_fenster("a@x.de", "T", state))
    assert r is None
    assert state["a@x.de"]["kal"]["ts"] == alt, "Fehler darf den Zeitstempel nicht erneuern"


def test_kal_cache_ueberlebt_state_neubau(monkeypatch):
    """Nach setze_fuer_postfach muss der Kalender-Cache (`kal`) im State erhalten
    bleiben — sonst liefe beim nächsten Poll wieder ein calendarView-Aufruf."""
    setting = {"status": "disabled", "internalReplyMessage": "", "externalReplyMessage": ""}
    reads = {"n": 0}

    async def fake_get(upn, token):
        return "ok", dict(setting)

    async def fake_patch(upn, token, s, html, html_extern=None, status=None, start=None, ende=None):
        return {"internalReplyMessage": html, "externalReplyMessage": html}

    async def fake_user(upn):
        return UserData(displayName="E", custom={})

    async def fake_events(upn, token):
        reads["n"] += 1
        return [{"start": {"dateTime": "2026-10-01T00:00:00", "timeZone": "UTC"},
                 "end": {"dateTime": "2026-10-10T00:00:00", "timeZone": "UTC"}, "privat": False}]

    monkeypatch.setattr(abwesenheit, "_get_setting", fake_get)
    monkeypatch.setattr(abwesenheit, "_patch_setting", fake_patch)
    monkeypatch.setattr(graph_client, "get_user", fake_user)
    monkeypatch.setattr(abwesenheit, "_kalender_oof_events", fake_events)
    monkeypatch.setattr(abwesenheit, "oof_vorlage_fuer", lambda *a: "Firma")
    monkeypatch.setattr(signature_engine, "render",
                        lambda u, template_name=None, extra=None: ("<p>OOF</p>", "OOF"))
    monkeypatch.setattr(abwesenheit, "_state_speichern", lambda st: None)
    monkeypatch.setattr(settings_store, "get",
                        lambda k, d=None: {"OOO_CALENDAR_AUTO": True,
                                           "OOO_CALENDAR_REFRESH_HOURS": 6}.get(k, d))
    state: dict = {}
    _run(abwesenheit.setze_fuer_postfach("a@x.de", "a@x.de", {}, {}, "T", state))
    assert "kal" in state["a@x.de"], "Kalender-Cache ging beim State-Neubau verloren"
    # Zweiter Lauf: Cache frisch → kein erneuter calendarView-Read.
    _run(abwesenheit.setze_fuer_postfach("a@x.de", "a@x.de", {}, {}, "T", state))
    assert reads["n"] == 1, "zweiter Poll hätte den Kalender-Cache nutzen müssen"


# ── C: Ankündigung künftiger Abwesenheiten ────────────────────────────────────

def _ev(tage_ab_jetzt_start, tage_ab_jetzt_ende, privat=False):
    from datetime import datetime, timezone, timedelta
    jetzt = datetime.now(timezone.utc)
    def iso(d):
        return (jetzt + timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%S")
    return {"start": {"dateTime": iso(tage_ab_jetzt_start), "timeZone": "UTC"},
            "end": {"dateTime": iso(tage_ab_jetzt_ende), "timeZone": "UTC"},
            "privat": privat}


def test_ankuendigung_auswahl_laufende_und_vergangene_raus():
    """Die aktuell laufende Abwesenheit (steht schon im OOF-Text) und vergangene
    Termine werden ausgenommen; nur künftige bleiben."""
    events = [_ev(-10, -5), _ev(-1, 1), _ev(3, 6), _ev(20, 25)]   # vorbei, laufend, 2 künftige
    cfg = {"mode": "anzahl", "x": 10, "privat": True, "extern": False}
    auswahl = abwesenheit._ankuendigung_auswahl(events, cfg)
    assert len(auswahl) == 2
    # der erste künftige Termin beginnt in 3 Tagen
    from datetime import datetime, timezone
    s0 = abwesenheit._dt(auswahl[0]["start"])
    assert s0 > datetime.now(timezone.utc).replace(tzinfo=None)


def test_ankuendigung_auswahl_modus_anzahl_begrenzt():
    events = [_ev(2, 3), _ev(5, 6), _ev(8, 9), _ev(11, 12)]
    cfg = {"mode": "anzahl", "x": 2, "privat": True, "extern": False}
    assert len(abwesenheit._ankuendigung_auswahl(events, cfg)) == 2


def test_ankuendigung_auswahl_modus_tage_begrenzt():
    events = [_ev(2, 3), _ev(5, 6), _ev(40, 41)]
    cfg = {"mode": "tage", "x": 10, "privat": True, "extern": False}
    auswahl = abwesenheit._ankuendigung_auswahl(events, cfg)
    assert len(auswahl) == 2       # der Termin in 40 Tagen fällt aus dem 10-Tage-Fenster


def test_ankuendigung_auswahl_privat_filter():
    events = [_ev(2, 3, privat=True), _ev(5, 6, privat=False)]
    aus = abwesenheit._ankuendigung_auswahl(events, {"mode": "anzahl", "x": 10, "privat": False, "extern": False})
    assert len(aus) == 1           # privater Termin ausgeschlossen
    ein = abwesenheit._ankuendigung_auswahl(events, {"mode": "anzahl", "x": 10, "privat": True, "extern": False})
    assert len(ein) == 2


def test_ankuendigung_formatieren_de_en():
    events = [{"start": {"dateTime": "2026-11-02T00:00:00", "timeZone": "UTC"},
               "end": {"dateTime": "2026-11-06T00:00:00", "timeZone": "UTC"}},
              {"start": {"dateTime": "2026-11-20T00:00:00", "timeZone": "UTC"},
               "end": {"dateTime": "2026-11-20T00:00:00", "timeZone": "UTC"}}]
    de = abwesenheit._ankuendigung_formatieren(events, "de")
    en = abwesenheit._ankuendigung_formatieren(events, "en")
    assert de == "Weitere geplante Abwesenheiten: vom 02.11.2026 bis 06.11.2026; am 20.11.2026."
    assert en == "Further planned absences: from 02.11.2026 to 06.11.2026; on 20.11.2026."
    assert abwesenheit._ankuendigung_formatieren([], "de") == ""


def test_vorlage_nutzt_ankuendigung(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    (tmp_path / "Mit.html").write_text("<p>{{ oof.ankuendigung }}</p>", encoding="utf-8")
    (tmp_path / "Ohne.html").write_text("<p>{{ oof.zeitraum }}</p>", encoding="utf-8")
    assert abwesenheit._vorlage_nutzt_ankuendigung("Mit") is True
    assert abwesenheit._vorlage_nutzt_ankuendigung("Ohne") is False
    assert abwesenheit._vorlage_nutzt_ankuendigung("") is False


def test_braucht_kalender_durch_template_gate(tmp_path, monkeypatch):
    """Auch ohne Kalender-Automatik braucht ein Postfach den Kalender, wenn seine
    Vorlage die Ankündigungs-Variable nutzt — und NUR dann."""
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    (tmp_path / "Mit.html").write_text("<p>{{ oof.ankuendigung }}</p>", encoding="utf-8")
    (tmp_path / "Ohne.html").write_text("<p>x</p>", encoding="utf-8")
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: {"OOO_CALENDAR_AUTO": False}.get(k, d))
    assert abwesenheit._braucht_kalender({}, "Mit") is True
    assert abwesenheit._braucht_kalender({}, "Ohne") is False


def test_render_oof_ankuendigungs_variable(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    (tmp_path / "A.html").write_text("<p>Weg. {{ oof.ankuendigung }} [{oof.announcement}]</p>", encoding="utf-8")
    (tmp_path / "A.txt").write_text("{{ oof.ankuendigung }}", encoding="utf-8")
    signature_engine._reload_env()
    html, txt = abwesenheit.render_oof(
        UserData(displayName="E", custom={}), "A", "",
        ankuendigung="Bald weg: am 01.12.2026.", announcement="Soon: on 01.12.2026.")
    assert "Bald weg: am 01.12.2026." in html
    assert "[Soon: on 01.12.2026.]" in html        # Kurzform mit Präfix
    assert txt == "Bald weg: am 01.12.2026."


def test_ankuendigung_nur_intern_divergenz(tmp_path, monkeypatch):
    """Standard (oof_announce_extern=false): die Ankündigung erscheint im INTERNEN
    Text, nicht im externen. Schlägt fehl, wenn beide Fassungen gleich gesetzt
    werden."""
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    (tmp_path / "Firma.html").write_text("<p>Ich bin weg. {{ oof.ankuendigung }}</p>", encoding="utf-8")
    (tmp_path / "Firma.txt").write_text("weg", encoding="utf-8")
    signature_engine._reload_env()

    patched: dict = {}

    async def fake_get(upn, token):
        return "ok", {"status": "alwaysEnabled", "internalReplyMessage": "", "externalReplyMessage": ""}

    async def fake_patch(upn, token, s, html, html_extern=None, status=None, start=None, ende=None):
        patched["intern"] = html
        patched["extern"] = html if html_extern is None else html_extern
        return {"internalReplyMessage": html, "externalReplyMessage": patched["extern"]}

    async def fake_user(upn):
        return UserData(displayName="E", custom={})

    async def fake_events(upn, token):
        return [_ev(3, 6)]

    monkeypatch.setattr(abwesenheit, "_get_setting", fake_get)
    monkeypatch.setattr(abwesenheit, "_patch_setting", fake_patch)
    monkeypatch.setattr(graph_client, "get_user", fake_user)
    monkeypatch.setattr(abwesenheit, "_kalender_oof_events", fake_events)
    monkeypatch.setattr(abwesenheit, "oof_vorlage_fuer", lambda *a: "Firma")
    monkeypatch.setattr(abwesenheit, "_state_speichern", lambda st: None)
    monkeypatch.setattr(settings_store, "get",
                        lambda k, d=None: {"OOO_CALENDAR_AUTO": False,
                                           "OOO_CALENDAR_REFRESH_HOURS": 6,
                                           "OOO_APPEND_SIGNATURE": False,
                                           "OOO_APPEND_BANNER": False}.get(k, d))
    r = _run(abwesenheit.setze_fuer_postfach("a@x.de", "a@x.de", {}, {}, "T", {}))
    assert r == abwesenheit.GESETZT
    assert "Weitere geplante Abwesenheiten" in patched["intern"]
    assert "Weitere geplante Abwesenheiten" not in patched["extern"]


def test_ankuendigung_auch_extern(tmp_path, monkeypatch):
    """Mit oof_announce_extern=true steht die Ankündigung auch im externen Text."""
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    (tmp_path / "Firma.html").write_text("<p>weg {{ oof.ankuendigung }}</p>", encoding="utf-8")
    (tmp_path / "Firma.txt").write_text("weg", encoding="utf-8")
    signature_engine._reload_env()
    patched: dict = {}

    async def fake_get(upn, token):
        return "ok", {"status": "alwaysEnabled", "internalReplyMessage": "", "externalReplyMessage": ""}

    async def fake_patch(upn, token, s, html, html_extern=None, status=None, start=None, ende=None):
        patched["extern"] = html if html_extern is None else html_extern
        return {"internalReplyMessage": html, "externalReplyMessage": patched["extern"]}

    async def fake_user(upn):
        return UserData(displayName="E", custom={})

    async def fake_events(upn, token):
        return [_ev(3, 6)]

    monkeypatch.setattr(abwesenheit, "_get_setting", fake_get)
    monkeypatch.setattr(abwesenheit, "_patch_setting", fake_patch)
    monkeypatch.setattr(graph_client, "get_user", fake_user)
    monkeypatch.setattr(abwesenheit, "_kalender_oof_events", fake_events)
    monkeypatch.setattr(abwesenheit, "oof_vorlage_fuer", lambda *a: "Firma")
    monkeypatch.setattr(abwesenheit, "_state_speichern", lambda st: None)
    monkeypatch.setattr(settings_store, "get",
                        lambda k, d=None: {"OOO_CALENDAR_AUTO": False,
                                           "OOO_CALENDAR_REFRESH_HOURS": 6,
                                           "OOO_APPEND_SIGNATURE": False,
                                           "OOO_APPEND_BANNER": False}.get(k, d))
    _run(abwesenheit.setze_fuer_postfach("a@x.de", "a@x.de", {},
                                         {"oof_announce_extern": True}, "T", {}))
    assert "Weitere geplante Abwesenheiten" in patched["extern"]


def test_ankuendigung_an_false_unterdrueckt_und_spart_read(tmp_path, monkeypatch):
    """`oof_announce=false` schaltet die Ankündigung ab — und dann wird der Kalender
    gar nicht erst gelesen (kein Graph-Aufruf). Default (Feld fehlt) = an."""
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    (tmp_path / "Mit.html").write_text("<p>{{ oof.ankuendigung }}</p>", encoding="utf-8")
    called = {"n": 0}

    async def fake_events(upn, token):
        called["n"] += 1
        return [_ev(3, 6)]

    monkeypatch.setattr(abwesenheit, "_kalender_oof_events", fake_events)
    monkeypatch.setattr(settings_store, "get",
                        lambda k, d=None: {"OOO_CALENDAR_REFRESH_HOURS": 6}.get(k, d))
    de, en = _run(abwesenheit.ankuendigung_fuer_render("a@x.de", "T", "Mit",
                                                       {"oof_announce": False}, {}))
    assert de == "" and en == "" and called["n"] == 0, "aus → leer UND kein Kalender-Read"
    de2, _ = _run(abwesenheit.ankuendigung_fuer_render("a@x.de", "T", "Mit", {}, {}))
    assert "Weitere geplante Abwesenheiten" in de2        # Default an


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
