"""Regelprobe für Hybrid-Nutzer (v1.12.67).

Eine Mail eines lokalen Postfachs an sich selbst bleibt in der lokalen
Organisation und erreicht die Transportregel nie. Die Probe geht deshalb per
EWS (CreateItem SendOnly, Impersonation) aus dem lokalen Postfach an das
Absender-Postfach der Benachrichtigungen (ein EXO-Postfach). Greift die Regel
nicht, wird sie dort per Graph gesucht und gelöscht.

Sicherheit — schlägt fehl, wenn die Bindung aufgeweicht wird:
  * Verworfen wird nur, wenn der Absender eine Adresse GENAU dieses
    Hybrid-Nutzers und der EINZIGE Empfänger eine Adresse GENAU dieses
    Prüfpostfachs ist.
  * Die Bindung eines EXO-Postfachs bleibt „an sich selbst“ — eine Mail an das
    Prüfpostfach verbraucht dessen Kennung nicht.

Testdaten erfunden (firma.at).
"""
from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET

import pytest

import ews_onprem
import exo_mailboxes
import regel_probe as rp
import settings_store

HYBRID = "max.mustermann@firma.at"
HYBRID_ALIAS = "mm@firma.at"
CLOUD = "erika.musterfrau@firma.at"
PRUEF = "gateway-meldungen@firma.at"
PRUEF_ALIAS = "gateway-meldungen@firma.mail.onmicrosoft.com"
PRUEF_MOERA = "gateway-meldungen@firma.onmicrosoft.com"
FREMD_EXO = "paul.beispiel@firma.at"
KONFIG = {"EWS_ONPREM_URL": "https://exchange.firma.at/EWS/Exchange.asmx",
          "EWS_ONPREM_KONTO": "FIRMA\\svc-gateway", "EWS_ONPREM_KENNWORT": "nur-ein-testwert"}


@pytest.fixture(autouse=True)
def sauber(monkeypatch):
    rp._offen.clear()
    rp._laufend.clear()
    monkeypatch.setattr(rp, "TAKT_S", 0.01)
    monkeypatch.setattr(rp, "SUCHE_TAKT_S", 0.02)
    monkeypatch.setattr(rp, "ANKUNFT_FRIST_S", 0.05)
    monkeypatch.setattr(rp, "GESAMT_FRIST_S", 0.3)
    yield
    rp._offen.clear()
    rp._laufend.clear()


@pytest.fixture
def umgebung(monkeypatch):
    store = {**KONFIG, "HYBRID_POSTFAECHER": True, "NOTIFICATION_MAILBOX": PRUEF,
             "MAILBOX_HEALTH": {},
             "MAILBOX_CONFIG": {"g-h": {"sig": True, "hybrid": True, "primary": HYBRID,
                                        "known_addresses": [HYBRID, HYBRID_ALIAS]},
                                "g-c": {"sig": True, "primary": CLOUD,
                                        "known_addresses": [CLOUD]}}}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(settings_store, "force_update", lambda p: store.update(p))
    monkeypatch.setattr(settings_store, "reinject_mode", lambda: "smtp")
    monkeypatch.setattr(exo_mailboxes, "_cache", [
        {"guid": "g-h", "primary": HYBRID, "addresses": [HYBRID, HYBRID_ALIAS],
         "display_name": "Max", "type": "MailUser"},
        {"guid": "g-c", "primary": CLOUD, "addresses": [CLOUD], "display_name": "Erika",
         "type": "UserMailbox"},
        {"guid": "g-p", "primary": PRUEF, "addresses": [PRUEF, PRUEF_ALIAS],
         "display_name": "Meldungen", "type": "SharedMailbox"},
        {"guid": "g-f", "primary": FREMD_EXO, "addresses": [FREMD_EXO], "display_name": "Paul",
         "type": "UserMailbox"}])
    return store


# ── Plan: wann geht die Probe, wann entfällt sie ────────────────────────────

