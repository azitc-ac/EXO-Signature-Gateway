"""S/MIME für Hybrid-Nutzer (seit v1.12.62).

Hybrid-Nutzer haben ihr Postfach in der lokalen Exchange-Organisation; in EXO
sind sie nur ein MailUser. Signieren, Verschlüsseln und Entschlüsseln laufen am
Gateway, der Rückweg ist der Smarthost — das geht für sie wie für jedes
Postfach. Nur der Zertifikatsbezug liest das Postfach (CASTLE: Challenge-Mail,
Hub: Bestätigungslink). Dafür:

* EINE Weiche (`ews_onprem.postfach_weg`): EXO-Postfach → Graph (unverändert),
  Hybrid mit Anbindung → EWS, Hybrid ohne Anbindung → gesperrt.
* Der EWS-Weg liefert die Form des Graph-Abrufs; die Auswertung (Betreff,
  Absender der CA, Link/Challenge im Text) bleibt EINE.
* Ohne Anbindung: Bestellung über einen Bezugsweg, der das Postfach liest,
  im Kern gesperrt (einwilligung.verlangen) — mit Grund, nicht als
  „Einwilligung fehlt“. Import geht.
* Gegenprobe Cloud: nie EWS, Graph-Abruf byte-gleich.

Testdaten erfunden (firma.at, Max Mustermann).
"""
from __future__ import annotations

import ast
import asyncio
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import ews_onprem
import exo_mailboxes
import settings_store

WURZEL = Path(__file__).resolve().parent.parent
NS = {"s": ews_onprem.NS_SOAP, "t": ews_onprem.NS_T, "m": ews_onprem.NS_M}
HYBRID = "max.mustermann@firma.at"
CLOUD = "erika.musterfrau@firma.at"
KONFIG = {"EWS_ONPREM_URL": "https://exchange.firma.at/EWS/Exchange.asmx",
          "EWS_ONPREM_KONTO": "FIRMA\\svc-gateway",
          "EWS_ONPREM_KENNWORT": "nur-ein-testwert"}
LINK = ("https://certmanager.test.example/emailVerification?themeId=default"
        "&partnerId=0123456789abcdef0123456789abcdef01234567&verificationId=AbCdEf"
        "GhIjKlMnOpQrStUvWxYz0123456789&language=en")


def _jetzt(**delta) -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0) + timedelta(**delta)


