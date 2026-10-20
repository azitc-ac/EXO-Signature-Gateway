"""Hauptschalter OOO_CALENDAR_READ: Kalender gar nicht lesen.

Vor v1.10.1 war `OOO_CALENDAR_AUTO` nur eine Vorgabe: Ein Postfach mit eigenem
„Kalender: an“ las trotzdem, die Ankündigungs-Variable las unabhängig davon,
und `Calendars.Read` (Lesezugriff auf ALLE Kalender des Tenants) wurde immer
beantragt. Der Hauptschalter schließt alle drei Wege und die Berechtigung.
"""
from __future__ import annotations

import asyncio

import pytest

import abwesenheit
import berechtigungen
import config
import graph_client
import settings_store
import setup_wizard
import signature_engine
from graph_client import UserData

CAL_ID = "798ee544-9d2d-430c-a058-570e29e34338"     # Calendars.Read (Graph-App-Rolle)


def _run(c):
    return asyncio.run(c)


def _werte(monkeypatch, **w):
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: w.get(k, d))


@pytest.fixture
def reads(monkeypatch):
    n = []

    async def fake_events(upn, token):
        n.append(upn)
        return [{"start": {"dateTime": "2099-10-01T00:00:00", "timeZone": "UTC"},
                 "end": {"dateTime": "2099-10-10T00:00:00", "timeZone": "UTC"}, "privat": False}]
    monkeypatch.setattr(abwesenheit, "_kalender_oof_events", fake_events)
    return n


def test_vorgabe_ist_an(monkeypatch):
    """Bestand ohne den Schlüssel liest weiter wie bisher."""
    _werte(monkeypatch)
    assert abwesenheit.kalender_lesen() is True
    assert settings_store.DEFAULTS["OOO_CALENDAR_READ"] is True


def test_aus_schlaegt_opt_in_je_postfach(monkeypatch):
    _werte(monkeypatch, OOO_CALENDAR_READ=False, OOO_CALENDAR_AUTO=True)
    assert abwesenheit._kalender_auto_an({"ooo_calendar": True}) is False


def test_aus_kein_read_und_kein_cache(monkeypatch, reads):
    _werte(monkeypatch, OOO_CALENDAR_READ=False)
    state: dict = {}
    assert _run(abwesenheit.kalender_events("a@x.de", "T", state, force=True)) == []
    assert reads == [] and "kal" not in state.get("a@x.de", {})


def test_aus_keine_ankuendigung_und_kein_read(monkeypatch, reads, tmp_path):
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    (tmp_path / "Mit.html").write_text("<p>{{ oof.ankuendigung }}</p>", encoding="utf-8")
    _werte(monkeypatch, OOO_CALENDAR_READ=False)
    assert _run(abwesenheit.ankuendigung_fuer_render("a@x.de", "T", "Mit", {}, {})) == ("", "")
    assert reads == []
    assert abwesenheit.ankuendigung_moeglich("Mit") is False      # Oberfläche bietet sie nicht an
    assert abwesenheit._braucht_kalender({"ooo_calendar": True}, "Mit") is False
    _werte(monkeypatch)                                              # Gegenprobe: an
    assert abwesenheit.ankuendigung_moeglich("Mit") is True


def test_abgleich_verwirft_gecachte_termine(monkeypatch, reads):
    """Termindaten aus der Zeit vor dem Ausschalten bleiben nicht im State liegen."""
    async def fake_get(upn, token):
        return "ok", {"status": "disabled", "internalReplyMessage": "", "externalReplyMessage": ""}

    async def fake_patch(upn, token, s, html, html_extern=None, status=None, start=None, ende=None):
        return {"internalReplyMessage": html, "externalReplyMessage": html}

    async def fake_user(upn):
        return UserData(displayName="E", custom={})
    monkeypatch.setattr(abwesenheit, "_get_setting", fake_get)
    monkeypatch.setattr(abwesenheit, "_patch_setting", fake_patch)
    monkeypatch.setattr(graph_client, "get_user", fake_user)
    monkeypatch.setattr(abwesenheit, "oof_vorlage_fuer", lambda *a: "Firma")
    monkeypatch.setattr(signature_engine, "render", lambda u, template_name=None, extra=None: ("<p>OOF</p>", "OOF"))
    monkeypatch.setattr(abwesenheit, "_state_speichern", lambda st: None)
    alt = {"ts": "2099-01-01T00:00:00+00:00", "events": [{"geheim": 1}], "v": abwesenheit._KAL_STAND}

    _werte(monkeypatch, OOO_CALENDAR_READ=False, OOO_CALENDAR_AUTO=True)
    state = {"a@x.de": {"kal": dict(alt)}}
    _run(abwesenheit.setze_fuer_postfach("a@x.de", "a@x.de", {}, {}, "T", state))
    assert "kal" not in state["a@x.de"] and reads == []

    _werte(monkeypatch, OOO_CALENDAR_AUTO=False)                     # Gegenprobe: an → bleibt
    state = {"a@x.de": {"kal": dict(alt)}}
    _run(abwesenheit.setze_fuer_postfach("a@x.de", "a@x.de", {}, {}, "T", state))
    assert state["a@x.de"]["kal"]["events"] == [{"geheim": 1}]