def test_plan_hybrid_mit_anbindung_und_pruefpostfach(umgebung):
    grund, plan = rp._plan(HYBRID)
    assert grund == "" and plan["hybrid"] is True and plan["postfach"] == PRUEF
    assert plan["ziel"] == PRUEF_ALIAS, "Routing-Adresse, nicht die primäre (lokal 550 5.1.10)"
    assert set(plan["absender"]) == {HYBRID, HYBRID_ALIAS}
    assert set(plan["empfaenger"]) == {PRUEF, PRUEF_ALIAS}


def test_plan_cloud_unveraendert_an_sich_selbst(umgebung):
    grund, plan = rp._plan(CLOUD)
    assert grund == "" and plan == {"hybrid": False, "postfach": CLOUD,
                                    "absender": [CLOUD], "empfaenger": [CLOUD]}


@pytest.mark.parametrize("aenderung,grund", [
    ({"EWS_ONPREM_URL": ""}, "HYBRID_OHNE_EWS"),
    ({"NOTIFICATION_MAILBOX": ""}, "HYBRID_OHNE_POSTFACH"),
    ({"NOTIFICATION_MAILBOX": "unbekannt@firma.at"}, "HYBRID_OHNE_POSTFACH"),
    ({"NOTIFICATION_MAILBOX": HYBRID_ALIAS}, "HYBRID_OHNE_POSTFACH"),   # kein EXO-Postfach
    ({"HYBRID_POSTFAECHER": False}, "HYBRID_INAKTIV"),
])
def test_plan_entfaellt_mit_grund(umgebung, aenderung, grund):
    umgebung.update(aenderung)
    g, plan = rp._plan(HYBRID)
    assert g == getattr(rp, grund) and plan is None and rp.entfaellt(g)


def test_plan_nicht_aktiv_ist_ablehnung_nicht_entfaellt(umgebung):
    umgebung["MAILBOX_CONFIG"]["g-h"]["sig"] = False
    g, plan = rp._plan(HYBRID)
    assert g and plan is None and not rp.entfaellt(g)


# ── Bindung: Sicherheit ─────────────────────────────────────────────────────

def _hybrid_kennung():
    pid, geheim = rp.ausstellen(HYBRID, [HYBRID, HYBRID_ALIAS], [PRUEF, PRUEF_ALIAS])
    return pid, f"{pid}.{geheim}"


@pytest.mark.parametrize("absender,empfaenger", [
    (HYBRID, [PRUEF]), (HYBRID_ALIAS, [PRUEF_ALIAS]), (HYBRID.upper(), [PRUEF.upper()])])
def test_hybrid_probe_wird_erkannt(absender, empfaenger):
    pid, k = _hybrid_kennung()
    erg = rp.erkennen(k, "", absender, empfaenger)
    assert erg and erg["adresse"] == HYBRID and erg["probe_id"] == pid


@pytest.mark.parametrize("absender,empfaenger", [
    (HYBRID, [PRUEF, "kunde@extern.example"]),     # zweiter Empfänger
    (HYBRID, [HYBRID]),                            # an sich selbst (lokal) — nicht gebunden
    (HYBRID, [FREMD_EXO]),                         # anderes EXO-Postfach
    (CLOUD, [PRUEF]),                              # fremder Absender
    ("angreifer@extern.example", [PRUEF]),
    (HYBRID, []),
])
def test_hybrid_bindung_streng(absender, empfaenger):
    _pid, k = _hybrid_kennung()
    assert rp.erkennen(k, "", absender, empfaenger) is None
    assert rp.erkennen(k, "", HYBRID, [PRUEF]) is not None, "Fehlversuch verbraucht nicht"