def _z(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Attrappe: eine lokale Exchange mit Posteingang ───────────────────────────

class FalscherPosteingang:
    """Beantwortet FindItem und GetItem wie Exchange SE (Standard-Namensräume,
    CR im Text als &#xD;). Wertet Restriction und Sortierung selbst aus — so
    prüft der Test, dass die Anfrage das Gewollte verlangt."""

    def __init__(self):
        self.mails: list[dict] = []
        self.anfragen: list[bytes] = []
        self.weg: set[str] = set()          # Kennungen „zwischen Suche und Abruf gelöscht“

    def __call__(self, ziel, umschlag):
        self.anfragen.append(umschlag)
        root = ET.fromstring(umschlag)
        assert root.find("s:Header/t:ExchangeImpersonation/t:ConnectingSID/t:PrimarySmtpAddress",
                         NS).text == ziel
        if root.find(".//m:FindItem", NS) is not None:
            return 200, self._finden(root)
        if root.find(".//m:GetItem", NS) is not None:
            return 200, self._holen(root)
        raise AssertionError("unerwartete Anfrage")

    def _finden(self, root):
        fi = root.find(".//m:FindItem", NS)
        assert fi.find("m:ParentFolderIds/t:DistinguishedFolderId", NS).get("Id") == "inbox"
        anzahl = int(fi.find("m:IndexedPageItemView", NS).get("MaxEntriesReturned"))
        ab, teil = None, None
        for v in fi.iter(f"{{{ews_onprem.NS_T}}}IsGreaterThanOrEqualTo"):
            assert v.find("t:FieldURI", NS).get("FieldURI") == "item:DateTimeReceived"
            ab = v.find("t:FieldURIOrConstant/t:Constant", NS).get("Value")
        for v in fi.iter(f"{{{ews_onprem.NS_T}}}Contains"):
            assert v.get("ContainmentComparison") == "IgnoreCase"
            teil = v.find("t:Constant", NS).get("Value")
        assert fi.find("m:SortOrder/t:FieldOrder", NS).get("Order") == "Descending"
        treffer = sorted(self.mails, key=lambda m: m["zeit"], reverse=True)
        if ab:
            treffer = [m for m in treffer if m["zeit"] >= ab]
        if teil:
            treffer = [m for m in treffer if teil.lower() in m["betreff"].lower()]
        env, rm = self._antwort("FindItemResponse", "FindItemResponseMessage")
        ordner = ET.SubElement(rm, f"{{{ews_onprem.NS_M}}}RootFolder")
        items = ET.SubElement(ordner, f"{{{ews_onprem.NS_T}}}Items")
        for m in treffer[:anzahl]:
            msg = ET.SubElement(items, f"{{{ews_onprem.NS_T}}}Message")
            ET.SubElement(msg, f"{{{ews_onprem.NS_T}}}ItemId", {"Id": m["id"], "ChangeKey": "CK"})
        return self._fertig(env)

    def _holen(self, root):
        t = lambda n: f"{{{ews_onprem.NS_T}}}{n}"  # noqa: E731
        ids = [i.get("Id") for i in root.find(".//m:GetItem/m:ItemIds", NS)]
        env = ET.Element(f"{{{ews_onprem.NS_SOAP}}}Envelope")
        body = ET.SubElement(env, f"{{{ews_onprem.NS_SOAP}}}Body")
        r = ET.SubElement(body, f"{{{ews_onprem.NS_M}}}GetItemResponse")
        rms = ET.SubElement(r, f"{{{ews_onprem.NS_M}}}ResponseMessages")
        for kennung in ids:
            m = next(x for x in self.mails if x["id"] == kennung)
            if kennung in self.weg:
                rm = ET.SubElement(rms, f"{{{ews_onprem.NS_M}}}GetItemResponseMessage",
                                   {"ResponseClass": "Error"})
                ET.SubElement(rm, f"{{{ews_onprem.NS_M}}}ResponseCode").text = "ErrorItemNotFound"
                ET.SubElement(rm, f"{{{ews_onprem.NS_M}}}Items")
                continue
            rm = ET.SubElement(rms, f"{{{ews_onprem.NS_M}}}GetItemResponseMessage",
                               {"ResponseClass": "Success"})
            ET.SubElement(rm, f"{{{ews_onprem.NS_M}}}ResponseCode").text = "NoError"
            items = ET.SubElement(rm, f"{{{ews_onprem.NS_M}}}Items")
            msg = ET.SubElement(items, t(m.get("art", "Message")))
            ET.SubElement(msg, t("ItemId"), {"Id": m["id"], "ChangeKey": "CK"})
            ET.SubElement(msg, t("Subject")).text = m["betreff"]
            ET.SubElement(msg, t("Body"), {"BodyType": m.get("text_art", "HTML")}).text = m["text"]
            ET.SubElement(msg, t("DateTimeReceived")).text = m["zeit"]
            mb = ET.SubElement(ET.SubElement(msg, t("From")), t("Mailbox"))
            ET.SubElement(mb, t("Name")).text = m["von"]
            ET.SubElement(mb, t("EmailAddress")).text = m["von"]
            ET.SubElement(mb, t("RoutingType")).text = m.get("routing", "SMTP")
            ET.SubElement(msg, t("InternetMessageId")).text = m.get("mid", "<x@firma.at>")
        roh = ET.tostring(env, encoding="utf-8", xml_declaration=True)
        # Wie Exchange: CR im Text als Zeichenreferenz (xml.etree schreibt CR roh,
        # ein Parser machte daraus LF — dann prüfte der Test nicht, was live kommt).
        return roh.replace(b"\r", b"&#xD;")

    def _antwort(self, name, rm_name):
        env = ET.Element(f"{{{ews_onprem.NS_SOAP}}}Envelope")
        body = ET.SubElement(env, f"{{{ews_onprem.NS_SOAP}}}Body")
        r = ET.SubElement(body, f"{{{ews_onprem.NS_M}}}{name}")
        rms = ET.SubElement(r, f"{{{ews_onprem.NS_M}}}ResponseMessages")
        rm = ET.SubElement(rms, f"{{{ews_onprem.NS_M}}}{rm_name}", {"ResponseClass": "Success"})
        ET.SubElement(rm, f"{{{ews_onprem.NS_M}}}ResponseCode").text = "NoError"
        return env, rm

    @staticmethod
    def _fertig(env):
        return ET.tostring(env, encoding="utf-8", xml_declaration=True)


@pytest.fixture
def lage(monkeypatch, tmp_path):
    """Ein EXO-Postfach (Cloud) und ein Hybrid-Nutzer; Einstellungen als
    Wörterbuch, EWS als Attrappe, Graph (httpx) als Attrappe mit Mitschrift."""
    store = {"MAILBOX_CONFIG": {
        "g-hybrid": {"sig": True, "smime": True, "hybrid": True, "primary": HYBRID},
        "g-cloud": {"sig": True, "smime": True, "primary": CLOUD}}}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(exo_mailboxes, "_cache", [
        {"primary": CLOUD, "addresses": [CLOUD], "type": "UserMailbox", "guid": "g-cloud"}])
    monkeypatch.setattr(exo_mailboxes, "_cache_ts", time.monotonic())
    ews = FalscherPosteingang()
    monkeypatch.setattr(ews_onprem, "_senden", ews)

    graph = {"urls": [], "mails": []}

    class _Antwort:
        status_code = 200
        def json(self): return {"value": graph["mails"]}

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            graph["urls"].append(url)
            return _Antwort()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())
    import graph_client

    async def _token():
        return "tok"
    monkeypatch.setattr(graph_client, "_acquire_token_async", _token)
    import einwilligung
    monkeypatch.setattr(einwilligung, "liegt_vor", lambda email: {"von": "admin:test"})
    return store, ews, graph


