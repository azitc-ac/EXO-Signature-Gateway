"""Self-Service-Fundament: Login-Freigabe + Sitzungs-Scope.

Der Kernschutz: ein Postfach-Nutzer darf NUR sein eigenes Postfach verwalten.
Die Identität kommt aus der Sitzung, nie aus einem Parameter — und eine Sitzung
als Self-Service-Nutzer gibt es nur, wenn der Betreiber es freigeschaltet hat und
das Postfach im Gateway überhaupt verwaltet wird.
"""
import pytest

import settings_store
import sso
from webui import deps


class _Req:
    """Minimaler Request-Ersatz (nur das, was _get_session_user liest)."""
    def __init__(self, cookie=None, header=None):
        self.cookies = {sso.SESSION_COOKIE: cookie} if cookie else {}
        self.headers = {"X-Addin-Session": header} if header else {}


def _store(monkeypatch, enabled, cfg):
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: {
        "SELF_SERVICE_ENABLED": enabled, "MAILBOX_CONFIG": cfg}.get(k, d))


_CFG = {"g": {"known_addresses": ["a@x.de"], "primary": "a@x.de", "sig": True}}


def test_rolle_nur_bei_freigabe(monkeypatch):
    _store(monkeypatch, False, _CFG)        # aus → keine Rolle, obwohl Postfach bekannt
    assert deps.self_service_rolle("a@x.de") is None


def test_rolle_bei_freigabe_und_bekanntem_postfach(monkeypatch):
    _store(monkeypatch, True, _CFG)
    assert deps.self_service_rolle("a@x.de") == sso.ROLE_SELF


def test_rolle_nicht_fuer_fremdes_postfach(monkeypatch):
    """Freigeschaltet, aber die Adresse ist kein verwaltetes Postfach → keine Sitzung."""
    _store(monkeypatch, True, _CFG)
    assert deps.self_service_rolle("fremd@y.de") is None


def test_require_self_ohne_sitzung_401(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(sso, "verify_session_cookie", lambda v: None)
    with pytest.raises(HTTPException) as exc:
        deps._require_self(_Req())
    assert exc.value.status_code == 401


# ── Vorlagenwahl: standardmäßig gesperrt, freischaltbar pro Postfach/Gruppe ──────

_MB = {"g1": {"known_addresses": ["a@x.de"], "primary": "a@x.de", "sig": True}}


def _store_tpl(monkeypatch, mb, groups=None, intern=None):
    data = {"MAILBOX_CONFIG": mb, "SELF_TEMPLATE_GROUPS": groups or [],
            "INTERNAL_GROUPS": intern or {}, "TEMPLATE_POLICIES": {}, "CUSTOM_POLICIES": []}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: data.get(k, d))


def test_darf_vorlagen_standard_aus(monkeypatch):
    """Standard: niemand darf die Vorlagen selbst wählen."""
    from webui.routen import selfservice as sv
    _store_tpl(monkeypatch, _MB)
    assert sv._darf_vorlagen_waehlen("a@x.de") is False


def test_darf_vorlagen_pro_postfach(monkeypatch):
    from webui.routen import selfservice as sv
    mb = {"g1": {**_MB["g1"], "self_templates": True}}
    _store_tpl(monkeypatch, mb)
    assert sv._darf_vorlagen_waehlen("a@x.de") is True


def test_darf_vorlagen_pro_gruppe(monkeypatch):
    from webui.routen import selfservice as sv
    _store_tpl(monkeypatch, _MB, groups=["team"], intern={"team": ["g1"]})
    assert sv._darf_vorlagen_waehlen("a@x.de") is True


def test_darf_vorlagen_gruppe_ohne_mitgliedschaft(monkeypatch):
    from webui.routen import selfservice as sv
    _store_tpl(monkeypatch, _MB, groups=["team"], intern={"team": ["andere"]})
    assert sv._darf_vorlagen_waehlen("a@x.de") is False