def test_cloud_bindung_bleibt_an_sich_selbst():
    """Eine EXO-Probe ist NICHT gegen das Prüfpostfach einlösbar."""
    pid, geheim = rp.ausstellen(CLOUD)
    k = f"{pid}.{geheim}"
    assert rp._offen[pid]["absender"] == {CLOUD} and rp._offen[pid]["empfaenger"] == {CLOUD}
    assert rp.erkennen(k, "", CLOUD, [PRUEF]) is None
    assert rp.erkennen(k, "", PRUEF, [CLOUD]) is None
    assert rp.erkennen(k, "", CLOUD, [CLOUD]) is not None


def test_leere_bindung_verwirft_nichts():
    """Fail-closed: eine leere Adressmenge bindet an NICHTS, nicht an alles."""
    pid, geheim = rp.ausstellen(HYBRID, [""], [""])
    assert rp._offen[pid]["empfaenger"] == frozenset()
    for abs_, rcpt in ((HYBRID, [HYBRID]), (HYBRID, [""]), ("", [""])):
        assert rp.erkennen(f"{pid}.{geheim}", "", abs_, rcpt) is None


# ── Ablauf ──────────────────────────────────────────────────────────────────

@pytest.fixture
def wege(umgebung, monkeypatch):
    z = {"ews": [], "graph": [], "gesucht": [], "geloescht": [], "ankunft": False,
         "posteingang": None, "ndr": [], "ndr_gesucht": [], "ndr_geloescht": [],
         "ndr_loeschfehler": None, "ndr_suchfehler": None}

    def ews_senden(von, an, betreff, kopf, wert, text):
        z["ews"].append((von, an, betreff, kopf, wert))
        if z["ankunft"]:     # Regel greift: am Gateway, Umschlag wie aus EXO
            rp.erkennen(wert, betreff, HYBRID_ALIAS, [PRUEF_ALIAS])

    async def graph_senden(adresse, pid, geheim):
        z["graph"].append(adresse)
        return ""

    async def suchen(postfach, pid, ab):
        z["gesucht"].append(postfach)
        return z["posteingang"]

    async def loeschen(postfach, nid):
        z["geloescht"].append((postfach, nid))
        return ""
    def ndr_suchen(adresse, teil, ab):
        z["ndr_gesucht"].append((adresse, teil))
        if z["ndr_suchfehler"]:
            raise z["ndr_suchfehler"]
        return [n for n in z["ndr"] if teil in n["betreff"]]

    def ndr_loeschen(adresse, kennung, aenderung=""):
        if z["ndr_loeschfehler"]:
            raise z["ndr_loeschfehler"]
        z["ndr_geloescht"].append((adresse, kennung, aenderung))
    monkeypatch.setattr(ews_onprem, "regelprobe_senden", ews_senden)
    monkeypatch.setattr(ews_onprem, "regelprobe_ndr_suchen", ndr_suchen)
    monkeypatch.setattr(ews_onprem, "regelprobe_ndr_loeschen", ndr_loeschen)
    monkeypatch.setattr(rp, "_senden", graph_senden)
    monkeypatch.setattr(rp, "_im_posteingang", suchen)
    monkeypatch.setattr(rp, "_loeschen", loeschen)
    monkeypatch.setattr(rp, "_regel_wartet", lambda cfg: False)
    return z


def test_hybrid_greift_ueber_ews(wege):
    wege["ankunft"] = True
    rp.anmelden([HYBRID])
    erg = asyncio.run(rp.pruefen(HYBRID))
    assert erg["ergebnis"] == "greift", erg
    assert wege["graph"] == [], "nie Graph sendMail ALS Hybrid-Nutzer"
    (von, an, betreff, kopf, wert), = wege["ews"]
    assert (von, an, kopf) == (HYBRID, PRUEF_ALIAS, rp.KOPF) and wert in betreff
    assert rp._offen == {}