# ── Weiche ────────────────────────────────────────────────────────────────────

def test_weiche_cloud_hybrid_gesperrt(lage):
    store, ews, _ = lage
    assert ews_onprem.postfach_weg(CLOUD) == "graph"
    assert ews_onprem.postfach_weg(HYBRID) == "gesperrt"
    assert ews_onprem.bezug_sperre(HYBRID) and not ews_onprem.bezug_sperre(CLOUD)
    store.update(KONFIG)
    assert ews_onprem.postfach_weg(CLOUD) == "graph", "EXO-Postfach geht NIE über EWS"
    assert ews_onprem.postfach_weg(HYBRID) == "ews"
    assert not ews_onprem.bezug_sperre(HYBRID)
    assert ews.anfragen == []


def test_abwesenheit_nutzt_dieselbe_weiche(lage, monkeypatch):
    import abwesenheit
    store, _, _ = lage
    gefragt = []
    monkeypatch.setattr(ews_onprem, "zustaendig", lambda a: gefragt.append(a) or True)
    assert abwesenheit._ueber_ews(HYBRID) is True and gefragt == [HYBRID]


# ── EWS: Anfragen und Antworten ──────────────────────────────────────────────

def test_finditem_anfrage_aufbau_und_maskierung():
    teil = 'ACME: <x> & "y" Grüße'
    roh = ews_onprem.anfrage_posteingang_suchen(HYBRID, _jetzt(minutes=-5), teil, 999)
    root = ET.fromstring(roh)                       # gültiges XML trotz Sonderzeichen
    fi = root.find(".//m:FindItem", NS)
    kinder = [k.tag.split("}")[1] for k in fi]
    assert kinder == ["ItemShape", "IndexedPageItemView", "Restriction", "SortOrder",
                      "ParentFolderIds"], "EWS-Schema verlangt diese Reihenfolge"
    assert fi.find("m:IndexedPageItemView", NS).get("MaxEntriesReturned") == "50"
    und = fi.find("m:Restriction/t:And", NS)
    assert und is not None and len(und) == 2
    assert fi.find(".//t:Contains/t:Constant", NS).get("Value") == teil
    assert fi.find(".//t:DistinguishedFolderId/t:Mailbox/t:EmailAddress", NS).text == HYBRID


def test_finditem_ohne_filter_ohne_restriction():
    fi = ET.fromstring(ews_onprem.anfrage_posteingang_suchen(HYBRID)).find(".//m:FindItem", NS)
    assert fi.find("m:Restriction", NS) is None