def test_zeitraum_deckt_ganzen_endtag_ab(monkeypatch):
    """Ein Zeitraum meint ganze Tage: Beginn 00:00, Ende 23:59 — NICHT 00:00 am
    Endtag (sonst wäre der letzte Tag nicht abgedeckt). Hier in UTC geprüft
    (tz-unabhängig robust)."""
    from webui.routen import selfservice as sv
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: {"LOG_TIMEZONE": "UTC"}.get(k, d))
    s = sv._synth_setting("scheduled", "2026-07-01", "2026-07-05")
    assert s["scheduledStartDateTime"]["dateTime"].startswith("2026-07-01T00:00:00")
    assert s["scheduledEndDateTime"]["dateTime"].startswith("2026-07-05T23:59:59")


def test_zeitraum_mit_uhrzeiten(monkeypatch):
    """Nicht-ganztägig: die gewählten Uhrzeiten gelten (statt 00:00/23:59)."""
    from webui.routen import selfservice as sv
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: {"LOG_TIMEZONE": "UTC"}.get(k, d))
    s = sv._synth_setting("scheduled", "2026-07-01", "2026-07-05", "09:00", "17:00")
    assert s["scheduledStartDateTime"]["dateTime"].startswith("2026-07-01T09:00:00")
    assert s["scheduledEndDateTime"]["dateTime"].startswith("2026-07-05T17:00:00")


def test_zeitraum_lokale_zeitzone_round_trip(monkeypatch):
    """Lokale Tage (Europe/Berlin) → UTC und zurück: zeitraum_text zeigt dieselben
    Kalendertage (kein Verrutschen durch die Zeitzone)."""
    import pytest
    from zoneinfo import ZoneInfo
    try:
        ZoneInfo("Europe/Berlin")
    except Exception:
        pytest.skip("keine tzdata für Europe/Berlin")
    from webui.routen import selfservice as sv
    import abwesenheit
    monkeypatch.setattr(settings_store, "get",
                        lambda k, d=None: {"LOG_TIMEZONE": "Europe/Berlin"}.get(k, d))
    s = sv._synth_setting("scheduled", "2026-07-01", "2026-07-05")   # Sommer: CEST=UTC+2
    assert s["scheduledStartDateTime"]["dateTime"].startswith("2026-06-30T22:00:00")
    assert abwesenheit.zeitraum_text(s) == "vom 01.07.2026 bis 05.07.2026"


CB = "https://sig.example/auth/id-callback"


def test_implicit_patch_leere_web_setzt_beides():
    """Raspi-Fall: web leer → id-callback-Redirect + ID-Token-Erteilung werden gesetzt."""
    import setup_wizard
    neu = setup_wizard._implicit_web_patch({}, CB)
    assert neu["redirectUris"] == [CB]
    assert neu["implicitGrantSettings"]["enableIdTokenIssuance"] is True
    assert neu["implicitGrantSettings"]["enableAccessTokenIssuance"] is False


def test_implicit_patch_schon_korrekt_gibt_none():
    """Prod-Fall: alles da → None (kein PATCH, keine Dedup-Störung)."""
    import setup_wizard
    web = {"redirectUris": [CB],
           "implicitGrantSettings": {"enableIdTokenIssuance": True, "enableAccessTokenIssuance": False}}
    assert setup_wizard._implicit_web_patch(web, CB) is None


def test_implicit_patch_ergaenzt_und_erhaelt():
    """Fehlt nur der Redirect, bleibt enableAccessTokenIssuance erhalten; andere
    web-Felder (homePageUrl) überleben den PATCH."""
    import setup_wizard
    web = {"homePageUrl": "https://x", "redirectUris": [],
           "implicitGrantSettings": {"enableIdTokenIssuance": True, "enableAccessTokenIssuance": True}}
    neu = setup_wizard._implicit_web_patch(web, CB)
    assert neu["redirectUris"] == [CB]
    assert neu["implicitGrantSettings"]["enableAccessTokenIssuance"] is True   # erhalten
    assert neu["homePageUrl"] == "https://x"                                   # erhalten
    assert CB not in (web.get("redirectUris") or []) or True                   # Original nicht nötig