def test_hybrid_greift_nicht_sucht_und_loescht_im_pruefpostfach(wege, umgebung):
    wege["posteingang"] = {"id": "AAMk1", "ueber_gateway": False}
    rp.anmelden([HYBRID])
    erg = asyncio.run(rp.pruefen(HYBRID))
    assert erg["ergebnis"] == "greift_nicht"
    assert set(wege["gesucht"]) == {PRUEF} and wege["geloescht"] == [(PRUEF, "AAMk1")]
    assert PRUEF in erg["detail"]
    assert umgebung["MAILBOX_HEALTH"][HYBRID]["checks"]["regel_greift"]["status"] == "error"


def test_hybrid_ohne_anbindung_entfaellt_ohne_mail(wege, umgebung):
    umgebung["EWS_ONPREM_URL"] = ""
    rp.anmelden([HYBRID])
    erg = asyncio.run(rp.pruefen(HYBRID))
    assert erg["ergebnis"] == "nicht_moeglich" and erg["detail"] == rp.HYBRID_OHNE_EWS
    assert wege["ews"] == [] and wege["graph"] == []
    assert umgebung["MAILBOX_HEALTH"][HYBRID]["checks"]["regel_greift"]["status"] == "skip"


def test_ews_fehler_wird_gemeldet(wege, monkeypatch):
    def kaputt(*a):
        raise ews_onprem.EwsFehler("ErrorImpersonateUserDenied", "Scope")
    monkeypatch.setattr(ews_onprem, "regelprobe_senden", kaputt)
    erg = asyncio.run(rp._durchlauf(HYBRID, rp._plan(HYBRID)[1]))
    assert erg["ergebnis"] == "fehler" and "ErrorImpersonateUserDenied" in erg["detail"]
    assert rp._offen == {}


def test_route_lehnt_hybrid_nicht_ab(umgebung, monkeypatch):
    from webui.routen import mailboxes as mbroute
    gestartet = []
    monkeypatch.setattr(rp, "starten", lambda a: gestartet.extend(a) or
                        {"gestartet": list(a), "laufen_schon": []})
    umgebung["EWS_ONPREM_URL"] = ""
    r = asyncio.run(mbroute.api_regel_probe({"emails": [HYBRID, CLOUD]}))
    assert r["abgelehnt"] == {} and gestartet == [HYBRID, CLOUD]


# ── EWS-Anfrage ─────────────────────────────────────────────────────────────

def test_anfrage_regelprobe_xml():
    roh = ews_onprem.anfrage_regelprobe(HYBRID, PRUEF, "GW-Regelprobe <&> ä",
                                        rp.KOPF, "abc.def", "Text & <b>")
    root = ET.fromstring(roh)
    T, M = f"{{{ews_onprem.NS_T}}}", f"{{{ews_onprem.NS_M}}}"
    imp = root.find(f".//{T}ExchangeImpersonation//{T}PrimarySmtpAddress")
    assert imp.text == HYBRID, "gesendet ALS der Hybrid-Nutzer"
    ci = root.find(f".//{M}CreateItem")
    assert ci.get("MessageDisposition") == "SendOnly"
    assert ci.find(f"{M}SavedItemFolderId") is None, "keine Kopie in Gesendete Elemente"
    msg = ci.find(f"{M}Items/{T}Message")
    assert [c.tag.split("}")[1] for c in msg] == ["Subject", "Body", "ExtendedProperty",
                                                  "ToRecipients"], "Schema-Reihenfolge"
    assert msg.find(f"{T}Subject").text == "GW-Regelprobe <&> ä"
    assert msg.find(f"{T}Body").get("BodyType") == "Text"
    uri = msg.find(f"{T}ExtendedProperty/{T}ExtendedFieldURI")
    assert uri.attrib == {"DistinguishedPropertySetId": "InternetHeaders",
                          "PropertyName": rp.KOPF, "PropertyType": "String"}
    assert msg.find(f"{T}ExtendedProperty/{T}Value").text == "abc.def"
    an = msg.findall(f"{T}ToRecipients/{T}Mailbox/{T}EmailAddress")
    assert [a.text for a in an] == [PRUEF]


