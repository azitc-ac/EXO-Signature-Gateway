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
         "posteingang": None}

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
    monkeypatch.setattr(ews_onprem, "regelprobe_senden", ews_senden)
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
    assert (von, an, kopf) == (HYBRID, PRUEF, rp.KOPF) and wert in betreff
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