def test_getitem_bodytype_vor_zusatzfeldern():
    roh = ews_onprem.anfrage_nachrichten_holen(HYBRID, [("ID1", "CK1"), ("ID2", "")])
    form = ET.fromstring(roh).find(".//m:GetItem/m:ItemShape", NS)
    assert [k.tag.split("}")[1] for k in form] == ["BaseShape", "BodyType", "AdditionalProperties"]
    ids = ET.fromstring(roh).findall(".//m:ItemIds/t:ItemId", NS)
    assert [(i.get("Id"), i.get("ChangeKey")) for i in ids] == [("ID1", "CK1"), ("ID2", None)]


def _mail(i, betreff, zeit, text="<p>hallo</p>", von="autoresponder@ca.example", **kw):
    return {"id": f"ID{i}", "betreff": betreff, "zeit": _z(zeit), "text": text, "von": von, **kw}


def test_posteingang_rundlauf_graph_form(lage):
    store, ews, _ = lage
    store.update(KONFIG)
    challenge = ("-----BEGIN ACME CHALLENGE-----\r\nTOKEN_part2-abc\r\n"
                 "-----END ACME CHALLENGE-----\r\n")
    ews.mails = [_mail(1, "ACME: tok1", _jetzt(minutes=-1), challenge, "acme@castle.example",
                       text_art="Text", mid="<c1@castle.example>"),
                 _mail(2, "Rechnung", _jetzt(minutes=-2))]
    erg = ews_onprem.posteingang_als_graph(HYBRID, None, "", 10)
    assert [m["subject"] for m in erg] == ["ACME: tok1", "Rechnung"]
    assert erg[0]["from"]["emailAddress"]["address"] == "acme@castle.example"
    assert erg[0]["internetMessageId"] == "<c1@castle.example>"
    assert erg[0]["body"] == {"contentType": "text", "content": challenge}, "CRLF bleibt erhalten"
    assert erg[1]["body"]["contentType"] == "html"
    import acme_state
    assert acme_state._extract_body_token(erg[0]["body"]["content"]) == "TOKEN_part2-abc"


def test_ohne_treffer_kein_getitem(lage):
    store, ews, _ = lage
    store.update(KONFIG)
    assert ews_onprem.posteingang_lesen(HYBRID, None, "nichts", 5) == []
    assert len(ews.anfragen) == 1


def test_geloeschte_mail_nimmt_die_anderen_nicht_mit(lage):
    store, ews, _ = lage
    store.update(KONFIG)
    ews.mails = [_mail(1, "a", _jetzt(minutes=-1)), _mail(2, "b", _jetzt(minutes=-2))]
    ews.weg = {"ID1"}
    assert [m["betreff"] for m in ews_onprem.posteingang_lesen(HYBRID)] == ["b"]
    ews.weg = {"ID1", "ID2"}
    with pytest.raises(ews_onprem.EwsFehler) as e:
        ews_onprem.posteingang_lesen(HYBRID)
    assert e.value.code == "ErrorItemNotFound"


def test_einzelantwort_bleibt_streng():
    """Ohne `teilweise` zählt wie bisher die ERSTE ResponseMessage."""
    roh = (f'<s:Envelope xmlns:s="{ews_onprem.NS_SOAP}"><s:Body><m:X xmlns:m="{ews_onprem.NS_M}">'
           '<m:ResponseMessages><m:AResponseMessage ResponseClass="Error"><m:ResponseCode>ErrorA'
           '</m:ResponseCode></m:AResponseMessage><m:AResponseMessage ResponseClass="Success">'
           '<m:ResponseCode>NoError</m:ResponseCode></m:AResponseMessage></m:ResponseMessages>'
           '</m:X></s:Body></s:Envelope>')
    with pytest.raises(ews_onprem.EwsFehler):
        ews_onprem.antwort_pruefen(200, roh)
    ews_onprem.antwort_pruefen(200, roh, teilweise=True)


def test_ohne_konfiguration_kein_netzaufruf(lage):
    _, ews, _ = lage
    with pytest.raises(ews_onprem.EwsFehler) as e:
        ews_onprem.posteingang_lesen(HYBRID)
    assert e.value.code == "konfiguration" and ews.anfragen == []


# ── hub_orders: Bestätigungslink ─────────────────────────────────────────────

def _ca_mail(i, ref, zeit, link=LINK):
    import hub_orders
    absender = f"autoresponder@{hub_orders._CA_ABSENDER[0]}"
    return _mail(i, f"E-mail verification - {ref}", zeit,
                 f'<img src="https://repo.example/x.png"><a href="{link.replace("&", "&amp;")}">Verify</a>',
                 absender)