def test_regelprobe_senden_ohne_anbindung_kein_netz(monkeypatch):
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: None)

    def netz(*a, **k):
        raise AssertionError("Netzaufruf ohne Konfiguration")
    monkeypatch.setattr(ews_onprem, "_senden", netz)
    with pytest.raises(ews_onprem.EwsFehler) as e:
        ews_onprem.regelprobe_senden(HYBRID, PRUEF, "b", rp.KOPF, "x", "t")
    assert e.value.code == "konfiguration"


# ── Routing-Adresse der Koexistenz-Domäne (v1.12.72) ────────────────────────
#
# Lab 18.11.2026: Probe an die PRIMÄRE Adresse des reinen Cloud-Postfachs
# scheiterte lokal mit 550 5.1.10 RecipientNotFound (Domäne lokal
# Authoritative, kein MailUser). Ziel ist deshalb die …mail.onmicrosoft.com-
# Adresse, die der Hybrid-Send-Connector nach Exchange Online leitet.

@pytest.mark.parametrize("adressen,erwartet", [
    ([PRUEF, PRUEF_ALIAS], PRUEF_ALIAS),
    ([PRUEF_ALIAS.upper(), PRUEF], PRUEF_ALIAS),
    (["b@firma.mail.onmicrosoft.com", "a@firma.mail.onmicrosoft.com"], "a@firma.mail.onmicrosoft.com"),
    ([PRUEF, PRUEF_MOERA], PRUEF_MOERA),                           # Rückfall: Start-Domäne
    ([PRUEF_MOERA, PRUEF_ALIAS], PRUEF_ALIAS),                     # Koexistenz-Domäne geht vor
    (["x@onmicrosoft.com", "y@mail.onmicrosoft.com", "@firma.onmicrosoft.com"], ""),
    (["x@firma.onmicrosoft.com.boese.example"], ""),
    (["x@firma.mail.onmicrosoft.com.boese.example"], ""),           # nur als Endung
    (["x@mail.onmicrosoft.com.example", "@firma.mail.onmicrosoft.com"], ""),
    ([], ""), (None, ""),
])
def test_routing_adresse(adressen, erwartet):
    assert rp.routing_adresse(adressen) == erwartet


def test_plan_ohne_routing_adresse_entfaellt(umgebung):
    exo_mailboxes._cache[2]["addresses"] = [PRUEF]          # reines Cloud-Postfach ohne Koexistenz-Adresse
    g, plan = rp._plan(HYBRID)
    assert g == rp.HYBRID_OHNE_ROUTING and plan is None and rp.entfaellt(g)
    assert "mail.onmicrosoft.com" in g


def test_ohne_routing_adresse_keine_mail(wege, umgebung):
    exo_mailboxes._cache[2]["addresses"] = [PRUEF]
    rp.anmelden([HYBRID])
    erg = asyncio.run(rp.pruefen(HYBRID))
    assert erg["ergebnis"] == "nicht_moeglich" and erg["detail"] == rp.HYBRID_OHNE_ROUTING
    assert wege["ews"] == [] and wege["graph"] == []
    assert umgebung["MAILBOX_HEALTH"][HYBRID]["checks"]["regel_greift"]["status"] == "skip"


@pytest.mark.parametrize("rcpt_am_gateway", [PRUEF_ALIAS, PRUEF])
def test_bindung_routing_oder_umgeschriebene_primaere(umgebung, rcpt_am_gateway):
    """EXO kann den Umschlag-Empfänger lassen (Routing-Adresse) oder auf die
    primäre umschreiben — beides ist DASSELBE Postfach und wird erkannt."""
    _g, plan = rp._plan(HYBRID)
    assert plan["ziel"] in plan["empfaenger"]
    pid, geheim = rp.ausstellen(HYBRID, plan["absender"], plan["empfaenger"])
    erg = rp.erkennen(f"{pid}.{geheim}", "", HYBRID, [rcpt_am_gateway])
    assert erg and erg["probe_id"] == pid