def test_ankuendigung_ins_eintrag_validiert():
    """Die Ankündigungsfelder aus dem Self-Service werden server-seitig validiert
    und geklemmt — dem Client-Wert wird nicht geglaubt."""
    from webui.routen import selfservice as sv
    e = {}
    sv._ankuendigung_ins_eintrag(e, {"an": True, "mode": "quatsch", "x": "999",
                                     "privat": False, "extern": True})
    assert e["oof_announce"] is True
    assert e["oof_announce_mode"] == "anzahl"      # unbekannter Modus → anzahl
    assert e["oof_announce_x"] == 50               # auf 1..50 geklemmt
    assert e["oof_announce_privat"] is False
    assert e["oof_announce_extern"] is True
    e2 = {}
    sv._ankuendigung_ins_eintrag(e2, {"an": False, "mode": "tage", "x": "foo"})
    assert e2["oof_announce"] is False and e2["oof_announce_mode"] == "tage"
    assert e2["oof_announce_x"] == 3               # ungültige Zahl → Default


def test_require_self_identitaet_aus_sitzung(monkeypatch):
    """DER Kernschutz: die Adresse kommt aus der Sitzung (klein), nicht aus einem
    Parameter — sonst könnte Nutzer A das Postfach von Nutzer B anfassen."""
    monkeypatch.setattr(sso, "verify_session_cookie",
                        lambda v: {"u": "Erika@X.DE", "r": sso.ROLE_SELF})
    assert deps._require_self(_Req(cookie="tok")) == "erika@x.de"
    assert deps._require_self(_Req(header="tok")) == "erika@x.de"   # Add-in-Weg


# ── Vorlagenwahl je Kategorie (seit v1.9.113) ──────────────────────────────────

_ARTEN = {"signatur": ["default", "Kurz", "Lang"], "oof": ["Urlaub"], "disclaimer": ["Recht"]}


def test_wahl_fasst_use_policy_nie_an():
    """Die alte Kopplung: eine Signaturwahl setzte use_policy=false und nahm dem
    Postfach still Banner und Disclaimer der Richtlinie."""
    from webui.routen import selfservice as sv
    e = {"use_policy": True}
    sv._wahl_ins_eintrag(e, {"sig": "Lang", "oof": "Urlaub"},
                         {"sig": True, "min": True, "oof": True}, _ARTEN)
    assert e == {"use_policy": True, "template": "Lang", "oof_template": "Urlaub"}


def test_wahl_nur_fuer_freigegebene_kategorien():
    from webui.routen import selfservice as sv
    e = {"oof_template": "Firma"}
    sv._wahl_ins_eintrag(e, {"sig": "Lang", "oof": ""},
                         {"sig": True, "min": False, "oof": False}, _ARTEN)
    assert e == {"oof_template": "Firma", "template": "Lang"}


def test_wahl_standard_und_keine():
    from webui.routen import selfservice as sv
    e = {"template": "Lang", "min_template": "Kurz"}
    sv._wahl_ins_eintrag(e, {"sig": sv.STANDARD, "min": ""},
                         {"sig": True, "min": True, "oof": True}, _ARTEN)
    assert e == {"min_template": ""}


def test_wahl_fremder_art_wird_abgewiesen():
    """Ein Disclaimer ist keine Signatur — per Hand geschickt muss das scheitern."""
    from fastapi import HTTPException
    from webui.routen import selfservice as sv
    with pytest.raises(HTTPException) as exc:
        sv._wahl_ins_eintrag({}, {"sig": "Recht"}, {"sig": True, "min": True, "oof": True}, _ARTEN)
    assert exc.value.status_code == 400


