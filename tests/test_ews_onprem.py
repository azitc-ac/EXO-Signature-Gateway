"""Anbindung an Exchange lokal (EWS) — Abwesenheit für Hybrid-Nutzer.

Was hier gesichert wird:

* XML wird mit xml.etree gebaut und gelesen. Ein Probelauf, der Antworttexte
  per Regex aus dem XML schnitt und wieder einsetzte, schrieb wörtlich
  „<Message/>“ in die Abwesenheitsnotiz. Deshalb: Sonderzeichen, HTML, leere
  Nachricht und der Rundlauf lesen → setzen → lesen.
* Fehlercodes (Impersonation verweigert, 401, kaputte Antwort) werden
  verständlich übersetzt.
* Ohne Konfiguration kein Netzaufruf; ein EXO-Postfach geht NIE über EWS;
  ein Hybrid-Nutzer ohne Anbindung bleibt wie bisher außen vor.
* Das Kennwort ist ein Geheimnis (maskiert, nicht exportiert).

Testdaten erfunden (firma.at, 10.99.0.x).
"""
import asyncio
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import abwesenheit  # noqa: E402
import ews_onprem  # noqa: E402
import exo_mailboxes  # noqa: E402
import settings_store  # noqa: E402

NS = {"s": ews_onprem.NS_SOAP, "t": ews_onprem.NS_T, "m": ews_onprem.NS_M}
HYBRID = "max.mustermann@firma.at"
CLOUD = "erika.musterfrau@firma.at"
KONFIG = {"EWS_ONPREM_URL": "https://exchange.firma.at/EWS/Exchange.asmx",
          "EWS_ONPREM_KONTO": "FIRMA\\svc-gateway",
          "EWS_ONPREM_KENNWORT": "nur-ein-testwert"}
SONDER = 'Grüße & <Test> "zitiert" \'einfach\' — ÄÖÜß €'
HTML = '<html><body><p style="color:#333">Bin weg &amp; zurück am <b>Montag</b></p></body></html>'

# Wörtliche Struktur der Antwort einer Exchange SE (15.2) auf GetUserOofSettings —
# Standard-Namensräume statt Präfixe, leere Nachricht als <Message/>.
LAB_ANTWORT = (
    '<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
    '<s:Header><h:ServerVersionInfo MajorVersion="15" MinorVersion="2" '
    'xmlns:h="http://schemas.microsoft.com/exchange/services/2006/types"/></s:Header>'
    '<s:Body><GetUserOofSettingsResponse xmlns="http://schemas.microsoft.com/exchange/services/2006/messages">'
    '<ResponseMessage ResponseClass="Success"><ResponseCode>NoError</ResponseCode></ResponseMessage>'
    '<OofSettings xmlns="http://schemas.microsoft.com/exchange/services/2006/types">'
    '<OofState>Disabled</OofState><ExternalAudience>All</ExternalAudience>'
    '<Duration><StartTime>{start}</StartTime><EndTime>{ende}</EndTime></Duration>'
    '<InternalReply><Message/></InternalReply><ExternalReply><Message/></ExternalReply>'
    '</OofSettings><AllowExternalOof>All</AllowExternalOof></GetUserOofSettingsResponse>'
    '</s:Body></s:Envelope>')

# Wörtlich (bis auf nichts) die Antwort für ein Postfach außerhalb des Management Scope.
LAB_VERWEIGERT = (
    '<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
    '<s:Body><s:Fault><faultcode xmlns:a="http://schemas.microsoft.com/exchange/services/2006/types">'
    'a:ErrorImpersonateUserDenied</faultcode><faultstring xml:lang="en-US">The account does not have '
    'permission to impersonate the requested user.</faultstring><detail><e:ResponseCode '
    'xmlns:e="http://schemas.microsoft.com/exchange/services/2006/errors">ErrorImpersonateUserDenied'
    '</e:ResponseCode><e:Message xmlns:e="http://schemas.microsoft.com/exchange/services/2006/errors">'
    'The account does not have permission to impersonate the requested user.</e:Message></detail>'
    '</s:Fault></s:Body></s:Envelope>')


