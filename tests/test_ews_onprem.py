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


def test_kalender_hybrid_ueber_ews_exo_ueber_graph(weiche, monkeypatch):
    """Die Weiche gilt auch für den Kalender: Hybrid-Nutzer über EWS (seit
    v1.12.61, vorher gesperrt), EXO-Postfächer unverändert über Graph."""
    werte, _ = weiche
    werte.update(KONFIG, OOO_CALENDAR_READ=True)
    ews = []
    monkeypatch.setattr(ews_onprem, "kalender_lesen", lambda a, von, bis: ews.append(a) or [])
    state = {}
    assert asyncio.run(abwesenheit.kalender_events(HYBRID, "tok", state)) == []
    assert ews == [HYBRID] and _GraphAttrappe.aufrufe == []
    assert state[HYBRID]["kal"]["events"] == []
    asyncio.run(abwesenheit.kalender_events(CLOUD, "tok", state))
    assert ews == [HYBRID]
    assert [u.split("?")[0] for _, u in _GraphAttrappe.aufrufe] == [
        f"https://graph.microsoft.com/v1.0/users/{CLOUD}/calendarView"]


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


# ── Eigene CA (EWS_ONPREM_CA) ────────────────────────────────────────────────
#
# Eine Test-CA und ein von ihr ausgestelltes Serverzertifikat, dazu ein echter
# TLS-Server auf 127.0.0.1. Geprüft wird der Weg, den der Verbindungstest geht:
# ohne CA scheitert die Prüfung, mit CA geht sie durch, mit fremder CA oder
# falschem Hostnamen scheitert sie weiter — und certifi bleibt im Kontext.

EWS_HOST = "ews.firma.test"


def _schluessel():
    from cryptography.hazmat.primitives.asymmetric import ec
    return ec.generate_private_key(ec.SECP256R1())


def _zert(name, schluessel, aussteller=None, aussteller_schluessel=None, ca=False,
          dns=None, tage_ab=-1, tage_bis=30, grund=True):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import NameOID
    jetzt = datetime.now(timezone.utc)
    subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    b = (x509.CertificateBuilder().subject_name(subj)
         .issuer_name(aussteller.subject if aussteller else subj)
         .public_key(schluessel.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(jetzt + timedelta(days=tage_ab))
         .not_valid_after(jetzt + timedelta(days=tage_bis)))
    if grund:
        b = b.add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    if ca:
        b = b.add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False),
                            critical=True)
    if dns:
        b = b.add_extension(x509.SubjectAlternativeName([x509.DNSName(dns)]), critical=False)
    return b.sign(aussteller_schluessel or schluessel, hashes.SHA256())


def _pem(z):
    from cryptography.hazmat.primitives.serialization import Encoding
    return z.public_bytes(Encoding.PEM).decode("ascii")


@pytest.fixture(scope="module")
def pki():
    ca_k = _schluessel()
    ca = _zert("Firma Test-CA", ca_k, ca=True)
    srv_k = _schluessel()
    srv = _zert(EWS_HOST, srv_k, aussteller=ca, aussteller_schluessel=ca_k, dns=EWS_HOST)
    fremd_k = _schluessel()
    fremd = _zert("Fremde Test-CA", fremd_k, ca=True)
    # Selbstsigniert ohne BasicConstraints — wie das Standardzertifikat einer frisch
    # installierten Exchange.
    selbst_k = _schluessel()
    selbst = _zert(EWS_HOST, selbst_k, dns=EWS_HOST, grund=False)
    return {"ca": ca, "ca_k": ca_k, "srv": srv, "srv_k": srv_k, "fremd": fremd,
            "selbst": selbst, "selbst_k": selbst_k}