def test_bestaetigungslink_hybrid_ueber_ews(lage):
    import hub_orders
    store, ews, graph = lage
    store.update(KONFIG)
    seit = _jetzt(minutes=-3)
    ews.mails = [_ca_mail(1, "exoREF2", _jetzt(minutes=-1), LINK.replace("AbCdEf", "FREMD")),
                 _ca_mail(2, "exoREF1", _jetzt(minutes=-2)),
                 _ca_mail(3, "exoREF1", _jetzt(minutes=-40), LINK.replace("AbCdEf", "ALT"))]
    link = asyncio.run(hub_orders.bestaetigungslink(HYBRID, "exoREF1", seit.isoformat()))
    assert link == LINK
    assert graph["urls"] == [], "Hybrid geht nie an Graph"
    # Zeitfilter mit Vorlauf (15 min) und Referenz im Betreff am Server
    fi = ET.fromstring(ews.anfragen[0]).find(".//m:FindItem", NS)
    ab = fi.find(".//t:IsGreaterThanOrEqualTo/t:FieldURIOrConstant/t:Constant", NS).get("Value")
    assert ab == _z(seit - timedelta(minutes=15))
    assert fi.find(".//t:Contains/t:Constant", NS).get("Value") == "exoREF1"


def test_bestaetigungslink_hybrid_ohne_ews_liest_nichts(lage):
    import hub_orders
    _, ews, graph = lage
    assert asyncio.run(hub_orders.bestaetigungslink(HYBRID, "exoREF1")) == ""
    assert graph["urls"] == [] and ews.anfragen == []


def test_bestaetigungslink_cloud_unveraendert(lage):
    """Gegenprobe: EXO-Postfach, auch mit eingerichteter Anbindung — nie EWS,
    Graph-Abruf byte-gleich mit der Fassung vor v1.12.62."""
    import hub_orders
    store, ews, graph = lage
    store.update(KONFIG)
    graph["mails"] = [{"subject": "E-mail verification - exoREF1",
                       "from": {"emailAddress": {"address": "autoresponder@" + hub_orders._CA_ABSENDER[0]}},
                       "body": {"content": f'<a href="{LINK}">x</a>'}}]
    seit = "2026-01-02T03:04:05+00:00"
    link = asyncio.run(hub_orders.bestaetigungslink(CLOUD, "exoREF1", seit))
    assert ews.anfragen == []
    assert graph["urls"] == [
        f"https://graph.microsoft.com/v1.0/users/{CLOUD}/mailFolders/inbox/messages"
        "?$select=subject,from,body&$top=25&$orderby=receivedDateTime desc"
        "&$filter=receivedDateTime ge 2026-01-02T02:49:05Z"]
    assert link == LINK


# ── acme_state: Challenge-Mail und Antwort ───────────────────────────────────

@pytest.fixture
def acme(lage, monkeypatch):
    import acme_state
    store, ews, graph = lage
    gespeichert: dict = {}
    gefunden: list = []
    order = {"email": "", "status": "waiting_challenge", "flow_id": "t", "token_part2": "AAAA",
             "created": _jetzt(minutes=-5).isoformat(), "from_address": ""}

    def _order(email):
        return {**order, "email": email}
    monkeypatch.setattr(acme_state, "get_order", _order)
    monkeypatch.setattr(acme_state, "get_pending_order_for_challenge", _order)
    monkeypatch.setattr(acme_state, "save_order", lambda email, st: gespeichert.update(st))

    async def _schlafen(s):
        return None
    monkeypatch.setattr(acme_state.asyncio, "sleep", _schlafen)

    async def _gefunden(pending, token_part1):
        gefunden.append((pending, token_part1))
    monkeypatch.setattr(acme_state, "handle_challenge_email", _gefunden)
    import notification
    monkeypatch.setattr(notification, "send_cert_renewal_failure", lambda *a, **k: True)
    return acme_state, store, ews, graph, gespeichert, gefunden, order