def _zeit(tage: int) -> str:
    t = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    return (t + timedelta(days=tage)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FalscheExchange:
    """Ein EWS-Server im Speicher: SetUserOofSettings legt ab, GetUserOofSettings
    antwortet mit dem Abgelegten — beides über xml.etree, wie es ein echter
    Server täte (Maskierung im Rumpf, Standard-Namensräume)."""

    def __init__(self):
        self.stand = {"OofState": "Disabled", "ExternalAudience": "All",
                      "StartTime": _zeit(0), "EndTime": _zeit(1), "intern": "", "extern": ""}
        self.anfragen: list[bytes] = []
        self.verweigert: set[str] = set()

    def __call__(self, ziel, umschlag):
        self.anfragen.append(umschlag)
        if ziel in self.verweigert:
            return 500, LAB_VERWEIGERT.encode()
        root = ET.fromstring(umschlag)
        assert root.find("s:Header/t:ExchangeImpersonation/t:ConnectingSID/t:PrimarySmtpAddress",
                         NS).text == ziel
        setzen = root.find(".//m:SetUserOofSettingsRequest", NS)
        if setzen is not None:
            s = setzen.find("t:UserOofSettings", NS)
            self.stand["OofState"] = s.find("t:OofState", NS).text
            self.stand["ExternalAudience"] = s.find("t:ExternalAudience", NS).text
            if s.find("t:Duration", NS) is not None:
                self.stand["StartTime"] = s.find("t:Duration/t:StartTime", NS).text
                self.stand["EndTime"] = s.find("t:Duration/t:EndTime", NS).text
            self.stand["intern"] = s.find("t:InternalReply/t:Message", NS).text or ""
            self.stand["extern"] = s.find("t:ExternalReply/t:Message", NS).text or ""
            return 200, self._antwort("SetUserOofSettingsResponse", mit_oof=False)
        return 200, self._antwort("GetUserOofSettingsResponse", mit_oof=True)

    def _antwort(self, name, mit_oof):
        env = ET.Element(f"{{{ews_onprem.NS_SOAP}}}Envelope")
        body = ET.SubElement(env, f"{{{ews_onprem.NS_SOAP}}}Body")
        r = ET.SubElement(body, f"{{{ews_onprem.NS_M}}}{name}")
        rm = ET.SubElement(r, f"{{{ews_onprem.NS_M}}}ResponseMessage", {"ResponseClass": "Success"})
        ET.SubElement(rm, f"{{{ews_onprem.NS_M}}}ResponseCode").text = "NoError"
        if mit_oof:
            t = lambda n: f"{{{ews_onprem.NS_T}}}{n}"  # noqa: E731
            o = ET.SubElement(r, t("OofSettings"))
            ET.SubElement(o, t("OofState")).text = self.stand["OofState"]
            ET.SubElement(o, t("ExternalAudience")).text = self.stand["ExternalAudience"]
            d = ET.SubElement(o, t("Duration"))
            ET.SubElement(d, t("StartTime")).text = self.stand["StartTime"]
            ET.SubElement(d, t("EndTime")).text = self.stand["EndTime"]
            m = ET.SubElement(ET.SubElement(o, t("InternalReply")), t("Message"))
            if self.stand["intern"]:
                m.text = self.stand["intern"]       # leer → <Message/> wie beim echten Server
            m = ET.SubElement(ET.SubElement(o, t("ExternalReply")), t("Message"))
            if self.stand["extern"]:
                m.text = self.stand["extern"]
        return ET.tostring(env, encoding="utf-8", xml_declaration=True)


@pytest.fixture
def werte(monkeypatch):
    store = {"MAILBOX_CONFIG": {}}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: store.get(k, d))
    return store


@pytest.fixture
def exchange(werte, monkeypatch):
    werte.update(KONFIG)
    fx = FalscheExchange()
    monkeypatch.setattr(ews_onprem, "_senden", fx)
    return fx