class _TlsServer:
    """HTTPS-Server auf 127.0.0.1, antwortet auf POST mit 200 „ok“."""

    def __init__(self, zert, schluessel, tmp_path):
        import http.server
        import ssl
        import threading
        from cryptography.hazmat.primitives.serialization import (
            Encoding, NoEncryption, PrivateFormat)
        cpath, kpath = tmp_path / "srv.pem", tmp_path / "srv.key"
        cpath.write_text(_pem(zert))
        kpath.write_bytes(schluessel.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cpath), str(kpath))
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def url(self, host=EWS_HOST):
        return f"https://{host}:{self.port}/EWS/Exchange.asmx"

    def zu(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server(pki, tmp_path):
    s = _TlsServer(pki["srv"], pki["srv_k"], tmp_path)
    yield s
    s.zu()


def _post(url, ca=""):
    """Wie _senden: Split-DNS auf 127.0.0.1, damit der Hostname nicht aufgelöst
    werden muss — die Prüfung läuft weiter gegen den Namen in der URL."""
    import requests
    sitzung, ziel = ews_onprem._tls_sitzung(url, "127.0.0.1", ca)
    try:
        return sitzung.post(ziel, data=b"x", timeout=5).status_code
    except requests.exceptions.SSLError:
        return "tls"
    finally:
        sitzung.close()


def test_ohne_ca_scheitert_die_pruefung(server):
    assert _post(server.url()) == "tls"


def test_mit_eigener_ca_geht_die_pruefung_durch(server, pki):
    assert _post(server.url(), _pem(pki["ca"])) == 200


def test_fremde_ca_hilft_nicht(server, pki):
    assert _post(server.url(), _pem(pki["fremd"])) == "tls"


def test_hostname_wird_mit_eigener_ca_weiter_geprueft(server, pki):
    assert _post(server.url("anders.firma.test"), _pem(pki["ca"])) == "tls"


def test_mehrere_cas_im_feld(server, pki):
    assert _post(server.url(), _pem(pki["fremd"]) + _pem(pki["ca"])) == 200


def test_selbstsigniertes_exchange_zertifikat(pki, tmp_path):
    s = _TlsServer(pki["selbst"], pki["selbst_k"], tmp_path)
    try:
        assert _post(s.url()) == "tls"
        pem, fehler = ews_onprem.ca_pruefen(_pem(pki["selbst"]))
        assert fehler == ""
        assert _post(s.url(), pem) == 200
    finally:
        s.zu()


def test_eigene_ca_ergaenzt_certifi(pki):
    """Ergänzen, nicht ersetzen: Alle certifi-Wurzeln stehen weiter im Kontext."""
    import ssl

    import certifi
    nur = ssl.create_default_context(cafile=certifi.where())
    mit = ews_onprem._tls_kontext(_pem(pki["ca"]))
    namen = {tuple(c["subject"]) for c in mit.get_ca_certs()}
    assert all(tuple(c["subject"]) in namen for c in nur.get_ca_certs())
    assert len(mit.get_ca_certs()) == len(nur.get_ca_certs()) + 1
    assert mit.verify_mode == ssl.CERT_REQUIRED


def test_ohne_ca_und_ip_bleibt_die_sitzung_wie_bisher():
    import requests
    sitzung, url = ews_onprem._tls_sitzung(KONFIG["EWS_ONPREM_URL"])
    assert url == KONFIG["EWS_ONPREM_URL"]
    assert type(sitzung.get_adapter(url)) is requests.adapters.HTTPAdapter
    assert sitzung.verify is True


def test_tls_fehler_nennt_ob_ca_hinterlegt_ist(server, pki, werte, monkeypatch):
    werte.update(KONFIG, EWS_ONPREM_URL=server.url(), EWS_ONPREM_VERBINDEN="127.0.0.1")
    monkeypatch.setattr(ews_onprem, "_sitzung_und_url",
                        lambda: ews_onprem._tls_sitzung(server.url(), "127.0.0.1",
                                                        ews_onprem._wert(ews_onprem.KEY_CA)))
    erg = ews_onprem.verbindung_testen(HYBRID)
    assert erg["code"] == "tls" and "Kein eigenes CA-Zertifikat hinterlegt" in erg["detail"]
    werte["EWS_ONPREM_CA"] = _pem(pki["fremd"])
    erg = ews_onprem.verbindung_testen(HYBRID)
    assert erg["code"] == "tls" and "Eigenes CA-Zertifikat hinterlegt (1)" in erg["detail"]
    werte["EWS_ONPREM_CA"] = _pem(pki["ca"])
    erg = ews_onprem.verbindung_testen(HYBRID)
    assert erg["code"] == "antwort"          # TLS durch; „ok“ ist kein SOAP


def test_ca_pruefen_normalisiert_und_lehnt_ab(pki):
    ca = _pem(pki["ca"])
    assert ews_onprem.ca_pruefen("") == ("", "")
    assert ews_onprem.ca_pruefen("  \n") == ("", "")
    pem, f = ews_onprem.ca_pruefen("Bag Attributes\n  friendlyName: x\n" + ca + "\nnoch Text")
    assert f == "" and pem == ca
    # Serverzertifikat statt CA: Meldung nennt die ausstellende CA.
    _, f = ews_onprem.ca_pruefen(_pem(pki["srv"]))
    assert "Firma Test-CA" in f and "kein CA-Zertifikat" in f
    # Privater Schlüssel wird nie angenommen (landete sonst in settings.json).
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
    schl = pki["ca_k"].private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    _, f = ews_onprem.ca_pruefen(ca + schl)
    assert "privater Schlüssel" in f
    assert ews_onprem.ca_pruefen("MIIB…ohne Kopf")[1]
    assert ews_onprem.ca_pruefen("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----")[1]
    # Abgelaufen bzw. noch nicht gültig — relativ zu jetzt.
    k = _schluessel()
    _, f = ews_onprem.ca_pruefen(_pem(_zert("Alte CA", k, ca=True, tage_ab=-30, tage_bis=-1)))
    assert "abgelaufen" in f
    _, f = ews_onprem.ca_pruefen(_pem(_zert("Neue CA", k, ca=True, tage_ab=2, tage_bis=30)))
    assert "gilt erst ab" in f


def test_schreibweg_prueft_und_normalisiert_die_ca(pki):
    from webui.routen.settings import _ews_onprem_pruefen
    ca = _pem(pki["ca"])
    sauber = {"EWS_ONPREM_CA": "\n" + ca + "\n\n"}
    assert _ews_onprem_pruefen(sauber) == "" and sauber["EWS_ONPREM_CA"] == ca
    leer = {"EWS_ONPREM_CA": ""}
    assert _ews_onprem_pruefen(leer) == "" and leer["EWS_ONPREM_CA"] == ""
    assert _ews_onprem_pruefen({"EWS_ONPREM_CA": _pem(pki["srv"])})
    assert _ews_onprem_pruefen({"EWS_ONPREM_CA": 42}) == ""      # kein Text → leer → ok
    assert settings_store.DEFAULTS["EWS_ONPREM_CA"] == ""
    assert "EWS_ONPREM_CA" not in settings_store.SECRET_KEYS     # öffentlich, kein Geheimnis


def test_ca_feld_haengt_am_speichern_knopf():
    seite = (Path(__file__).resolve().parent.parent / "app/webui/templates/settings.html").read_text("utf-8")
    import re
    wache = re.search(r'id="ews-btn" data-wache="([^"]+)"', seite).group(1).split(",")
    assert "ews-ca" in wache
    assert "EWS_ONPREM_CA: feldWert('ews-ca')" in seite


# ── Kalender über EWS (Kalender-Automatik und Ankündigung für Hybrid-Nutzer) ─

def _kal_antwort(termine, gesamt=None):
    """FindItem-Antwort in der Form einer Exchange SE: m:/t:-Präfixe,
    FindItemResponseMessage in ResponseMessages, Felder in Schema-Reihenfolge."""
    items = "".join(
        '<t:CalendarItem><t:ItemId Id="AAMk{i}" ChangeKey="DwAA"/>'
        '<t:Sensitivity>{sens}</t:Sensitivity>'
        '<t:Start>{start}</t:Start><t:End>{ende}</t:End>'
        '<t:IsAllDayEvent>{ganz}</t:IsAllDayEvent>'
        '<t:LegacyFreeBusyStatus>{fb}</t:LegacyFreeBusyStatus></t:CalendarItem>'.format(i=i, **t)
        for i, t in enumerate(termine))
    n = len(termine) if gesamt is None else gesamt
    return (
        '<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        '<s:Header><h:ServerVersionInfo MajorVersion="15" MinorVersion="2" '
        'xmlns:h="http://schemas.microsoft.com/exchange/services/2006/types"/></s:Header>'
        '<s:Body><m:FindItemResponse xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages" '
        'xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"><m:ResponseMessages>'
        '<m:FindItemResponseMessage ResponseClass="Success"><m:ResponseCode>NoError</m:ResponseCode>'
        f'<m:RootFolder TotalItemsInView="{n}" IncludesLastItemInRange="true"><t:Items>{items}</t:Items>'
        '</m:RootFolder></m:FindItemResponseMessage></m:ResponseMessages></m:FindItemResponse>'
        '</s:Body></s:Envelope>').encode()


def _kal_fehler(code):
    return (
        '<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        '<s:Body><m:FindItemResponse xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages">'
        '<m:ResponseMessages><m:FindItemResponseMessage ResponseClass="Error">'
        f'<m:MessageText>x</m:MessageText><m:ResponseCode>{code}</m:ResponseCode>'
        '<m:DescriptiveLinkKey>0</m:DescriptiveLinkKey></m:FindItemResponseMessage>'
        '</m:ResponseMessages></m:FindItemResponse></s:Body></s:Envelope>').encode()


MITTAG = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)