def test_bindung_routing_fremdes_postfach_streng(umgebung):
    _g, plan = rp._plan(HYBRID)
    pid, geheim = rp.ausstellen(HYBRID, plan["absender"], plan["empfaenger"])
    for rcpt in (["fremd@firma.mail.onmicrosoft.com"], [FREMD_EXO], [PRUEF_ALIAS, FREMD_EXO]):
        assert rp.erkennen(f"{pid}.{geheim}", "", HYBRID, rcpt) is None


# ── Unzustellbarkeitsmeldung (NDR) der lokalen Exchange ─────────────────────

# Form wie live gelesen (Lab, Exchange SE, 18.11.2026), Adressen erfunden.
NDR_TEXT = (
    "Delivery has failed to these recipients or groups:\r\n\r\n"
    "gateway-meldungen@firma.at (gateway-meldungen@firma.at)\r\n"
    "The email address you entered couldn't be found.\r\n\r\n"
    "Diagnostic information for administrators:\r\n\r\n"
    "Generating server: ex01.firma.local\r\n\r\n"
    "gateway-meldungen@firma.at\r\n"
    "Remote Server returned '550 5.1.10 RESOLVER.ADR.RecipientNotFound; Recipient not "
    "found by SMTP address lookup'\r\n\r\nOriginal message headers:\r\n\r\n"
    "Received: from ex01.firma.local (10.99.0.5) by ex01.firma.local (10.99.0.5) with "
    "Microsoft SMTP Server (version=TLS1_2) id 15.2.2562.57; Wed, 18 Nov 2026 06:59:06 +0100\r\n")


@pytest.mark.parametrize("text,code", [
    (NDR_TEXT, "550 5.1.10 RESOLVER.ADR.RecipientNotFound"),
    ("Der Remoteserver hat '550 5.1.10 RESOLVER.ADR.RecipientNotFound; nicht gefunden' "
     "zurückgegeben", "550 5.1.10 RESOLVER.ADR.RecipientNotFound"),
    ("Remote Server returned &#39;554 5.4.14 Hop count exceeded&#39;", "554 5.4.14"),
    ("Status: 5.4.1 relay access denied", "5.4.1"),
    ("id 15.2.2562.57; Version 15.02.2562.057", ""),            # Versionsnummern sind kein Code
    ("", ""),
])
def test_ndr_code(text, code):
    assert rp.ndr_code(text) == code


def _ndr(pid, kennung="AAMkNDR", aenderung="CQAAAB"):
    return {"id": kennung, "aenderung": aenderung, "klasse": "REPORT.IPM.Note.NDR",
            "betreff": f"Undeliverable: {rp.BETREFF} {pid}.geheimnis", "text": NDR_TEXT}


def _mit_ndr(wege):
    """EWS-Senden legt sofort das NDR zur gerade gesendeten Probe an."""
    alt = ews_onprem.regelprobe_senden

    def senden(von, an, betreff, kopf, wert, text):
        alt(von, an, betreff, kopf, wert, text)
        wege["ndr"].append(_ndr(wert.split(".")[0]))
    return senden


def test_ndr_ergibt_nicht_pruefbar_und_wird_entfernt(wege, umgebung, monkeypatch):
    monkeypatch.setattr(ews_onprem, "regelprobe_senden", _mit_ndr(wege))
    rp.anmelden([HYBRID])
    erg = asyncio.run(rp.pruefen(HYBRID))
    assert erg["ergebnis"] == "nicht_pruefbar", erg
    assert erg["status"] == "info", "Lücke im Prüfweg: nie Warnung/Fehler für das Postfach"
    assert "550 5.1.10 RESOLVER.ADR.RecipientNotFound" in erg["detail"]
    assert PRUEF_ALIAS in erg["detail"] and "entfernt" in erg["detail"]
    assert wege["ndr_geloescht"] == [(HYBRID, "AAMkNDR", "CQAAAB")], "im LOKALEN Postfach"
    assert wege["ndr_gesucht"][0][0] == HYBRID
    assert wege["geloescht"] == [], "im Prüfpostfach wird nichts gelöscht"
    h = umgebung["MAILBOX_HEALTH"][HYBRID]
    assert h["checks"]["regel_greift"]["status"] == "info" and h["overall"] == "ok"
    assert rp._offen == {}