# ── XML: Sonderzeichen, HTML, leere Nachricht ────────────────────────────────

def test_anfrage_maskiert_sonderzeichen_und_bleibt_gueltiges_xml():
    roh = ews_onprem.anfrage_oof_setzen(HYBRID, "Enabled", "All", SONDER, HTML)
    assert b"<Test>" not in roh and "&lt;Test&gt;".encode() in roh
    s = ET.fromstring(roh).find(".//t:UserOofSettings", NS)
    assert s.find("t:InternalReply/t:Message", NS).text == SONDER
    assert s.find("t:ExternalReply/t:Message", NS).text == HTML


def test_adresse_im_kopf_wird_maskiert():
    boese = 'a&b"<x>@firma.at'
    root = ET.fromstring(ews_onprem.anfrage_oof_lesen(boese))
    assert root.find(".//t:PrimarySmtpAddress", NS).text == boese
    assert root.find(".//t:Address", NS).text == boese


def test_lab_antwort_mit_leerer_nachricht():
    roh = LAB_ANTWORT.format(start=_zeit(0), ende=_zeit(1))
    oof = ews_onprem.oof_aus_antwort(ews_onprem.antwort_pruefen(200, roh.encode()))
    assert oof["state"] == "Disabled" and oof["extern_publikum"] == "All"
    assert oof["intern"] == "" and oof["extern"] == ""          # nie „<Message/>“
    g = ews_onprem.als_graph(oof)
    assert g["status"] == "disabled" and g["externalAudience"] == "all"
    assert g["scheduledStartDateTime"] == {"dateTime": _zeit(0)[:-1] + ".0000000", "timeZone": "UTC"}


@pytest.mark.parametrize("intern,extern", [(SONDER, HTML), ("", ""), (HTML, "")])
def test_rundlauf_lesen_setzen_lesen(exchange, intern, extern):
    vorher = ews_onprem.oof_lesen(HYBRID)
    gelesen = ews_onprem.oof_setzen(HYBRID, "Enabled", "Known", intern, extern)
    assert gelesen["intern"] == intern and gelesen["extern"] == extern
    assert gelesen["state"] == "Enabled" and gelesen["extern_publikum"] == "Known"
    # zurücksetzen mit genau dem Gelesenen — Ausgangszustand exakt wieder da
    zurueck = ews_onprem.oof_setzen(HYBRID, **vorher)
    assert zurueck == vorher
    assert "<Message" not in zurueck["intern"] + zurueck["extern"]


def test_zeitplan_rundlauf_in_utc(exchange):
    start, ende = _zeit(2), _zeit(5)
    erg = ews_onprem.oof_setzen(HYBRID, "Scheduled", "All", "a", "b", start, ende)
    assert (erg["start"], erg["ende"]) == (start, ende)
    # Graph-Form mit Nicht-UTC-Zone wird umgerechnet
    g = {"dateTime": "2030-01-10T09:00:00.0000000", "timeZone": "Europe/Berlin"}
    assert ews_onprem._zeit_ews(ews_onprem._iso_aus_graph(g)) == "2030-01-10T08:00:00Z"


# ── Fehler verständlich ──────────────────────────────────────────────────────

def test_impersonation_verweigert(exchange):
    exchange.verweigert.add(HYBRID)
    with pytest.raises(ews_onprem.EwsFehler) as e:
        ews_onprem.oof_lesen(HYBRID)
    assert e.value.code == "ErrorImpersonateUserDenied" and e.value.kein_zugriff
    assert "Management Scope" in e.value.detail
    erg = ews_onprem.verbindung_testen(HYBRID)
    assert erg["ok"] is False and erg["code"] == "ErrorImpersonateUserDenied"