def _z(tage, stunde):
    return (MITTAG + timedelta(days=tage)).replace(hour=stunde).strftime("%Y-%m-%dT%H:%M:%SZ")


def _berliner_mitternacht_utc(tage):
    """UTC-Zeitpunkt der Berliner Mitternacht am Tag MITTAG+tage — so liefert EWS
    die Grenzen eines ganztägigen Termins in Europe/Berlin."""
    from zoneinfo import ZoneInfo
    tag = (MITTAG + timedelta(days=tage)).date()
    lokal = datetime(tag.year, tag.month, tag.day, tzinfo=ZoneInfo("Europe/Berlin"))
    return lokal.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), tag


ZEITTERMIN = {"sens": "Normal", "start": _z(3, 7), "ende": _z(3, 16), "ganz": "false", "fb": "OOF"}
BELEGT = {"sens": "Normal", "start": _z(4, 7), "ende": _z(4, 17), "ganz": "false", "fb": "Busy"}
KURZ = {"sens": "Normal", "start": _z(5, 9), "ende": _z(5, 11), "ganz": "false", "fb": "OOF"}
PRIVAT = {"sens": "Private", "start": _z(6, 6), "ende": _z(6, 18), "ganz": "false", "fb": "OOF"}


@pytest.fixture
def kalender(weiche, monkeypatch):
    """Hybrid-Nutzer mit EWS; _senden liefert eine vorgegebene Antwort."""
    werte, _ = weiche
    werte.update(KONFIG, LOG_TIMEZONE="Europe/Berlin", OOO_CALENDAR_MIN_HOURS=8,
                 OOO_CALENDAR_READ=True)
    antwort = {"status": 200, "roh": _kal_antwort([])}
    anfragen = []

    def senden(ziel, umschlag):
        anfragen.append((ziel, umschlag))
        return antwort["status"], antwort["roh"]
    monkeypatch.setattr(ews_onprem, "_senden", senden)
    return antwort, anfragen