@pytest.mark.parametrize("lesen,erwartet", [(False, False), (True, True), (None, True)])
def test_calendars_read_nur_beantragt_wenn_gelesen_wird(monkeypatch, lesen, erwartet):
    """Antrag (requiredResourceAccess) UND Erteilung folgen dem Schalter."""
    aufrufe = []

    async def gh(methode, url, token, json=None):
        aufrufe.append((methode, url, json))
        if methode == "get":
            return {"value": [{"id": "x"}]}
        return {}
    monkeypatch.setattr(setup_wizard, "_gh", gh)
    monkeypatch.setattr(setup_wizard._gc, "reset_msal_app", lambda: None)
    _werte(monkeypatch, CLIENT_ID="cid", OOO_CALENDAR_READ=lesen)
    _run(setup_wizard.berechtigungen_nachziehen("tok"))
    beantragt = [r["id"] for m, u, j in aufrufe if m == "patch"
                 for res in j["requiredResourceAccess"] for r in res["resourceAccess"]]
    erteilt = [j["appRoleId"] for m, u, j in aufrufe if m == "post"]
    assert (CAL_ID in beantragt) is erwartet and (CAL_ID in erteilt) is erwartet
    assert "6931bccd-447a-43d1-b442-00a195474933" in erteilt        # MailboxSettings bleibt


def test_status_meldet_nicht_benoetigte_rolle(monkeypatch):
    monkeypatch.setattr(berechtigungen, "graph_rollen", lambda: {"Calendars.Read"})
    monkeypatch.setattr(berechtigungen, "exo_rollen", lambda: set())
    _werte(monkeypatch, OOO_CALENDAR_READ=False)
    st = {b["name"]: b for b in berechtigungen.status()}
    assert st["Calendars.Read"]["benoetigt"] is False and st["Calendars.Read"]["erteilt"] is True
    assert st["Mail.Send"]["benoetigt"] is True
    _werte(monkeypatch)
    assert {b["name"]: b for b in berechtigungen.status()}["Calendars.Read"]["benoetigt"] is True


def test_berechtigungsliste_zaehlt_nicht_benoetigte_nicht_als_fehlend():
    """Sonst bliebe „Fehlende Berechtigungen erteilen“ sichtbar, obwohl der Knopf
    die Rolle gar nicht mehr beantragt."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "app" / "webui" / "templates"
           / "_berechtigungen.html").read_text(encoding="utf-8")
    assert "if (b.erteilt === false && b.benoetigt !== false) fehlt++;" in src


def test_nur_abwesenheit_fragt_die_vorlage_ohne_hauptschalter():
    """Außerhalb von abwesenheit.py nur `ankuendigung_moeglich` — sonst böte eine
    Oberfläche (Self-Service, Abwesenheits-Dashboard) die Ankündigung an, obwohl
    der Kalender nicht gelesen wird und sie nie erscheint."""
    from pathlib import Path
    app = Path(__file__).resolve().parent.parent / "app"
    treffer = [str(p.relative_to(app)) for p in app.rglob("*.py")
               if p.name != "abwesenheit.py" or p.parent != app
               if "_vorlage_nutzt_ankuendigung" in p.read_text(encoding="utf-8")]
    assert not treffer, treffer