def test_challenge_hybrid_ueber_ews(acme):
    acme_state, store, ews, graph, gespeichert, gefunden, order = acme
    store.update(KONFIG)
    ews.mails = [_mail(1, "ACME: tokenEins", _jetzt(minutes=-1),
                       "-----BEGIN ACME CHALLENGE-----\r\nBBBB\r\n-----END ACME CHALLENGE-----\r\n",
                       "acme@castle.example", text_art="Text", mid="<c@castle.example>"),
                 _mail(2, "ACME: alt", _jetzt(minutes=-60))]
    asyncio.run(acme_state._poll_mailbox_for_challenge(HYBRID))
    assert graph["urls"] == []
    assert len(gefunden) == 1
    pending, teil1 = gefunden[0]
    assert teil1 == "tokenEins"
    assert pending["from_address"] == "acme@castle.example"
    assert pending["challenge_internet_msg_id"] == "<c@castle.example>"
    assert pending["token_part2"] == "BBBB", "Wert aus dem Text wie beim Graph-Weg"
    fi = ET.fromstring(ews.anfragen[0]).find(".//m:FindItem", NS)
    assert fi.find(".//t:Contains/t:Constant", NS).get("Value") == "ACME: "
    assert fi.find(".//t:IsGreaterThanOrEqualTo/t:FieldURIOrConstant/t:Constant", NS).get("Value") \
        == _z(datetime.fromisoformat(order["created"]))


def test_challenge_hybrid_ohne_ews_bricht_mit_grund_ab(acme):
    acme_state, store, ews, graph, gespeichert, gefunden, _ = acme
    asyncio.run(acme_state._poll_mailbox_for_challenge(HYBRID))
    assert graph["urls"] == [] and ews.anfragen == [] and gefunden == []
    assert gespeichert["status"] == "error" and "Exchange lokal" in gespeichert["error"]


def test_challenge_hybrid_impersonation_verweigert_bricht_ab(acme, monkeypatch):
    acme_state, store, ews, graph, gespeichert, gefunden, _ = acme
    store.update(KONFIG)

    def _verweigert(*a, **k):
        raise ews_onprem.EwsFehler("ErrorImpersonateUserDenied", "Dienstkonto darf nicht")
    monkeypatch.setattr(ews_onprem, "posteingang_als_graph", _verweigert)
    asyncio.run(acme_state._poll_mailbox_for_challenge(HYBRID))
    assert gespeichert["status"] == "error" and "verweigert" in gespeichert["error"]


def test_challenge_cloud_unveraendert(acme):
    acme_state, store, ews, graph, gespeichert, gefunden, order = acme
    store.update(KONFIG)
    graph["mails"] = [{"subject": "ACME: tokenZwei", "from": {"emailAddress": {"address": "a@ca.example"}},
                       "internetMessageId": "<g@ca.example>", "body": {"content": ""}}]
    asyncio.run(acme_state._poll_mailbox_for_challenge(CLOUD))
    assert ews.anfragen == []
    cutoff = datetime.fromisoformat(order["created"]).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert graph["urls"] == [
        f"https://graph.microsoft.com/v1.0/users/{CLOUD}/mailFolders/inbox/messages"
        "?$select=subject,from,receivedDateTime,internetMessageId,body&$top=50"
        f"&$orderby=receivedDateTime desc&$filter=receivedDateTime gt {cutoff}"]
    assert gefunden[0][1] == "tokenZwei"


@pytest.mark.parametrize("adresse,erwartet", [(HYBRID, "reinject"), (CLOUD, "graph")])
def test_antwort_methode_graph_hybrid_ueber_smarthost(lage, monkeypatch, adresse, erwartet):
    """ACME_REPLY_METHOD=graph sendete ALS den Nutzer — für Hybrid unmöglich.
    Er geht über reinject (dort: Smarthost). EXO-Postfächer unverändert."""
    import acme_state
    import graph_reinject
    import reinject
    store, _, _ = lage
    store["ACME_REPLY_METHOD"] = "graph"
    wege = []
    monkeypatch.setattr(reinject, "send", lambda *a, **k: wege.append(("reinject", a, k)))
    monkeypatch.setattr(graph_reinject, "send_via_graph_mime",
                        lambda *a, **k: wege.append(("graph", a, k)) or True)
    ok = asyncio.run(acme_state._send_challenge_reply(adresse, "acme@ca.example", "Re: ACME: x", "d"))
    assert ok is True and [w[0] for w in wege] == [erwartet]
    roh = wege[0][1][2]
    assert b"\r\n" in roh and b"\n" not in roh.replace(b"\r\n", b""), "nur CRLF (policy SMTP)"


# ── Bestellung: Sperre im Kern, nicht als Einwilligung ───────────────────────