def test_ndr_loeschen_gescheitert_bleibt_info(wege, monkeypatch):
    monkeypatch.setattr(ews_onprem, "regelprobe_senden", _mit_ndr(wege))
    wege["ndr_loeschfehler"] = ews_onprem.EwsFehler("ErrorAccessDenied", "nein")
    erg = asyncio.run(rp._durchlauf(HYBRID, rp._plan(HYBRID)[1]))
    assert erg["ergebnis"] == "nicht_pruefbar" and erg["status"] == "info"
    assert "gescheitert" in erg["detail"] and wege["ndr_geloescht"] == []


def test_fremdes_ndr_zaehlt_nicht(wege):
    """NDR einer ANDEREN Probe (andere Kennung im Betreff) — weder Ergebnis
    noch Löschen."""
    wege["ndr"].append(_ndr("0123456789ab"))
    erg = asyncio.run(rp._durchlauf(HYBRID, rp._plan(HYBRID)[1]))
    assert erg["ergebnis"] == "unklar" and wege["ndr_geloescht"] == []
    assert wege["ndr_gesucht"], "es wurde gesucht"


def test_ndr_suchfehler_bricht_nicht_ab(wege):
    wege["ndr_suchfehler"] = ews_onprem.EwsFehler("ErrorServerBusy", "busy")
    erg = asyncio.run(rp._durchlauf(HYBRID, rp._plan(HYBRID)[1]))
    assert erg["ergebnis"] == "unklar" and rp._offen == {}


def test_cloud_sucht_kein_ndr(wege):
    erg = asyncio.run(rp._durchlauf(CLOUD))
    assert erg["ergebnis"] == "unklar" and wege["ndr_gesucht"] == []


# ── EWS: NDR lesen und löschen ──────────────────────────────────────────────

T_, M_ = ews_onprem.NS_T, ews_onprem.NS_M


def _getitem_antwort(elemente: list[tuple[str, str, str]]) -> ET.Element:
    """[(ItemClass, Betreff, Text)] → GetItemResponse mit je einer ResponseMessage."""
    teile = []
    for i, (klasse, betreff, text) in enumerate(elemente):
        teile.append(
            f'<m:GetItemResponseMessage ResponseClass="Success"><m:ResponseCode>NoError'
            f'</m:ResponseCode><m:Items><t:Message><t:ItemId Id="id{i}" ChangeKey="ck{i}"/>'
            f'<t:ItemClass>{klasse}</t:ItemClass><t:Subject>{betreff}</t:Subject>'
            f'<t:Body BodyType="Text">{text}</t:Body></t:Message></m:Items>'
            f'</m:GetItemResponseMessage>')
    roh = (f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" xmlns:m="{M_}" '
           f'xmlns:t="{T_}"><s:Body><m:GetItemResponse><m:ResponseMessages>{"".join(teile)}'
           f'</m:ResponseMessages></m:GetItemResponse></s:Body></s:Envelope>')
    return ews_onprem.antwort_pruefen(200, roh, teilweise=True)