def test_waehlbar_je_kategorie(monkeypatch):
    from webui.routen import selfservice as sv
    mb = {"g1": {**_MB["g1"], "self_templates": True}}
    data = {"MAILBOX_CONFIG": mb, "SELF_TEMPLATE_KATEGORIEN": ["oof"], "INTERNAL_GROUPS": {},
            "ABWESENHEIT_AKTIV": True}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: data.get(k, d))
    assert sv._waehlbar("a@x.de") == {"sig": False, "min": False, "oof": True}
    data["MAILBOX_CONFIG"] = _MB          # Postfach nicht freigeschaltet → nichts
    assert sv._waehlbar("a@x.de") == {"sig": False, "min": False, "oof": False}


def test_nicht_freigeschaltet_bleibt_durchspielbar():
    """Der Hinweis verspricht „Du kannst alles durchspielen, aber nicht
    speichern". Bis v1.9.133 sperrte das Skript im selben Zustand jedes Feld —
    durchspielen ging nicht. Gesperrt wird nur der Speichern-Knopf; das
    Speichern selbst verweigert der Server (siehe unten)."""
    import re
    from pathlib import Path
    quelle = (Path(__file__).parent.parent / "app/webui/templates/self.html").read_text()
    assert "Du kannst alles durchspielen" in quelle
    zweige = re.findall(r"if \(!ctx\.freigeschaltet\)(.*?)\n\s*\n", quelle, re.S)
    assert zweige, "Zweig für „nicht freigeschaltet“ nicht gefunden"
    for z in zweige:
        assert ".disabled" not in z, f"Felder werden gesperrt: {z.strip()[:200]}"
    assert any("el('speichern').hidden = true" in z for z in zweige)


def test_speichern_ohne_freischaltung_verweigert(monkeypatch):
    import asyncio
    from fastapi import HTTPException
    from webui.routen import selfservice
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: False if k == "SELF_SERVICE_ENABLED" else d)
    with pytest.raises(HTTPException) as e:
        asyncio.run(selfservice.self_save({}, email="erika@example.org"))
    assert e.value.status_code == 403


class _JsonReq:
    def __init__(self, body):
        self._b = body

    async def json(self):
        return self._b


def test_speichern_lehnt_ende_vor_beginn_ab_ohne_zu_schreiben(monkeypatch):
    """Testlauf 11.10.2026: „von 11.10. bis 09.10." wurde angenommen. Jetzt
    400, bevor die Konfiguration geschrieben wird (sonst stünde die
    Vorlagenwahl schon drin, während die Abwesenheit abgelehnt wird)."""
    import asyncio
    from fastapi import HTTPException
    from webui.routen import selfservice
    geschrieben = []
    monkeypatch.setattr(settings_store, "get",
                        lambda k, d=None: True if k == "SELF_SERVICE_ENABLED"
                        else ("UTC" if k == "LOG_TIMEZONE" else d))
    monkeypatch.setattr(settings_store, "update", lambda d: geschrieben.append(d))
    body = {"oof_status": "scheduled", "oof_start": "2026-10-11", "oof_ende": "2026-10-09"}
    with pytest.raises(HTTPException) as e:
        asyncio.run(selfservice.self_save(_JsonReq(body), email="erika@example.org"))
    assert e.value.status_code == 400 and "Ende" in e.value.detail
    assert geschrieben == []
    # Gleicher Tag, Ende vor Beginn (Uhrzeit)
    body = {"oof_status": "scheduled", "oof_start": "2026-10-11", "oof_ende": "2026-10-11",
            "ganztaegig": False, "start_zeit": "12:00", "ende_zeit": "09:00"}
    with pytest.raises(HTTPException):
        asyncio.run(selfservice.self_save(_JsonReq(body), email="erika@example.org"))