def test_verlangen_sperrt_hybrid_ohne_ews(lage, monkeypatch):
    import acme_state
    import einwilligung
    store, _, _ = lage
    with pytest.raises(acme_state.EnrollmentNotAllowed) as e:
        einwilligung.verlangen(HYBRID)
    assert not isinstance(e.value, acme_state.EinwilligungFehlt)
    assert "Exchange lokal" in str(e.value)
    einwilligung.verlangen(CLOUD)                        # Cloud: unverändert
    store.update(KONFIG)
    einwilligung.verlangen(HYBRID)                       # mit Anbindung frei
    monkeypatch.setattr(einwilligung, "liegt_vor", lambda email: None)
    with pytest.raises(acme_state.EinwilligungFehlt):
        einwilligung.verlangen(HYBRID)                   # dann gilt die Einwilligung wie sonst


def _klassen_quelle():
    out = {}
    for datei in (WURZEL / "app/ca_backends").glob("*.py"):
        baum = ast.parse(datei.read_text("utf-8"))
        for k in ast.walk(baum):
            if isinstance(k, ast.ClassDef):
                out[k.name] = ast.get_source_segment(datei.read_text("utf-8"), k)
    return out


def test_liest_postfach_passt_zum_code():
    """Ein Bezugsweg, der einwilligung.verlangen (direkt oder über
    initiate_acme_order) ruft, liest das Postfach — und muss das sagen, sonst
    zeigte die Oberfläche den Knopf für gesperrte Hybrid-Nutzer frei."""
    import ca_backends
    quelle = _klassen_quelle()
    namen = {"AssistedManualBackend": "assisted_manual", "CastleAcmeBackend": "castle_acme",
             "CastleAcmeStagingBackend": "castle_acme_staging",
             "DigiCertDirectBackend": "digicert_direct", "HubProviderBackend": "hub:x"}
    for klasse, name in namen.items():
        b = ca_backends.get_backend(name)
        liest = "verlangen(" in quelle[klasse] or "initiate_acme_order" in quelle[klasse] \
            or klasse == "CastleAcmeStagingBackend"
        assert b.liest_postfach() is liest, klasse
    zeilen = {b["name"]: b["liest_postfach"] for b in ca_backends.list_backends()}
    assert zeilen["castle_acme"] is True and zeilen["digicert_direct"] is False


# ── Speichern und Oberfläche ─────────────────────────────────────────────────

def test_oberflaeche_sperrt_smime_fuer_hybrid_nicht_mehr():
    html = (WURZEL / "app/webui/templates/mailboxes.html").read_text("utf-8")
    zeile = [z for z in html.splitlines() if "smimeCb" in z and "isHybrid" in z]
    assert zeile and all("disabled" not in z and "checked = false" not in z for z in zeile)


def test_smime_seite_sperrt_bezug_mit_grund():
    src = (WURZEL / "app/webui/templates/smime.html").read_text("utf-8")
    assert "_sperre" in src and "liest_postfach" in src
    route = (WURZEL / "app/webui/routen/smime.py").read_text("utf-8")
    assert '"bezug_gesperrt"' in route and "bezug_sperre(em)" in route


# ── Gesundheitsprüfung ───────────────────────────────────────────────────────

@pytest.fixture
def gesund(lage, monkeypatch):
    import health_check
    monkeypatch.setattr(exo_mailboxes, "hybrid_aktiv", lambda: True)
    monkeypatch.setattr(health_check, "_check_smime_cert",
                        lambda e: health_check._make_result("ok", "Zertifikat gültig"))
    monkeypatch.setattr(health_check, "_check_smime_key",
                        lambda e: health_check._make_result("ok", "Schlüssel passt"))
    return health_check, lage


def test_gesundheit_hybrid_prueft_smime_am_gateway(gesund):
    health_check, (store, ews, _) = gesund
    erg = asyncio.run(health_check._checks_hybrid(HYBRID, {"smime": True}, {HYBRID: {"dg_member": True}}))
    assert erg["smime_cert"]["status"] == "ok" and erg["smime_key"]["status"] == "ok"
    assert erg["postfach_lesen"]["status"] == "skip" and "Import" in erg["postfach_lesen"]["detail"]
    assert erg["graph_postfach"]["status"] == "skip"
    assert ews.anfragen == []