def test_ndr_aus_antwort_nur_ndr():
    root = _getitem_antwort([
        ("REPORT.IPM.Note.NDR", "Undeliverable: GW-Regelprobe abc", "550 5.1.10 x"),
        ("IPM.Note", "GW-Regelprobe abc", "eine normale Mail"),
        ("REPORT.IPM.Note.DR", "Delivered: GW-Regelprobe abc", "zugestellt"),
        ("report.ipm.note.ndr", "Unzustellbar: GW-Regelprobe abc", "&lt;b&gt; &amp;"),
    ])
    erg = ews_onprem.ndr_aus_antwort(root)
    assert [(e["id"], e["aenderung"]) for e in erg] == [("id0", "ck0"), ("id3", "ck3")]
    assert erg[1]["text"] == "<b> &" and erg[0]["betreff"].startswith("Undeliverable")


def test_anfrage_ndr_holen_xml():
    root = ET.fromstring(ews_onprem.anfrage_ndr_holen(HYBRID, [("i1", "c1"), ("i2", "")]))
    form = root.find(f".//{{{M_}}}GetItem/{{{M_}}}ItemShape")
    assert form.find(f"{{{T_}}}BodyType").text == "Text"
    uris = [f.get("FieldURI") for f in form.iter(f"{{{T_}}}FieldURI")]
    assert "item:ItemClass" in uris and "item:Body" in uris
    ids = root.findall(f".//{{{M_}}}ItemIds/{{{T_}}}ItemId")
    assert [i.attrib for i in ids] == [{"Id": "i1", "ChangeKey": "c1"}, {"Id": "i2"}]


def test_anfrage_loeschen_xml_softdelete_mit_impersonation():
    root = ET.fromstring(ews_onprem.anfrage_loeschen(HYBRID, "AAMk<&>", "CQ"))
    imp = root.find(f".//{{{T_}}}ExchangeImpersonation//{{{T_}}}PrimarySmtpAddress")
    assert imp.text == HYBRID, "gelöscht im Postfach DES Hybrid-Nutzers"
    di = root.find(f".//{{{M_}}}DeleteItem")
    assert di.get("DeleteType") == "SoftDelete"
    ids = di.findall(f"{{{M_}}}ItemIds/{{{T_}}}ItemId")
    assert [i.attrib for i in ids] == [{"Id": "AAMk<&>", "ChangeKey": "CQ"}], "genau EIN Element"


def test_ndr_loeschen_ohne_anbindung_oder_kennung_kein_netz(monkeypatch):
    def netz(*a, **k):
        raise AssertionError("Netzaufruf")
    monkeypatch.setattr(ews_onprem, "_senden", netz)
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: None)
    with pytest.raises(ews_onprem.EwsFehler) as e:
        ews_onprem.regelprobe_ndr_loeschen(HYBRID, "AAMk1")
    assert e.value.code == "konfiguration"
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: KONFIG.get(k, d))
    with pytest.raises(ews_onprem.EwsFehler) as e:
        ews_onprem.regelprobe_ndr_loeschen(HYBRID, "")
    assert e.value.code == "kennung"


def test_rueckfall_auf_start_domaene(wege, umgebung, monkeypatch):
    """Ohne Koexistenz-Adresse: Probe an …@<tenant>.onmicrosoft.com (smtp:*-
    Connector); die Bindung bleibt an ALLE Adressen des Prüfpostfachs."""
    exo_mailboxes._cache[2]["addresses"] = [PRUEF, PRUEF_MOERA]
    g, plan = rp._plan(HYBRID)
    assert g == "" and plan["ziel"] == PRUEF_MOERA
    assert set(plan["empfaenger"]) == {PRUEF, PRUEF_MOERA}
    wege["ankunft"] = False
    rp.anmelden([HYBRID])

    def senden(von, an, betreff, kopf, wert, text):
        wege["ews"].append((von, an))
        rp.erkennen(wert, betreff, HYBRID, [PRUEF])      # EXO schreibt ggf. auf die primäre um
    monkeypatch.setattr(ews_onprem, "regelprobe_senden", senden)
    erg = asyncio.run(rp.pruefen(HYBRID))
    assert erg["ergebnis"] == "greift" and wege["ews"] == [(HYBRID, PRUEF_MOERA)]