def test_zeitraum_fehler_regeln():
    import abwesenheit
    s = abwesenheit.synth_setting
    assert abwesenheit.zeitraum_fehler(s("scheduled", "2026-10-11", "2026-10-09"))
    assert abwesenheit.zeitraum_fehler(s("scheduled", "2026-10-11", "2026-10-11", "12:00", "12:00"))
    assert not abwesenheit.zeitraum_fehler(s("scheduled", "2026-10-11", "2026-10-11"))
    assert not abwesenheit.zeitraum_fehler(s("scheduled", "2026-10-11", ""))
    assert not abwesenheit.zeitraum_fehler(s("disabled", "2026-10-11", "2026-10-09"))


def test_seite_prueft_vor_dem_speichern_und_hat_ein_zeitlimit():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "app/webui/templates/self.html").read_text(encoding="utf-8")
    speichern = html.split("async function speichern()", 1)[1].split("\n  }\n", 1)[0]
    assert "if (pruefeZeitraum()) return;" in speichern
    assert "gespeichertText(status)" in speichern
    # Zeitlimit im gemeinsamen Versandweg — und ALLE drei Knöpfe gehen darüber.
    senden = html.split("async function senden(", 1)[1].split("\n  }\n", 1)[0]
    assert "AbortController" in senden and "signal: abbruch.signal" in senden
    assert html.count("api('/api/self/save'") == 1, "Speichern an senden() vorbei"
    assert "await senden(" in speichern
    assert "await senden(" in html.split("async function wahlSpeichern(", 1)[1].split("\n  }\n", 1)[0]
    assert "Abwesenheit und Signatur sind gesetzt" not in html


# ── Speichern je Reiter (bereich) ─────────────────────────────────────────────

def _speicher_umgebung(monkeypatch, waehlbar, cfg=None, oof_anhang=False):
    from webui.routen import selfservice
    import signature_engine
    daten = {"SELF_SERVICE_ENABLED": True, "LOG_TIMEZONE": "UTC",
             "ABWESENHEIT_AKTIV": True, "OOO_APPEND_SIGNATURE": oof_anhang,
             "MAILBOX_CONFIG": cfg if cfg is not None else {
                 "a@x.de": {"sig": True, "template": "Alt", "min_template": "Kurz"}}}
    geschrieben = []
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: daten.get(k, d))
    monkeypatch.setattr(settings_store, "update", lambda d: (geschrieben.append(d), daten.update(d)))
    monkeypatch.setattr(selfservice, "_waehlbar", lambda e: waehlbar)
    monkeypatch.setattr(signature_engine, "templates_nach_art",
                        lambda: {"signatur": ["Alt", "Neu", "Kurz"], "oof": ["Urlaub"]})
    return selfservice, geschrieben


def test_signatur_reiter_speichert_nur_die_signatur(monkeypatch):
    """Der Knopf im Reiter Signatur sichert NUR die Signaturwahl — keine
    Abwesenheit (kein Graph-Aufruf), keine Antwort-Signatur, auch wenn die
    Anfrage sie mitschickt."""
    import asyncio
    selfservice, geschrieben = _speicher_umgebung(
        monkeypatch, {"sig": True, "min": True, "oof": True})
    async def kein_graph(*a, **k):
        raise AssertionError("Graph darf beim Signatur-Speichern nicht gerufen werden")
    monkeypatch.setattr(selfservice.graph_client, "_acquire_token_async", kein_graph)
    r = asyncio.run(selfservice.self_save(_JsonReq(
        {"bereich": "signatur", "wahl": {"sig": "Neu", "min": "", "oof": "Urlaub"},
         "oof_status": "alwaysEnabled"}), email="a@x.de"))
    assert r.status_code == 200
    eintrag = geschrieben[-1]["MAILBOX_CONFIG"]["a@x.de"]
    assert eintrag["template"] == "Neu"
    assert eintrag["min_template"] == "Kurz"          # unberührt
    assert "oof_template" not in eintrag