def test_gesundheit_hybrid_postfach_lesen_ueber_ews(gesund):
    health_check, (store, ews, _) = gesund
    store.update(KONFIG)
    erg = asyncio.run(health_check._checks_hybrid(HYBRID, {"smime": True}, {HYBRID: {"dg_member": True}}))
    assert erg["postfach_lesen"]["status"] == "ok"
    fi = ET.fromstring(ews.anfragen[-1]).find(".//m:FindItem", NS)
    assert fi.find("m:ItemShape/t:BaseShape", NS).text == "IdOnly"
    assert ews.anfragen and not any(b"GetItem" in a for a in ews.anfragen), "nur Kennungen"


def test_gesundheit_hybrid_ohne_smime_liest_nicht(gesund):
    health_check, (store, ews, _) = gesund
    store.update(KONFIG)
    erg = asyncio.run(health_check._checks_hybrid(HYBRID, {"sig": False}, {HYBRID: {"dg_member": True}}))
    assert erg["postfach_lesen"]["status"] == "skip" and erg["smime_cert"]["status"] == "skip"
    assert not any(b"FindItem" in a for a in ews.anfragen)


# ── Senden ALS der Nutzer: Portal-Mails und NDR ──────────────────────────────

@pytest.mark.parametrize("absender,weg", [(HYBRID, "reinject"), (CLOUD, "graph")])
def test_portal_mail_als_hybrid_ueber_smarthost(lage, monkeypatch, absender, weg):
    """#enc ohne Empfängerzertifikat → Portal. Die Benachrichtigung geht ALS
    der Absender — für Hybrid über reinject (Smarthost), EXO unverändert Graph."""
    import notification
    import reinject
    store, _, _ = lage
    monkeypatch.setattr(settings_store, "reinject_mode", lambda: "smtp")
    monkeypatch.setattr(notification, "_extern_umgeleitet", lambda to: False)
    wege = []
    monkeypatch.setattr(reinject, "send", lambda f, r, roh, **k: wege.append(("reinject", f, r, roh)))
    monkeypatch.setattr(notification.graph_client, "_acquire_token",
                        lambda: wege.append(("graph",)) or None)
    notification._graph_send("extern@kunde.example", "Betreff", "<p>x</p>", sender=absender)
    assert [w[0] for w in wege] == [weg]
    if weg == "reinject":
        assert wege[0][1] == HYBRID and wege[0][2] == ["extern@kunde.example"]
        import loop_detector
        assert loop_detector.header_name().encode() in wege[0][3], "Markierung gegen die Schleife"


@pytest.mark.parametrize("absender,graph_versucht", [(HYBRID, False), (CLOUD, True)])
def test_ndr_hybrid_ohne_graph_versuch(lage, monkeypatch, absender, graph_versucht):
    import graph_reinject
    import handler
    versuche = []
    monkeypatch.setattr(graph_reinject, "send_via_graph_mime",
                        lambda *a, **k: versuche.append(a) or False)

    class _SMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def ehlo(self): pass
        def starttls(self, **k): pass
        def sendmail(self, von, an, roh): versuche.append(("smtp", an))
    monkeypatch.setattr(handler.smtplib, "SMTP", _SMTP)
    handler._send_ndr(absender, ["extern@kunde.example"])
    assert (len(versuche) == 2) is graph_versucht
    assert versuche[-1] == ("smtp", [absender])


@pytest.mark.parametrize("override", [None, "Verschlüsselung fehlgeschlagen.\n"])
def test_ndr_umlaut_betreff_wird_serialisiert(lage, monkeypatch, override):
    """Beide NDR-Betreffs enthalten „ü“. Bis v1.12.61 brach die Serialisierung
    mit UnicodeEncodeError ab (SMTP-Policy, compat32-Nachricht) — die Meldung
    ging für JEDES Postfach nie hinaus."""
    import email as _email
    import email.policy
    import graph_reinject
    import handler
    roh = []
    monkeypatch.setattr(graph_reinject, "send_via_graph_mime",
                        lambda a, r, b: roh.append(b) or True)
    handler._send_ndr(CLOUD, ["extern@kunde.example"], override_body=override)
    assert len(roh) == 1
    m = _email.message_from_bytes(roh[0], policy=email.policy.default)
    assert m["Subject"].startswith("Zustellung fehlgeschlagen:") and "ü" in m["Subject"]