def test_kalender_anfrage(kalender):
    antwort, anfragen = kalender
    asyncio.run(abwesenheit._kalender_oof_events(HYBRID, "tok"))
    assert _GraphAttrappe.aufrufe == []
    ziel, umschlag = anfragen[0]
    root = ET.fromstring(umschlag)
    assert ziel == HYBRID
    assert root.find("s:Header/t:ExchangeImpersonation/t:ConnectingSID/t:PrimarySmtpAddress",
                     NS).text == HYBRID
    fi = root.find("s:Body/m:FindItem", NS)
    assert fi.get("Traversal") == "Shallow"
    # Schema-Reihenfolge: ItemShape, CalendarView, ParentFolderIds
    assert [el.tag.split("}")[1] for el in fi] == ["ItemShape", "CalendarView", "ParentFolderIds"]
    felder = {f.get("FieldURI") for f in fi.iter(f"{{{ews_onprem.NS_T}}}FieldURI")}
    assert felder == {"calendar:Start", "calendar:End", "calendar:LegacyFreeBusyStatus",
                      "calendar:IsAllDayEvent", "item:Sensitivity"}       # kein Betreff
    cv = fi.find("m:CalendarView", NS)
    von = datetime.fromisoformat(cv.get("StartDate").replace("Z", "+00:00"))
    bis = datetime.fromisoformat(cv.get("EndDate").replace("Z", "+00:00"))
    assert cv.get("StartDate").endswith("Z") and cv.get("MaxEntriesReturned") is None
    assert abs((datetime.now(timezone.utc) - von).total_seconds()) < 120
    assert (bis - von).days == abwesenheit._LOOKAHEAD_TAGE
    ordner = fi.find("m:ParentFolderIds/t:DistinguishedFolderId", NS)
    assert ordner.get("Id") == "calendar"
    assert ordner.find("t:Mailbox/t:EmailAddress", NS).text == HYBRID