@pytest.mark.parametrize("status,roh,code", [
    (401, b"", "anmeldung"),
    (502, b"<html>Bad Gateway", "http"),
    (200, b"kein xml", "antwort"),
    (200, b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>', "antwort"),
])
def test_fehlercodes(status, roh, code):
    with pytest.raises(ews_onprem.EwsFehler) as e:
        ews_onprem.antwort_pruefen(status, roh)
    assert e.value.code == code


def test_responseclass_error_mit_code():
    roh = (f'<s:Envelope xmlns:s="{ews_onprem.NS_SOAP}"><s:Body><GetUserOofSettingsResponse '
           f'xmlns="{ews_onprem.NS_M}"><ResponseMessage ResponseClass="Error"><MessageText>weg'
           '</MessageText><ResponseCode>ErrorNonExistentMailbox</ResponseCode></ResponseMessage>'
           '</GetUserOofSettingsResponse></s:Body></s:Envelope>')
    with pytest.raises(ews_onprem.EwsFehler) as e:
        ews_onprem.antwort_pruefen(200, roh.encode())
    assert e.value.code == "ErrorNonExistentMailbox" and not e.value.kein_zugriff


# ── Ohne Konfiguration: kein Netz ────────────────────────────────────────────

def test_nicht_konfiguriert_kein_netzaufruf(werte, monkeypatch):
    def netz(*a, **k):
        raise AssertionError("Netzaufruf ohne Konfiguration")
    monkeypatch.setattr(ews_onprem, "_senden", netz)
    monkeypatch.setattr(ews_onprem, "_sitzung_und_url", netz)
    assert ews_onprem.konfiguriert() is False
    with pytest.raises(ews_onprem.EwsFehler) as e:
        ews_onprem.oof_lesen(HYBRID)
    assert e.value.code == "konfiguration"
    assert ews_onprem.verbindung_testen(HYBRID)["ok"] is False
    werte.update({**KONFIG, "EWS_ONPREM_KENNWORT": ""})
    assert ews_onprem.konfiguriert() is False


# ── Die Weiche in abwesenheit ────────────────────────────────────────────────

class _GraphAttrappe:
    aufrufe: list = []

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def get(self, url, headers=None):
        _GraphAttrappe.aufrufe.append(("get", url))
        from types import SimpleNamespace
        return SimpleNamespace(status_code=200, text="{}", raise_for_status=lambda: None,
                               json=lambda: {"status": "disabled"})


@pytest.fixture
def weiche(werte, monkeypatch, tmp_path):
    """Ein Hybrid-Nutzer und ein EXO-Postfach; Graph und EWS als Attrappen."""
    monkeypatch.setattr(abwesenheit.config, "DATA_DIR", str(tmp_path))
    werte["MAILBOX_CONFIG"] = {"g-hybrid": {"sig": True, "hybrid": True, "primary": HYBRID},
                               "g-cloud": {"sig": True, "primary": CLOUD}}
    monkeypatch.setattr(exo_mailboxes, "_cache", [
        {"primary": CLOUD, "addresses": [CLOUD], "type": "UserMailbox", "guid": "g-cloud"}])
    _GraphAttrappe.aufrufe = []
    monkeypatch.setattr(abwesenheit.httpx, "AsyncClient", _GraphAttrappe)
    ews = []
    monkeypatch.setattr(ews_onprem, "oof_lesen",
                        lambda a: ews.append(a) or {"state": "Enabled", "extern_publikum": "All",
                                                    "intern": SONDER, "extern": "", "start": "",
                                                    "ende": ""})
    return werte, ews


def _lesen(upn):
    return asyncio.run(abwesenheit._get_setting(upn, "tok"))


def test_exo_postfach_nie_ueber_ews(weiche):
    werte, ews = weiche
    werte.update(KONFIG)
    status, setting = _lesen(CLOUD)
    assert status == "ok" and ews == []
    assert [u for k, u in _GraphAttrappe.aufrufe] == [
        f"https://graph.microsoft.com/v1.0/users/{CLOUD}/mailboxSettings/automaticRepliesSetting"]


def test_hybrid_mit_ews_liest_ueber_ews(weiche):
    werte, ews = weiche
    werte.update(KONFIG)
    status, setting = _lesen(HYBRID)
    assert status == "ok" and ews == [HYBRID] and _GraphAttrappe.aufrufe == []
    assert setting["status"] == "alwaysEnabled" and setting["internalReplyMessage"] == SONDER
    assert sorted(u for u, _ in abwesenheit._aktive_postfaecher(werte["MAILBOX_CONFIG"])) == \
        sorted([HYBRID, CLOUD])


def test_hybrid_ohne_ews_wie_bisher(weiche, monkeypatch):
    werte, ews = weiche
    monkeypatch.setattr(exo_mailboxes, "ist_hybrid",
                        lambda e: (_ for _ in ()).throw(AssertionError("ohne EWS nicht befragen")))
    assert [u for u, _ in abwesenheit._aktive_postfaecher(werte["MAILBOX_CONFIG"])] == [CLOUD]
    assert abwesenheit._ueber_ews(HYBRID) is False
    _lesen(CLOUD)
    assert ews == []


def test_schreiben_fuehrt_mit_dem_gelesenen_stand_zusammen(weiche, monkeypatch):
    werte, _ = weiche
    werte.update(KONFIG)
    gesetzt = {}

    def setzen(adresse, **felder):
        gesetzt.update(felder, adresse=adresse)
        return {**felder}
    monkeypatch.setattr(ews_onprem, "oof_setzen", setzen)
    gelesen = {"status": "scheduled", "externalAudience": "contactsOnly",
               "scheduledStartDateTime": {"dateTime": "2030-03-01T08:00:00.0000000", "timeZone": "UTC"},
               "scheduledEndDateTime": {"dateTime": "2030-03-05T08:00:00.0000000", "timeZone": "UTC"},
               "internalReplyMessage": "alt", "externalReplyMessage": "alt"}
    kanon = asyncio.run(abwesenheit._patch_setting(HYBRID, "tok", gelesen, SONDER, html_extern=HTML))
    assert gesetzt["adresse"] == HYBRID and _GraphAttrappe.aufrufe == []
    assert gesetzt["state"] == "Scheduled" and gesetzt["extern_publikum"] == "Known"
    assert gesetzt["intern"] == SONDER and gesetzt["extern"] == HTML
    assert ews_onprem._zeit_ews(gesetzt["start"]) == "2030-03-01T08:00:00Z"
    assert kanon["internalReplyMessage"] == SONDER and kanon["status"] == "scheduled"


def test_kein_zugriff_ueber_ews_eigene_meldung(weiche, monkeypatch):
    werte, _ = weiche
    werte.update(KONFIG)

    def verweigert(a):
        raise ews_onprem.EwsFehler("ErrorImpersonateUserDenied", "x")
    monkeypatch.setattr(ews_onprem, "oof_lesen", verweigert)
    assert _lesen(HYBRID)[0] == "kein_zugriff"
    assert "Dienstkonto" in abwesenheit.zugriff_meldung(HYBRID)
    assert [e["grund"] for e in abwesenheit.zugriff_stand()] == [abwesenheit.EWS_IMPERSONATION]


def test_kalender_fuer_ews_postfach_ohne_graph(weiche, monkeypatch):
    werte, _ = weiche
    werte.update(KONFIG, OOO_CALENDAR_READ=True)
    monkeypatch.setattr(abwesenheit, "kalender_lesen", lambda: True)

    aufrufe = []

    # Aufrufe zählen statt werfen: kalender_events fängt jede Ausnahme ab.
    async def graph_kalender(upn, token):
        aufrufe.append(upn)
        return [{"start": {}, "end": {}}]
    monkeypatch.setattr(abwesenheit, "_kalender_oof_events", graph_kalender)
    state = {}
    assert asyncio.run(abwesenheit.kalender_events(HYBRID, "tok", state)) == []
    assert aufrufe == [] and "kal" not in (state.get(HYBRID) or {})
    asyncio.run(abwesenheit.kalender_events(CLOUD, "tok", state))
    assert aufrufe == [CLOUD]                 # EXO-Postfach liest den Kalender weiter


# ── Gesundheitsprüfung ───────────────────────────────────────────────────────

def test_gesundheit_ews_zugriff_statt_uebersprungen(weiche, monkeypatch):
    import health_check
    werte, _ = weiche
    werte.update(KONFIG, ABWESENHEIT_AKTIV=True)
    monkeypatch.setattr(ews_onprem, "verbindung_testen",
                        lambda a: {"ok": False, "code": "ErrorImpersonateUserDenied",
                                   "detail": "Dienstkonto darf …", "dauer": 0.1})
    erg = asyncio.run(health_check._checks_hybrid(HYBRID, {"sig": False}, {}))
    assert erg["abwesenheit"]["status"] == "error"
    assert erg["graph_postfach"]["status"] == "skip"
    werte["EWS_ONPREM_URL"] = ""
    erg = asyncio.run(health_check._checks_hybrid(HYBRID, {"sig": False}, {}))
    assert erg["abwesenheit"]["status"] == "skip"


# ── Geheimnis ────────────────────────────────────────────────────────────────

def test_kennwort_ist_geheimnis():
    assert "EWS_ONPREM_KENNWORT" in settings_store.SECRET_KEYS
    for k in ("EWS_ONPREM_URL", "EWS_ONPREM_KONTO", "EWS_ONPREM_KENNWORT", "EWS_ONPREM_VERBINDEN"):
        assert settings_store.DEFAULTS[k] == ""


def test_kennwort_maskiert_in_public_view(monkeypatch):
    monkeypatch.setattr(settings_store, "get_all", lambda: {**KONFIG})
    pv = settings_store.public_view()
    assert pv["EWS_ONPREM_KENNWORT"] == settings_store.MASK
    assert pv["EWS_ONPREM_KONTO"] == KONFIG["EWS_ONPREM_KONTO"]


def test_vorlage_rendert_kein_kennwort():
    seite = (Path(__file__).resolve().parent.parent / "app/webui/templates/settings.html").read_text("utf-8")
    import re
    assert not re.search(r"value=\"\{\{\s*s\.EWS_ONPREM_KENNWORT", seite)
    assert re.search(r'id="ews-kennwort"[^>]*>', seite.replace("\n", " "))


def test_schreibweg_prueft_adresse_und_ip():
    from webui.routen.settings import _ews_onprem_pruefen
    assert _ews_onprem_pruefen({"EWS_ONPREM_URL": "http://exchange.firma.at/EWS/Exchange.asmx"})
    assert _ews_onprem_pruefen({"EWS_ONPREM_VERBINDEN": "exchange.firma.at"})
    assert _ews_onprem_pruefen({"EWS_ONPREM_KONTO": "FIRMA\\a\r\nb"})
    sauber = {"EWS_ONPREM_URL": " https://exchange.firma.at/EWS/Exchange.asmx ",
              "EWS_ONPREM_VERBINDEN": "10.99.0.20", "EWS_ONPREM_KENNWORT": " mit Leerzeichen "}
    assert _ews_onprem_pruefen(sauber) == ""
    assert sauber["EWS_ONPREM_URL"].startswith("https://") and sauber["EWS_ONPREM_KENNWORT"] == " mit Leerzeichen "


def test_split_dns_sitzung_prueft_den_hostnamen(werte):
    pytest.importorskip("requests_ntlm")
    werte.update(KONFIG, EWS_ONPREM_VERBINDEN="10.99.0.20")
    sitzung, url = ews_onprem._sitzung_und_url()
    assert url == "https://10.99.0.20/EWS/Exchange.asmx"
    assert sitzung.headers["Host"] == "exchange.firma.at"
    adapter = sitzung.get_adapter(url)
    assert adapter.poolmanager.connection_pool_kw["assert_hostname"] == "exchange.firma.at"
    assert adapter.poolmanager.connection_pool_kw["server_hostname"] == "exchange.firma.at"
    assert sitzung.verify is True
