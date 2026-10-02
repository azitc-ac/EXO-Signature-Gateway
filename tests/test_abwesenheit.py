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

    async def fake_patch(upn, token, s, html, status=None, start=None, ende=None):
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

    async def fake_patch(upn, token, s, html, status=None, start=None, ende=None):
        patch_calls.append({"status": status, "start": start, "ende": ende})
        return {"internalReplyMessage": html, "externalReplyMessage": html}

    async def fake_user(upn):
        return UserData(displayName="E", custom={})

    async def fake_fenster(upn, token):
        return ({"dateTime": "2026-10-01T00:00:00", "timeZone": "UTC"},
                {"dateTime": "2026-10-10T00:00:00", "timeZone": "UTC"})

    monkeypatch.setattr(abwesenheit, "_get_setting", fake_get)
    monkeypatch.setattr(abwesenheit, "_patch_setting", fake_patch)
    monkeypatch.setattr(graph_client, "get_user", fake_user)
    monkeypatch.setattr(abwesenheit, "_kalender_oof_fenster", fake_fenster)
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

    async def fake_patch(upn, token, s, html, status=None, start=None, ende=None):
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
