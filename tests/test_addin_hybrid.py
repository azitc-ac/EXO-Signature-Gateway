"""Outlook-Add-in für Hybrid-Nutzer (seit v1.12.69).

Hybrid-Nutzer haben ihr Postfach in der lokalen Exchange-Organisation; in EXO
sind sie nur ein MailUser. Bis v1.12.67 war das Add-in für sie gesperrt
(Auswahl verworfen, Spalte ausgegraut, Prüfung „entfällt“). Geprüft:

* Signatur liefern geht ohne Postfach (Entra-Daten), auch für Hybrid-Nutzer.
* Antworten: EXO-Postfach → Graph (unverändert); Hybrid mit Anbindung → EWS
  (Message-ID in Posteingang + Gesendete Elemente), ausgewertet von DERSELBEN
  Stelle wie Graph; Hybrid ohne Anbindung oder ohne inReplyTo → kein
  Graph-Abruf, Entwurf leer, das Gateway entscheidet beim Versand.
* Ein Abruf eines Hybrid-Nutzers wird gemerkt (Beleg der lokalen
  Bereitstellung, Postfachprüfung); einer aus EXO nicht.
* Befehle für Exchange lokal: Parameter laut MS Learn (New-App/Remove-App),
  Werte nur als Literal.
* Self-Service: Abwesenheit für Hybrid ohne Anbindung ausgeblendet.

Testdaten erfunden (firma.at, Max Mustermann).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import ews_onprem
import exo_mailboxes
import graph_client
import policies
import settings_store
import sig_thread
import signature_engine
from webui.routen import addin, addin_verfassen as av

from test_addin_verfassen import _Req, _token, umgebung  # noqa: F401

WURZEL = Path(__file__).resolve().parent.parent
NS = {"s": ews_onprem.NS_SOAP, "t": ews_onprem.NS_T, "m": ews_onprem.NS_M}
HYBRID = "max.mustermann@firma.at"
CLOUD = "erika.musterfrau@firma.at"
EWS = {"EWS_ONPREM_URL": "https://exchange.firma.at/EWS/Exchange.asmx",
       "EWS_ONPREM_KONTO": "FIRMA\\svc-gateway", "EWS_ONPREM_KENNWORT": "nur-ein-testwert"}
MID = "<beantwortet-1@extern.example>"
T = lambda n: f"{{{ews_onprem.NS_T}}}{n}"  # noqa: E731
M = lambda n: f"{{{ews_onprem.NS_M}}}{n}"  # noqa: E731


# ── Attrappe: lokale Exchange mit Posteingang und Gesendete Elemente ────────

class FalschesPostfach:
    """FindItem über mehrere Ordner (je Ordner eine ResponseMessage, wie
    Exchange) und GetItem mit IsDraft/References/InReplyTo. Wertet die
    Restriction selbst aus — der Test prüft so, was die Anfrage verlangt."""

    def __init__(self):
        self.mails: list[dict] = []          # {id, ordner, mid, refs, irt, entwurf, html, von}
        self.anfragen: list[ET.Element] = []
        self.kaputt: set[str] = set()        # Ordner, die mit Fehler antworten

    def __call__(self, ziel, umschlag):
        root = ET.fromstring(umschlag)
        self.anfragen.append(root)
        assert root.find("s:Header/t:ExchangeImpersonation/t:ConnectingSID/t:PrimarySmtpAddress",
                         NS).text == ziel
        env = ET.Element(f"{{{ews_onprem.NS_SOAP}}}Envelope")
        body = ET.SubElement(env, f"{{{ews_onprem.NS_SOAP}}}Body")
        fi = root.find(".//m:FindItem", NS)
        if fi is not None:
            gleich = fi.find("m:Restriction/t:IsEqualTo", NS)
            assert gleich.find("t:FieldURI", NS).get("FieldURI") == "message:InternetMessageId"
            wert = gleich.find("t:FieldURIOrConstant/t:Constant", NS).get("Value")
            rms = ET.SubElement(ET.SubElement(body, M("FindItemResponse")), M("ResponseMessages"))
            for o in fi.findall("m:ParentFolderIds/t:DistinguishedFolderId", NS):
                assert o.find("t:Mailbox/t:EmailAddress", NS).text == ziel
                if o.get("Id") in self.kaputt:
                    rm = ET.SubElement(rms, M("FindItemResponseMessage"), {"ResponseClass": "Error"})
                    ET.SubElement(rm, M("ResponseCode")).text = "ErrorFolderNotFound"
                    continue
                rm = ET.SubElement(rms, M("FindItemResponseMessage"), {"ResponseClass": "Success"})
                ET.SubElement(rm, M("ResponseCode")).text = "NoError"
                items = ET.SubElement(ET.SubElement(rm, M("RootFolder")), T("Items"))
                for m in self.mails:
                    if m["ordner"] == o.get("Id") and m["mid"] == wert:
                        ET.SubElement(ET.SubElement(items, T("Message")), T("ItemId"),
                                      {"Id": m["id"], "ChangeKey": "CK"})
            return 200, ET.tostring(env, encoding="utf-8")
        gi = root.find(".//m:GetItem", NS)
        assert gi is not None, "unerwartete Anfrage"
        felder = {f.get("FieldURI") for f in gi.findall("m:ItemShape/t:AdditionalProperties/t:FieldURI", NS)}
        assert {"item:IsDraft", "message:References", "item:InReplyTo"} <= felder
        rms = ET.SubElement(ET.SubElement(body, M("GetItemResponse")), M("ResponseMessages"))
        for i in gi.findall("m:ItemIds/t:ItemId", NS):
            m = next(x for x in self.mails if x["id"] == i.get("Id"))
            rm = ET.SubElement(rms, M("GetItemResponseMessage"), {"ResponseClass": "Success"})
            ET.SubElement(rm, M("ResponseCode")).text = "NoError"
            msg = ET.SubElement(ET.SubElement(rm, M("Items")), T("Message"))
            ET.SubElement(msg, T("ItemId"), {"Id": m["id"]})
            ET.SubElement(msg, T("Body"), {"BodyType": "HTML"}).text = m.get("html", "<p>x</p>")
            ET.SubElement(msg, T("DateTimeReceived")).text = "2026-11-18T09:00:00Z"
            mb = ET.SubElement(ET.SubElement(msg, T("From")), T("Mailbox"))
            ET.SubElement(mb, T("EmailAddress")).text = m.get("von", "kunde@extern.example")
            ET.SubElement(msg, T("InternetMessageId")).text = m["mid"]
            ET.SubElement(msg, T("IsDraft")).text = "true" if m.get("entwurf") else "false"
            if m.get("refs"):
                ET.SubElement(msg, T("References")).text = m["refs"]
            if m.get("irt"):
                ET.SubElement(msg, T("InReplyTo")).text = m["irt"]
        return 200, ET.tostring(env, encoding="utf-8")


@pytest.fixture
def lage(umgebung, monkeypatch):  # noqa: F811
    """Hybrid- und Cloud-Postfach; Graph-Abrufe der Antwort-Erkennung mitgeschrieben."""
    umgebung["MAILBOX_CONFIG"] = {
        "g-hybrid": {"sig": True, "hybrid": True, "primary": HYBRID, "known_addresses": [HYBRID]},
        "g-cloud": {"sig": True, "primary": CLOUD, "known_addresses": [CLOUD]}}
    monkeypatch.setattr(exo_mailboxes, "_cache", [
        {"primary": CLOUD, "addresses": [CLOUD], "type": "UserMailbox", "guid": "g-cloud"}])
    monkeypatch.setattr(exo_mailboxes, "_cache_ts", time.monotonic())
    ews = FalschesPostfach()
    monkeypatch.setattr(ews_onprem, "_senden", ews)
    graph = []

    async def _per_kennung(p, mid):
        graph.append(("kennung", p, mid))
        return {"kennungen": [mid, "<eigene@firma.at>"], "html": "", "von": "k@x", "zeit": "2026-11-18"}

    async def _konv(p, konv, *a, **kw):
        graph.append(("konversation", p, konv))
        return None

    async def _betreff(p, betreff, an=None):
        graph.append(("betreff", p, betreff))
        return None
    monkeypatch.setattr(graph_client, "bezug_per_kennung", _per_kennung)
    monkeypatch.setattr(graph_client, "antwort_bezug", _konv)
    monkeypatch.setattr(graph_client, "konversation_per_betreff", _betreff)

    async def _user(email):
        return graph_client.UserData(mail=email)
    monkeypatch.setattr(graph_client, "get_user", _user)
    monkeypatch.setattr(signature_engine, "render",
                        lambda u, t=None, extra=None, template_name=None: (f"<p>SIG {t or template_name}</p>", ""))
    monkeypatch.setattr(policies, "aufloesen", lambda e, *a, **kw: {
        "sig": "Voll", "min": "Kurz", "oof": "", "banner": "", "disclaimer": "", "intern": ""})
    bekannt: set = set()
    monkeypatch.setattr(sig_thread, "kennt", lambda p, refs: bool(set(refs or []) & bekannt))
    return umgebung, ews, graph, bekannt


def _abruf(wer, **q):
    req = _Req("Bearer " + _token(preferred_username=wer))
    req.query_params = {"client": "PC", **q}
    return json.loads(asyncio.run(av.verfassen_signatur(req)).body)


# ── Neue Mail: Signatur ohne Postfach ────────────────────────────────────────

def test_neue_mail_hybrid_bekommt_signatur_und_abruf_wird_gemerkt(lage):
    store, ews, graph, _ = lage
    body = _abruf(HYBRID)
    assert body["html"] == addin.markiert("<p>SIG Voll</p>")
    assert graph == [] and ews.anfragen == []
    stand = store["_ADDIN_LOKAL"]["g-hybrid"]["verfassen"]
    assert stand["client"] == "PC" and len(stand["tag"]) == 10


def test_neue_mail_cloud_wird_nicht_gemerkt(lage):
    store, _, _, _ = lage
    assert _abruf(CLOUD)["html"]
    assert "_ADDIN_LOKAL" not in store, "EXO-Postfächer: Fassung kommt aus Get-App"


def test_abruf_schreibt_hoechstens_einmal_je_tag(lage, monkeypatch):
    store, _, _, _ = lage
    schreiben = []
    monkeypatch.setattr(settings_store, "force_update", lambda p: schreiben.append(p) or store.update(p))
    for _ in range(3):
        _abruf(HYBRID)
    assert len(schreiben) == 1


# ── Antworten ────────────────────────────────────────────────────────────────

def test_hybrid_mit_anbindung_sucht_die_beantwortete_mail_ueber_ews(lage):
    store, ews, graph, bekannt = lage
    store.update(EWS)
    ews.mails = [{"id": "I1", "ordner": "sentitems", "mid": MID, "refs": "<alt@firma.at>"}]
    bekannt.add(MID)                       # die beantwortete Mail ist eine signierte eigene
    body = _abruf(HYBRID, art="reply", bezug=MID)
    assert body["kette"] is True and body["html"] == addin.markiert("<p>SIG Kurz</p>")
    assert graph == [], "kein Graph-Abruf für ein Postfach in der lokalen Organisation"
    fi = ews.anfragen[0].find(".//m:FindItem", NS)
    assert [o.get("Id") for o in fi.findall("m:ParentFolderIds/t:DistinguishedFolderId", NS)] \
        == ["inbox", "sentitems"]


def test_ews_treffer_ergibt_dieselbe_kette_wie_graph(lage):
    store, ews, _, _ = lage
    store.update(EWS)
    ews.mails = [{"id": "I0", "ordner": "inbox", "mid": MID, "entwurf": True},
                 {"id": "I1", "ordner": "inbox", "mid": MID, "refs": "<a@x> <b@x>", "irt": "<b@x>"}]
    treffer = ews_onprem.bezug_per_kennung(HYBRID, MID)
    assert [t["isDraft"] for t in treffer] == [True, False]
    bezug = graph_client.bezug_aus_treffern(treffer)
    assert bezug["kennungen"][0] == MID and {"<a@x>", "<b@x>"} <= set(bezug["kennungen"])
    graph_form = [{"internetMessageId": MID, "isDraft": False, "body": {"content": "<p>x</p>"},
                   "from": {"emailAddress": {"address": "kunde@extern.example"}},
                   "receivedDateTime": "2026-11-18T09:00:00Z",
                   "internetMessageHeaders": [{"name": "References", "value": "<a@x> <b@x>"},
                                              {"name": "In-Reply-To", "value": "<b@x>"}]}]
    assert bezug == graph_client.bezug_aus_treffern(graph_form), "EINE Auswertung für beide Wege"


def test_ein_kaputter_ordner_nimmt_den_anderen_nicht_mit(lage):
    store, ews, _, _ = lage
    store.update(EWS)
    ews.kaputt = {"inbox"}
    ews.mails = [{"id": "I1", "ordner": "sentitems", "mid": MID}]
    assert [t["internetMessageId"] for t in ews_onprem.bezug_per_kennung(HYBRID, MID)] == [MID]


def test_message_id_mit_sonderzeichen_bleibt_ein_wert(lage):
    store, ews, _, _ = lage
    store.update(EWS)
    boese = "<a'b\"c&d<e>@x.example>"
    assert ews_onprem.bezug_per_kennung(HYBRID, boese) == []
    wert = ews.anfragen[0].find(".//t:IsEqualTo/t:FieldURIOrConstant/t:Constant", NS).get("Value")
    assert wert == boese
    assert len(ews.anfragen) == 1, "ohne Treffer kein GetItem"


def test_hybrid_nicht_gefunden_keine_schaetzung_entwurf_leer(lage, caplog):
    store, ews, graph, _ = lage
    store.update(EWS)
    with caplog.at_level(logging.INFO):
        body = _abruf(HYBRID, art="reply", bezug=MID, konversation="AAQkAGI2", betreff="AW: Test",
                      an="kunde@extern.example")
    assert body == {"html": ""}
    assert graph == [], "Schätzung (Konversation/Betreff) liefe über Graph — entfällt"
    assert any("Schätzung über Konversation/Betreff entfällt" in r.getMessage() for r in caplog.records)


def test_hybrid_ohne_anbindung_gar_kein_postfachabruf(lage):
    _, ews, graph, _ = lage
    assert _abruf(HYBRID, art="reply", bezug=MID, konversation="AAQkAGI2") == {"html": ""}
    assert graph == [] and ews.anfragen == []


def test_ews_fehler_entwurf_leer_kein_absturz(lage, monkeypatch):
    store, _, graph, _ = lage
    store.update(EWS)

    def kaputt(ziel, umschlag):
        raise ews_onprem.EwsFehler("verbindung", "Exchange nicht erreichbar")
    monkeypatch.setattr(ews_onprem, "_senden", kaputt)
    assert _abruf(HYBRID, art="reply", bezug=MID) == {"html": ""}
    assert graph == []


# ── Gegenprobe Cloud: unverändert Graph, nie EWS ─────────────────────────────

def test_cloud_antwort_geht_unveraendert_ueber_graph_auch_mit_anbindung(lage):
    store, ews, graph, bekannt = lage
    store.update(EWS)
    bekannt.add(MID)
    body = _abruf(CLOUD, art="reply", bezug=MID)
    assert graph == [("kennung", CLOUD, MID)] and ews.anfragen == []
    assert body["kette"] is True


def test_cloud_schaetzung_ueber_konversation_bleibt(lage):
    store, ews, graph, _ = lage
    store.update(EWS)
    assert _abruf(CLOUD, art="reply", konversation="AAQkAGI2") == {"html": ""}
    assert graph == [("konversation", CLOUD, "AAQkAGI2")] and ews.anfragen == []


# ── Knopf (Aufgabenbereich) ──────────────────────────────────────────────────

class _Sitzung:
    headers: dict = {}
    cookies: dict = {}


def test_knopf_hybrid_liefert_und_merkt(lage):
    store, _, _, _ = lage
    r = json.loads(asyncio.run(addin.api_addin_signature(_Sitzung(), HYBRID, "", user=HYBRID)).body)
    assert r["marked_html"]
    assert store["_ADDIN_LOKAL"]["g-hybrid"]["knopf"]["tag"]


# ── Postfachprüfung ──────────────────────────────────────────────────────────

def test_pruefung_hybrid_nennt_den_letzten_abruf(lage):
    import health_check
    store, _, _, _ = lage
    store["ADDIN_ENABLED"] = True
    erg = health_check._checks_addin_lokal(HYBRID)
    assert erg["addin_verfassen"]["status"] == "skip"
    assert "Exchange lokal" in erg["addin_verfassen"]["detail"]
    assert "soll" not in erg["addin_verfassen"], "Fassungsprüfung der Abnahme zählt sie nicht"
    _abruf(HYBRID)
    erg = health_check._checks_addin_lokal(HYBRID)
    assert erg["addin_verfassen"]["status"] == "ok" and "abgerufen am" in erg["addin_verfassen"]["detail"]
    assert erg["addin_knopf"]["status"] == "skip"
    store["ADDIN_ENABLED"] = False
    assert health_check._checks_addin_lokal(HYBRID)["addin_verfassen"]["detail"] == "Nicht eingerichtet"


def test_checks_hybrid_nutzt_die_abrufpruefung():
    quelle = (WURZEL / "app/health_check.py").read_text("utf-8")
    rumpf = quelle.split("async def _checks_hybrid(")[1].split("\n# ──")[0]
    assert "_checks_addin_lokal(email)" in rumpf
    assert '"addin_verfassen", "addin_knopf"' not in rumpf, "nicht mehr pauschal „entfällt“"


# ── Befehle für Exchange lokal ───────────────────────────────────────────────

def test_befehle_fuer_exchange_lokal(lage):
    befehle = {b["art"]: b for b in addin.lokal_bereitstellung("https://sig.firma.at")}
    assert set(befehle) == {"verfassen", "knopf"}
    v = befehle["verfassen"]
    assert v["neu_url"] == ("New-App -OrganizationApp -Url 'https://sig.firma.at/addin/verfassen/"
                            "manifest.xml' -ProvidedTo Everyone -DefaultStateForUser Enabled")
    assert "-FileData ([System.IO.File]::ReadAllBytes('C:\\Temp\\exo-signatur-verfassen.xml'))" \
        in v["neu_datei"]
    assert v["kennung"] == av.addin_id("sig.firma.at")
    assert v["entfernen"] == f"Remove-App -Identity '{v['kennung']}' -OrganizationApp -Confirm:$false"
    assert befehle["knopf"]["kennung"] == addin.knopf_addin_id("sig.firma.at")


def test_befehle_maskieren_die_adresse(lage):
    b = addin.lokal_bereitstellung("https://sig.fir'ma.at")[0]
    assert "'https://sig.fir''ma.at/addin/verfassen/manifest.xml'" in b["neu_url"]


def test_einrichtung_zeigt_befehle_nur_mit_hybrid(lage):
    from webui.routen import setup
    store, _, _, _ = lage
    assert setup._addin_lokal("https://sig.firma.at"), "Hybrid-Eintrag vorhanden"
    store["MAILBOX_CONFIG"] = {"g-cloud": {"sig": True, "primary": CLOUD}}
    assert setup._addin_lokal("https://sig.firma.at") == []
    store["HYBRID_POSTFAECHER"] = True
    assert setup._addin_lokal("https://sig.firma.at")


# ── Speichern und Oberfläche ─────────────────────────────────────────────────

def test_speichern_verwirft_die_addin_auswahl_nicht_mehr():
    quelle = (WURZEL / "app/webui/routen/mailboxes.py").read_text("utf-8")
    assert 'entry.pop("addin_templates"' not in quelle


def test_oberflaeche_sperrt_die_addin_spalte_nicht_fuer_hybrid():
    html = (WURZEL / "app/webui/templates/mailboxes.html").read_text("utf-8")
    zeile = [z for z in html.splitlines() if "var addinAus" in z]
    assert zeile == ["      var addinAus = !sig || pol;"]
    assert "addinCtrl.style.pointerEvents = 'none'; addinCtrl.style.opacity = '0.4';\n" \
           "      addinCtrl.title = HYBRID_HINWEIS" not in html


# ── Self-Service ─────────────────────────────────────────────────────────────

def test_abwesenheit_je_postfach(lage):
    import abwesenheit
    store, _, _, _ = lage
    store["ABWESENHEIT_AKTIV"] = True
    assert abwesenheit.fuer_postfach(CLOUD) is True
    assert abwesenheit.fuer_postfach(HYBRID) is False
    store.update(EWS)
    assert abwesenheit.fuer_postfach(HYBRID) is True
    store["ABWESENHEIT_AKTIV"] = False
    assert abwesenheit.fuer_postfach(CLOUD) is False


def test_selfservice_nutzt_die_abwesenheit_je_postfach():
    quelle = (WURZEL / "app/webui/routen/selfservice.py").read_text("utf-8")
    assert "abwesenheit.funktion_aktiv()" not in quelle, \
        "Self-Service fragt je Postfach (abwesenheit.fuer_postfach)"