def test_kalender_anfrage_rechnet_in_utc():
    from zoneinfo import ZoneInfo
    von = datetime(MITTAG.year, MITTAG.month, MITTAG.day, 10, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    root = ET.fromstring(ews_onprem.anfrage_kalender(HYBRID, von, von + timedelta(hours=1)))
    erwartet = von.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert root.find(".//m:CalendarView", NS).get("StartDate") == erwartet


def test_kalender_ews_wie_graph(kalender, monkeypatch):
    """Dieselben Termine über EWS und über Graph ergeben dasselbe Ergebnis —
    die Auswertung (Abwesend, Mindestdauer, privat, ganztägig) ist EINE."""
    antwort, _ = kalender
    g_start, tag = _berliner_mitternacht_utc(10)
    g_ende, _ = _berliner_mitternacht_utc(13)
    ganz = {"sens": "Normal", "start": g_start, "ende": g_ende, "ganz": "true", "fb": "OOF"}
    antwort["roh"] = _kal_antwort([PRIVAT, ganz, BELEGT, KURZ, ZEITTERMIN])
    ews = asyncio.run(abwesenheit._kalender_oof_events(HYBRID, "tok"))

    def g(t, ganztag):
        if ganztag:
            d = (MITTAG + timedelta(days=t)).strftime("%Y-%m-%dT00:00:00.0000000")
            return {"dateTime": d, "timeZone": "UTC"}
        return {"dateTime": t.replace("Z", ".0000000"), "timeZone": "UTC"}
    graph_werte = [
        {"showAs": "oof", "sensitivity": "private", "isAllDay": False,
         "start": g(PRIVAT["start"], False), "end": g(PRIVAT["ende"], False)},
        {"showAs": "oof", "sensitivity": "normal", "isAllDay": True,
         "start": g(10, True), "end": g(13, True)},
        {"showAs": "busy", "sensitivity": "normal", "isAllDay": False,
         "start": g(BELEGT["start"], False), "end": g(BELEGT["ende"], False)},
        {"showAs": "oof", "sensitivity": "normal", "isAllDay": False,
         "start": g(KURZ["start"], False), "end": g(KURZ["ende"], False)},
        {"showAs": "oof", "sensitivity": "normal", "isAllDay": False,
         "start": g(ZEITTERMIN["start"], False), "end": g(ZEITTERMIN["ende"], False)}]

    from types import SimpleNamespace

    class _Kal(_GraphAttrappe):
        async def get(self, url, headers=None):
            return SimpleNamespace(status_code=200, text="", raise_for_status=lambda: None,
                                   json=lambda: {"value": graph_werte})
    monkeypatch.setattr(abwesenheit.httpx, "AsyncClient", _Kal)
    graph = asyncio.run(abwesenheit._kalender_oof_events(CLOUD, "tok"))
    assert ews == graph
    # Inhaltlich: Belegt und kurz fallen weg, Reihenfolge nach Beginn, privat erkannt,
    # ganztägig = Berliner Mitternacht (wie beim Graph-Weg).
    assert [e["privat"] for e in ews] == [False, True, False]
    assert ews[0]["start"]["dateTime"] == ZEITTERMIN["start"].replace("Z", ".0000000")
    assert ews[2]["start"] == abwesenheit.als_utc(f"{tag:%Y-%m-%d}", "00:00:00")
    assert abwesenheit._fenster_texte(ews[2]["start"], ews[2]["end"]).de == \
        f"vom {tag:%d.%m.%Y} bis {(tag + timedelta(days=2)):%d.%m.%Y}"


@pytest.mark.parametrize("utc_stunde,tag_versatz", [
    (5, 0),     # Termin-Zone New York (UTC−5): Mitternacht = 05:00 UTC desselben Tags
    (15, 1),    # Termin-Zone Tokio (UTC+9): Mitternacht = 15:00 UTC des Vortags
    (0, 0),     # Termin-Zone UTC
])
def test_ganztaegig_aus_anderer_zeitzone_bleibt_am_richtigen_tag(utc_stunde, tag_versatz):
    from zoneinfo import ZoneInfo
    start = (MITTAG + timedelta(days=5)).replace(hour=utc_stunde)
    ev = ews_onprem.kalender_als_graph([{"start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                         "ende": "", "frei_belegt": "OOF", "ganztaegig": True,
                                         "vertraulichkeit": "Normal"}], ZoneInfo("Europe/Berlin"))[0]
    erwartet = (start + timedelta(days=tag_versatz)).strftime("%Y-%m-%dT00:00:00.0000000")
    assert ev["start"] == {"dateTime": erwartet, "timeZone": "UTC"}


@pytest.mark.parametrize("ews_wert,graph_wert", [
    ("Private", "private"), ("Confidential", "confidential"), ("Personal", "personal"),
    ("Normal", "normal"), ("", "normal")])
def test_vertraulichkeit_wie_graph(ews_wert, graph_wert):
    ev = ews_onprem.kalender_als_graph([{"start": _z(1, 8), "ende": _z(1, 18), "frei_belegt": "OOF",
                                         "ganztaegig": False, "vertraulichkeit": ews_wert}])[0]
    assert ev["sensitivity"] == graph_wert
    assert abwesenheit._qualifizierende([ev], 8)[0]["privat"] is (graph_wert in ("private", "confidential"))


def test_kalender_leer_wird_gemerkt(kalender):
    antwort, anfragen = kalender
    antwort["roh"] = _kal_antwort([], gesamt=0)
    state = {}
    assert asyncio.run(abwesenheit.kalender_events(HYBRID, "tok", state)) == []
    kal = state[HYBRID]["kal"]
    assert kal["events"] == [] and kal["ts"] and "fehler" not in kal
    asyncio.run(abwesenheit.kalender_events(HYBRID, "tok", state))
    assert len(anfragen) == 1                     # aus dem Cache bedient


def test_kalender_impersonation_verweigert_wie_403(kalender):
    antwort, _ = kalender
    antwort.update(status=500, roh=LAB_VERWEIGERT.encode())
    state = {}
    assert asyncio.run(abwesenheit.kalender_events(HYBRID, "tok", state)) == []
    assert state[HYBRID]["kal"]["fehler"] == "403"


@pytest.mark.parametrize("status,roh", [
    (200, _kal_fehler("ErrorNonExistentMailbox")),
    (200, _kal_fehler("ErrorServerBusy")),
    (401, b""),
    (503, b"<html>Service Unavailable</html>"),
    (200, b"kein xml")])
def test_kalender_echter_fehler_wird_nicht_gecacht(kalender, status, roh):
    antwort, _ = kalender
    state = {HYBRID: {"kal": {"ts": "", "events": [{"start": {}, "end": {}}], "v": abwesenheit._KAL_STAND}}}
    antwort.update(status=status, roh=roh)
    erg = asyncio.run(abwesenheit.kalender_events(HYBRID, "tok", state))
    assert erg == [{"start": {}, "end": {}}]          # voriger Stand gilt weiter
    assert state[HYBRID]["kal"]["ts"] == ""           # nicht als „gelesen“ gemerkt
    with pytest.raises(Exception):
        asyncio.run(abwesenheit._kalender_oof_events(HYBRID, "tok"))


def test_kalender_responseclass_error_wird_erkannt():
    with pytest.raises(ews_onprem.EwsFehler) as e:
        ews_onprem.antwort_pruefen(200, _kal_fehler("ErrorFolderNotFound"))
    assert e.value.code == "ErrorFolderNotFound"