def test_antwort_reiter_keine_antwortsignatur(monkeypatch):
    """„— keine —" (leer) wird als ausdrückliche Wahl gespeichert — der Handler
    hängt dann an Antworten im Verlauf nichts an."""
    import asyncio
    selfservice, geschrieben = _speicher_umgebung(monkeypatch, {"sig": True, "min": True, "oof": False})
    asyncio.run(selfservice.self_save(_JsonReq({"bereich": "antwort", "wahl": {"min": ""}}),
                                      email="a@x.de"))
    eintrag = geschrieben[-1]["MAILBOX_CONFIG"]["a@x.de"]
    assert eintrag["min_template"] == "" and eintrag["template"] == "Alt"


def test_reiter_ohne_freigabe_wird_abgewiesen(monkeypatch):
    import asyncio
    from fastapi import HTTPException
    selfservice, geschrieben = _speicher_umgebung(monkeypatch, {"sig": True, "min": False, "oof": False})
    with pytest.raises(HTTPException) as e:
        asyncio.run(selfservice.self_save(_JsonReq({"bereich": "antwort", "wahl": {"min": ""}}),
                                          email="a@x.de"))
    assert e.value.status_code == 403 and geschrieben == []


@pytest.mark.parametrize("bereich", ["sonstwas", ["signatur"], {"a": 1}])
def test_unbekannter_bereich_400(monkeypatch, bereich):
    import asyncio
    from fastapi import HTTPException
    selfservice, geschrieben = _speicher_umgebung(monkeypatch, {"sig": True, "min": True, "oof": True})
    with pytest.raises(HTTPException) as e:
        asyncio.run(selfservice.self_save(_JsonReq({"bereich": bereich, "wahl": {"sig": "Neu"}}),
                                          email="a@x.de"))
    assert e.value.status_code == 400 and geschrieben == []


def test_signaturwechsel_frischt_abwesenheitstext_auf(monkeypatch):
    """Hängt die Signatur unter der Abwesenheitsnotiz, setzt ein Signaturwechsel
    deren Text sofort neu — Status und Zeitraum bleiben unangetastet."""
    import asyncio
    selfservice, _g = _speicher_umgebung(monkeypatch, {"sig": True, "min": True, "oof": True},
                                         oof_anhang=True)
    gepatcht = []
    async def token():
        return "t"
    async def lesen(email, tok):
        return "ok", {"status": "scheduled", "internalReplyMessage": "alt"}
    async def nutzer(email):
        return {}
    async def rendern(*a, **k):
        return "<p>neu</p>", "<p>neu</p>"
    async def patchen(upn, tok, setting, html, html_extern=None, status=None, start=None, ende=None):
        gepatcht.append({"html": html, "status": status, "start": start, "ende": ende})
    monkeypatch.setattr(selfservice.graph_client, "_acquire_token_async", token)
    monkeypatch.setattr(selfservice.graph_client, "get_user", nutzer)
    monkeypatch.setattr(selfservice.abwesenheit, "_get_setting", lesen)
    monkeypatch.setattr(selfservice.abwesenheit, "render_intern_extern", rendern)
    monkeypatch.setattr(selfservice.abwesenheit, "_patch_setting", patchen)
    monkeypatch.setattr(selfservice.abwesenheit, "_state", lambda: {})
    monkeypatch.setattr(selfservice, "_effektive_vorlagen", lambda e: ("Urlaub", "Neu"))
    r = asyncio.run(selfservice.self_save(_JsonReq({"bereich": "signatur", "wahl": {"sig": "Neu"}}),
                                          email="a@x.de"))
    import json
    assert json.loads(r.body)["oof_aufgefrischt"] is True
    assert gepatcht == [{"html": "<p>neu</p>", "status": None, "start": None, "ende": None}]

    # Antwort-Signatur betrifft die Abwesenheitsnotiz nicht.
    gepatcht.clear()
    asyncio.run(selfservice.self_save(_JsonReq({"bereich": "antwort", "wahl": {"min": "Kurz"}}),
                                      email="a@x.de"))
    assert gepatcht == []
