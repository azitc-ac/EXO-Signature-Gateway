"""OWA-Signatur lokal für Hybrid-Nutzer (v1.12.63).

Was hier gesichert wird:

* XML der Benutzerkonfiguration (OWA.UserOptions) über xml.etree: Sonderzeichen
  und HTML kommen Zeichen für Zeichen zurück, Typen (Boolean, Integer32,
  StringArray, Eintrag ohne Wert) bleiben erhalten.
* UpdateUserConfiguration ERSETZT das Wörterbuch (im Lab gemessen) — die falsche
  Exchange unten tut dasselbe. Fremde Einträge überleben nur, weil
  `owa_signatur_setzen` liest, zusammenführt und vollständig zurückschreibt.
* Fehlt das Objekt, wird es angelegt (Create); ein zweites Create scheitert wie
  beim echten Server.
* Cloud-Postfächer nie über EWS; Schalter aus → kein Aufruf, nichts geschrieben.
* Doppelsignatur: Was OWA einfügt, trägt die Kennung des Add-ins, und das
  Gateway setzt keine zweite Signatur.

Testdaten erfunden (firma.at).
"""
import asyncio
import email
import xml.etree.ElementTree as ET

import pytest

import ews_onprem
import exo_mailboxes
import mail_processor
import owa_signatur
import settings_store

T = f"{{{ews_onprem.NS_T}}}"
M = f"{{{ews_onprem.NS_M}}}"
HYBRID = "max.mustermann@firma.at"
HYBRID2 = "erika.beispiel@firma.at"
CLOUD = "eva.wolke@firma.at"
MIGRIERT = "paul.umzug@firma.at"      # Eintrag noch mit hybrid-Marke, Postfach inzwischen in EXO
KONFIG = {"EWS_ONPREM_URL": "https://exchange.firma.at/EWS/Exchange.asmx",
          "EWS_ONPREM_KONTO": "FIRMA\\svc-gateway",
          "EWS_ONPREM_KENNWORT": "nur-ein-testwert"}
SIG = ('<table><tr><td style="font-family:Calibri">Grüße &amp; <b>Max Mustermann</b> — '
       'äöüß € &lt;Vertrieb&gt; "zitiert" \'einfach\'</td></tr></table>')
TXT = "Grüße & Max Mustermann — <Vertrieb>"


class FalscheExchange:
    """EWS im Speicher, je Postfach ein OWA.UserOptions. Verhalten wie im Lab
    gemessen: Get ohne Objekt → ErrorItemNotFound; Create auf Vorhandenes →
    ErrorItemSave; Update ERSETZT das Wörterbuch als Ganzes."""

    def __init__(self):
        self.objekte: dict[str, dict] = {}
        self.aufrufe: list[tuple[str, str]] = []
        self.kuerzen = 0                     # >0: Exchange kürzt signaturehtml
        self.verweigert: set[str] = set()

    def __call__(self, ziel, umschlag):
        root = ET.fromstring(umschlag)
        rumpf = root.find(f"{{{ews_onprem.NS_SOAP}}}Body")[0]
        op = rumpf.tag.split("}")[1]
        self.aufrufe.append((op, ziel))
        if ziel in self.verweigert:
            return 500, _fault("ErrorImpersonateUserDenied")
        if op == "GetUserConfiguration":
            if ziel not in self.objekte:
                return 200, _fehler(op, "ErrorItemNotFound")
            return 200, self._get(op, self.objekte[ziel])
        uc = rumpf.find(f"{M}UserConfiguration")
        neu = _uc_lesen(uc)
        if op == "CreateUserConfiguration" and ziel in self.objekte:
            return 200, _fehler(op, "ErrorItemSave")
        if op == "UpdateUserConfiguration" and ziel not in self.objekte:
            return 200, _fehler(op, "ErrorItemNotFound")
        if self.kuerzen:
            for e in neu["eintraege"]:
                if e["k"].lower() == "signaturehtml":
                    e["werte"] = [e["werte"][0][:self.kuerzen]]
        self.objekte[ziel] = neu             # Update ersetzt ALLES
        return 200, _ok(op)

    def _get(self, op, obj):
        env, rm = _huelle(op, "Success", "NoError")
        uc = ET.SubElement(rm, f"{M}UserConfiguration")
        ET.SubElement(uc, f"{T}UserConfigurationName", {"Name": "OWA.UserOptions"})
        d = ET.SubElement(uc, f"{T}Dictionary")
        for e in obj["eintraege"]:
            de = ET.SubElement(d, f"{T}DictionaryEntry")
            k = ET.SubElement(de, f"{T}DictionaryKey")
            ET.SubElement(k, f"{T}Type").text = e["ktyp"]
            ET.SubElement(k, f"{T}Value").text = e["k"]
            if e["werte"] is not None:
                v = ET.SubElement(de, f"{T}DictionaryValue")
                ET.SubElement(v, f"{T}Type").text = e["vtyp"]
                for w in e["werte"]:
                    ET.SubElement(v, f"{T}Value").text = w
        if obj.get("xml_data") is not None:
            ET.SubElement(uc, f"{T}XmlData").text = obj["xml_data"]
        if obj.get("binary_data") is not None:
            ET.SubElement(uc, f"{T}BinaryData").text = obj["binary_data"]
        return ET.tostring(env, encoding="utf-8", xml_declaration=True)


def _uc_lesen(uc):
    eintraege = []
    for de in uc.iter(f"{T}DictionaryEntry"):
        k, v = de.find(f"{T}DictionaryKey"), de.find(f"{T}DictionaryValue")
        eintraege.append({"ktyp": k.find(f"{T}Type").text, "k": k.find(f"{T}Value").text or "",
                          "vtyp": v.find(f"{T}Type").text if v is not None else "",
                          "werte": [w.text or "" for w in v.findall(f"{T}Value")] if v is not None else None})
    x, b = uc.find(f"{T}XmlData"), uc.find(f"{T}BinaryData")
    return {"eintraege": eintraege, "xml_data": x.text if x is not None else None,
            "binary_data": b.text if b is not None else None}


def _huelle(op, klasse, code):
    env = ET.Element(f"{{{ews_onprem.NS_SOAP}}}Envelope")
    body = ET.SubElement(env, f"{{{ews_onprem.NS_SOAP}}}Body")
    r = ET.SubElement(ET.SubElement(body, f"{M}{op}Response"), f"{M}ResponseMessages")
    rm = ET.SubElement(r, f"{M}{op}ResponseMessage", {"ResponseClass": klasse})
    ET.SubElement(rm, f"{M}ResponseCode").text = code
    return env, rm


def _ok(op):
    return ET.tostring(_huelle(op, "Success", "NoError")[0], encoding="utf-8")


def _fehler(op, code):
    return ET.tostring(_huelle(op, "Error", code)[0], encoding="utf-8")


def _fault(code):
    return (f'<s:Envelope xmlns:s="{ews_onprem.NS_SOAP}"><s:Body><s:Fault><faultcode>a:{code}</faultcode>'
            f'<detail><e:ResponseCode xmlns:e="{ews_onprem.NS_E}">{code}</e:ResponseCode></detail>'
            '</s:Fault></s:Body></s:Envelope>').encode()


def _e(k, vtyp, *werte, ktyp="String"):
    return {"ktyp": ktyp, "k": k, "vtyp": vtyp, "werte": list(werte)}


@pytest.fixture
def werte(monkeypatch):
    store = {"MAILBOX_CONFIG": {}}
    geschrieben = []
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: store.get(k, d))

    def force_update(patch):
        geschrieben.append(dict(patch))
        store.update(patch)
    monkeypatch.setattr(settings_store, "force_update", force_update)
    store["_geschrieben"] = geschrieben
    return store


@pytest.fixture
def exchange(werte, monkeypatch):
    werte.update(KONFIG)
    fx = FalscheExchange()
    monkeypatch.setattr(ews_onprem, "_senden", fx)
    return fx


# ── XML und Zusammenführen ───────────────────────────────────────────────────

def test_anfrage_rundlauf_sonderzeichen_und_typen():
    konfig = {"eintraege": [_e("signaturehtml", "String", SIG), _e("autoaddsignature", "Boolean", "true"),
                            _e("liste", "StringArray", "a<b", "c&d"), _e("zahl", "Integer32", "42"),
                            {"ktyp": "String", "k": "leer", "vtyp": "", "werte": None}],
              "xml_data": "PGE+PC9hPg==", "binary_data": None}
    roh = ews_onprem.anfrage_uc_schreiben(HYBRID, konfig, anlegen=False)
    uc = ET.fromstring(roh).find(f".//{M}UserConfiguration")
    assert uc is not None and ET.fromstring(roh).find(f".//{M}UpdateUserConfiguration") is not None
    zurueck = _uc_lesen(uc)
    assert zurueck == konfig
    # Rundlauf auch über den Leser des Moduls (Antwortform eines Get)
    env = FalscheExchange()._get("GetUserConfiguration", konfig)
    assert ews_onprem.uc_aus_antwort(ET.fromstring(env)) == konfig


def test_unbekannter_typ_wird_nicht_geschrieben():
    with pytest.raises(ValueError):
        ews_onprem.anfrage_uc_schreiben(HYBRID, {"eintraege": [_e("x", "Erfunden", "1")]}, True)


def test_zusammenfuehren_behaelt_fremde_eintraege_und_typen():
    bestand = [_e("timezone", "String", "W. Europe Standard Time"), _e("SignatureHtml", "String", "alt"),
               _e("liste", "StringArray", "a", "b"), _e("AutoAddSignature", "String", "False"),
               _e("zahl", "Integer32", "7"), {"ktyp": "String", "k": "leer", "vtyp": "", "werte": None}]
    neu = ews_onprem.uc_zusammenfuehren(bestand, {
        "signaturehtml": ("String", SIG), "autoaddsignature": ("Boolean", True),
        "autoaddsignatureonreply": ("Boolean", False)})
    assert [e["k"] for e in neu] == ["timezone", "SignatureHtml", "liste", "AutoAddSignature", "zahl",
                                     "leer", "autoaddsignatureonreply"]
    assert neu[0] == bestand[0] and neu[2] == bestand[2] and neu[4] == bestand[4] and neu[5] == bestand[5]
    assert neu[1]["werte"] == [SIG]
    assert neu[3] == _e("AutoAddSignature", "String", "True")          # Typ des Bestands bleibt
    assert neu[6] == _e("autoaddsignatureonreply", "Boolean", "false")
    assert bestand[1]["werte"] == ["alt"]                              # Eingabe unverändert


# ── Setzen gegen die falsche Exchange ────────────────────────────────────────

def test_setzen_legt_fehlendes_objekt_an(exchange):
    stand = ews_onprem.owa_signatur_setzen(HYBRID, SIG, TXT, True, False)
    assert stand == {"vorhanden": True, "html": SIG, "text": TXT, "auto_neu": True, "auto_antwort": False}
    assert [op for op, _ in exchange.aufrufe] == ["GetUserConfiguration", "CreateUserConfiguration",
                                                  "GetUserConfiguration"]
    typen = {e["k"]: e["vtyp"] for e in exchange.objekte[HYBRID]["eintraege"]}
    assert typen == {"signaturehtml": "String", "signaturetext": "String",
                     "autoaddsignature": "Boolean", "autoaddsignatureonreply": "Boolean"}


def test_setzen_fuehrt_mit_bestand_zusammen(exchange):
    """Update ersetzt das Wörterbuch — ohne Zusammenführen wären die fremden
    Einträge weg (im Lab so gemessen)."""
    fremd = [_e("timezone", "String", "W. Europe Standard Time"), _e("zahl", "Integer32", "42"),
             _e("liste", "StringArray", "x", "y")]
    exchange.objekte[HYBRID] = {"eintraege": [*fremd, _e("signaturehtml", "String", "alt")],
                                "xml_data": "PGE+PC9hPg==", "binary_data": "AAEC"}
    ews_onprem.owa_signatur_setzen(HYBRID, SIG, TXT, True, False)
    assert [op for op, _ in exchange.aufrufe][1] == "UpdateUserConfiguration"
    obj = exchange.objekte[HYBRID]
    for e in fremd:
        assert e in obj["eintraege"]
    assert obj["xml_data"] == "PGE+PC9hPg==" and obj["binary_data"] == "AAEC"
    assert sum(e["k"].lower() == "signaturehtml" for e in obj["eintraege"]) == 1


def test_gekuerzt_zurueckgelesen_ist_ein_fehler(exchange):
    exchange.kuerzen = 20
    with pytest.raises(ews_onprem.EwsFehler) as fehler:
        ews_onprem.owa_signatur_setzen(HYBRID, SIG, TXT, True, False)
    assert fehler.value.code == "antwort"


def test_nicht_konfiguriert_kein_netzaufruf(werte, monkeypatch):
    netz = []
    monkeypatch.setattr(ews_onprem, "_senden", lambda *a: netz.append(a) or (200, b""))
    with pytest.raises(ews_onprem.EwsFehler):
        ews_onprem.owa_signatur_setzen(HYBRID, SIG, TXT, True, False)
    assert netz == []


# ── Abgleich (owa_signatur) ──────────────────────────────────────────────────

@pytest.fixture
def abgleich(exchange, werte, monkeypatch):
    werte["OWA_SIGNATUR_LOKAL"] = True
    werte["MAILBOX_CONFIG"] = {
        "g1": {"sig": True, "hybrid": True, "primary": HYBRID},
        "g2": {"sig": True, "hybrid": True, "primary": HYBRID2},
        "g3": {"sig": True, "primary": CLOUD},
        "g4": {"sig": False, "hybrid": True, "primary": "ohne.signatur@firma.at"},
        "g5": {"sig": True, "hybrid": True, "primary": MIGRIERT}}
    monkeypatch.setattr(exo_mailboxes, "_cache", [
        {"primary": CLOUD, "addresses": [CLOUD], "type": "UserMailbox", "guid": "g3"},
        {"primary": MIGRIERT, "addresses": [MIGRIERT], "type": "UserMailbox", "guid": "g5"}])
    inhalte = {HYBRID: SIG, HYBRID2: SIG.replace("Max Mustermann", "Erika Beispiel")}

    async def inhalt(upn, mailbox_cfg, cfg):
        html = inhalte.get(upn, "")
        return (mail_processor.markiert(html) if html else ""), TXT, "default"
    monkeypatch.setattr(owa_signatur, "inhalt", inhalt)
    monkeypatch.setattr(owa_signatur, "PAUSE_S", 0)
    return werte, inhalte


def _lauf(**kw):
    return asyncio.run(owa_signatur.abgleich(**kw))


def test_ziele_nur_hybrid_mit_signatur_nie_cloud(abgleich, exchange):
    """Auch ein nach EXO migriertes Postfach mit veralteter hybrid-Marke geht
    nicht über EWS — die Weiche ist ews_onprem.zustaendig."""
    werte, _ = abgleich
    assert sorted(u for u, _ in owa_signatur.ziele(werte["MAILBOX_CONFIG"])) == [HYBRID2, HYBRID]
    _lauf(erzwingen=True)
    assert {z for _, z in exchange.aufrufe} == {HYBRID, HYBRID2}


def test_schalter_aus_nichts(abgleich, exchange):
    werte, _ = abgleich
    werte["OWA_SIGNATUR_LOKAL"] = False
    erg = _lauf(erzwingen=True)
    assert erg["aktiv"] is False and erg["gesamt"] == 0
    assert exchange.aufrufe == [] and werte["_geschrieben"] == []


def test_ohne_anbindung_nichts(abgleich, exchange):
    werte, _ = abgleich
    werte["EWS_ONPREM_URL"] = ""
    werte["_OWA_SIGNATUR_STAND"] = {HYBRID: {"summe": "x"}}
    _lauf()
    assert exchange.aufrufe == [] and werte["_geschrieben"] == []


def test_abgleich_schreibt_nur_bei_aenderung(abgleich, exchange):
    werte, inhalte = abgleich
    erg = _lauf()
    assert (erg["gesetzt"], erg["gesamt"], erg["fehler"]) == (2, 2, 0)
    assert CLOUD not in {z for _, z in exchange.aufrufe}
    assert ews_onprem.owa_signatur_lesen(HYBRID)["html"] == mail_processor.markiert(SIG)
    exchange.aufrufe.clear()
    erg = _lauf()
    assert (erg["gesetzt"], erg["unveraendert"]) == (0, 2) and exchange.aufrufe == []
    inhalte[HYBRID] = SIG + "<p>neu</p>"
    erg = _lauf()
    assert erg["gesetzt"] == 1 and {z for _, z in exchange.aufrufe} == {HYBRID}
    exchange.aufrufe.clear()
    assert _lauf(erzwingen=True)["gesetzt"] == 2


def test_fehler_bei_einem_postfach_bricht_nicht_ab(abgleich, exchange):
    werte, _ = abgleich
    exchange.verweigert.add(HYBRID)
    erg = _lauf()
    assert (erg["gesetzt"], erg["kein_zugriff"]) == (1, 1)
    assert erg["fehler_liste"][0]["adresse"] == HYBRID
    assert HYBRID not in werte["_OWA_SIGNATUR_STAND"]          # nächster Lauf versucht erneut
    assert HYBRID2 in werte["_OWA_SIGNATUR_STAND"]
    assert werte["_OWA_SIGNATUR_LAST"]["gesamt"] == 2


def test_ausschalten_raeumt_nur_die_eigene_signatur(abgleich, exchange):
    werte, _ = abgleich
    _lauf()
    # Erika hat inzwischen selbst eine Signatur gesetzt (ohne Kennung).
    ews_onprem.owa_signatur_setzen(HYBRID2, "<p>Meine eigene</p>", "Meine eigene", True, False)
    werte["OWA_SIGNATUR_LOKAL"] = False
    erg = _lauf()
    assert erg["entfernt"] == 1
    assert ews_onprem.owa_signatur_lesen(HYBRID) == {"vorhanden": True, "html": "", "text": "",
                                                     "auto_neu": False, "auto_antwort": False}
    assert ews_onprem.owa_signatur_lesen(HYBRID2)["html"] == "<p>Meine eigene</p>"
    assert werte["_OWA_SIGNATUR_STAND"] == {}
    exchange.aufrufe.clear()
    werte["_geschrieben"].clear()
    _lauf()
    assert exchange.aufrufe == [] and werte["_geschrieben"] == []


def test_signatur_ausgeschaltet_raeumt_das_postfach(abgleich, exchange):
    werte, _ = abgleich
    _lauf()
    werte["MAILBOX_CONFIG"]["g1"]["sig"] = False
    assert _lauf()["entfernt"] == 1
    assert ews_onprem.owa_signatur_lesen(HYBRID)["html"] == ""
    assert ews_onprem.owa_signatur_lesen(HYBRID2)["html"] == mail_processor.markiert(inhalte_2())


def inhalte_2():
    return SIG.replace("Max Mustermann", "Erika Beispiel")


def test_knopf_fuer_ein_postfach(abgleich, exchange):
    erg = _lauf(erzwingen=True, nur=HYBRID.upper())
    assert erg["gesetzt"] == 1 and {z for _, z in exchange.aufrufe} == {HYBRID}
    erg = _lauf(erzwingen=True, nur=CLOUD)
    assert erg["ok"] is False and {z for _, z in exchange.aufrufe} == {HYBRID}


def test_inhalt_ist_die_markierte_versandsignatur(werte, monkeypatch):
    """Vorlage wie im Versand (policies, Slot sig), gerendert mit den Daten des
    Inhabers, markiert wie beim Add-in."""
    import graph_client
    import policies
    import signature_engine
    from webui.routen import addin
    aufrufe = []
    monkeypatch.setattr(policies, "vorlage_fuer", lambda s, slot, mb, cfg: aufrufe.append(slot) or "Vertrieb")

    async def get_user(upn):
        return graph_client.UserData(mail=upn, displayName="Max Mustermann")
    monkeypatch.setattr(graph_client, "get_user", get_user)
    monkeypatch.setattr(signature_engine, "render",
                        lambda u, template_name=None, extra=None: (f"<p>{u.displayName}|{template_name}|"
                                                                   f"{extra['bearbeiter'].mail}</p>", "txt"))
    html, txt, vorlage = asyncio.run(owa_signatur.inhalt(HYBRID, {}, {}))
    assert aufrufe == ["sig"] and vorlage == "Vertrieb" and txt == "txt"
    assert html == addin.markiert(f"<p>Max Mustermann|Vertrieb|{HYBRID}</p>")


# ── Doppelsignatur: die von OWA eingefügte Signatur wird erkannt ─────────────

def _owa_mail(signatur_html: str) -> email.message.Message:
    """Neue Mail aus OWA: Text des Nutzers, darunter die eingefügte Signatur."""
    return email.message_from_string(
        f"From: {HYBRID}\nTo: kunde@kunde.example\nSubject: Angebot\nMIME-Version: 1.0\n"
        "Content-Type: text/html; charset=utf-8\n\n"
        f"<html><body><div>Hallo,</div><div>anbei das Angebot.</div><br>{signatur_html}</body></html>")


@pytest.mark.parametrize("bearbeitet", [
    lambda h: h,                                                         # unverändert
    lambda h: h.replace("<!-- exo-sig-start -->", "").replace("<!-- exo-sig-end -->", ""),
    lambda h: h.replace('id="exo-sig-s"', 'id="x_exo-sig-s"'),
    lambda h: (h.replace("<!-- exo-sig-start -->", "").replace("<!-- exo-sig-end -->", "")
               .replace(' id="exo-sig-s"', "")),                       # nur die Klasse bleibt
    lambda h: h.replace(' class="exo-gateway-sig"', "").replace("<!-- exo-sig-start -->", ""),
], ids=["unveraendert", "ohne-kommentar", "x-praefix", "nur-klasse", "nur-id-und-ende"])
def test_owa_signatur_wird_im_verfassenbereich_erkannt(bearbeitet):
    msg = _owa_mail(bearbeitet(mail_processor.markiert(SIG)))
    assert mail_processor._has_own_sig_in_compose_area(msg)


def test_gateway_setzt_keine_zweite_signatur(werte):
    werte["SKIP_SIG_IN_THREAD"] = True
    msg = _owa_mail(mail_processor.markiert(SIG))
    raus = mail_processor.inject(msg, "<p>GATEWAY-SIGNATUR</p>", "GATEWAY-SIGNATUR")
    html = mail_processor.extract_html(raus)
    assert "GATEWAY-SIGNATUR" not in html
    assert html.count("Max Mustermann") == 1


def test_ohne_owa_signatur_signiert_das_gateway(werte):
    """Gegenprobe: dieselbe Mail ohne eingefügte Signatur bekommt die des Gateways."""
    werte["SKIP_SIG_IN_THREAD"] = True
    raus = mail_processor.inject(_owa_mail(""), "<p>GATEWAY-SIGNATUR</p>", "GATEWAY-SIGNATUR")
    assert "GATEWAY-SIGNATUR" in mail_processor.extract_html(raus)


def test_antworten_fuegt_owa_nicht_ein(abgleich, exchange):
    """Über Antworten entscheidet das Gateway (volle oder Antwort-Signatur)."""
    _lauf()
    assert ews_onprem.owa_signatur_lesen(HYBRID)["auto_antwort"] is False
    assert ews_onprem.owa_signatur_lesen(HYBRID)["auto_neu"] is True


# ── Einstellung: Vorgabe aus, registriert, an der Oberfläche ─────────────────

def test_schalter_vorgabe_aus_und_registriert():
    import einstellungen_register
    assert settings_store.DEFAULTS["OWA_SIGNATUR_LOKAL"] is False
    assert "OWA_SIGNATUR_LOKAL" in einstellungen_register.REGISTER
    assert {"_OWA_SIGNATUR_STAND", "_OWA_SIGNATUR_LAST"} <= settings_store.INTERNAL_KEYS


def test_schalter_in_der_ews_karte():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "app/webui/templates/settings.html").read_text()
    karte = html[html.index('id="exchange-lokal-ews"'):]
    karte = karte[:karte.index("<script>")]
    assert 'id="owa-signatur-lokal"' in karte and "saveOwaSignatur(this.checked)" in karte
    assert "OWA_SIGNATUR_LOKAL: an" in html


from test_seiten import client  # noqa: E402,F401  (TestClient mit umgangener Anmeldung)


def test_karte_zeigt_zaehlung_mit_bezugsgroesse(client):
    """Regel 8: Die Zählung des letzten Laufs steht immer da, auch mit Nullen —
    samt der Zahl der Hybrid-Nutzer. Ohne Schalter erscheint sie nicht."""
    import html as _html
    settings_store.force_update({"OWA_SIGNATUR_LOKAL": True, "_OWA_SIGNATUR_LAST": {
        "gesamt": 7, "gesetzt": 0, "unveraendert": 5, "keine_vorlage": 1, "entfernt": 0,
        "kein_zugriff": 1, "fehler": 0, "ts": "2030-01-02T03:04:05Z"}})
    try:
        r = client.get("/settings")
        assert r.status_code == 200
        text = " ".join(_html.unescape(r.text).split())
        assert ("Letzter Abgleich 2030-01-02 03:04 UTC: von 7 Hybrid-Nutzern 0 übertragen, "
                "5 unverändert, 1 ohne Signatur, 0 entfernt, 1 ohne Zugriff, 0 Fehler.") in text
        assert 'id="owa-sig-btn"' in r.text
        settings_store.force_update({"OWA_SIGNATUR_LOKAL": False})
        r = client.get("/settings")
        assert r.status_code == 200 and "owa-signatur-stand" not in r.text
        assert 'id="owa-signatur-lokal"' in r.text
    finally:
        settings_store.force_update({"OWA_SIGNATUR_LOKAL": False, "_OWA_SIGNATUR_LAST": {}})


def test_knopf_route(client, monkeypatch):
    """Ohne Schalter/Anbindung: Fehlermeldung, kein Abgleich. Mit Adresse: Ergebnis
    am Knopf; ohne: alle im Hintergrund."""
    aufrufe = []

    async def abgleich_attrappe(erzwingen=False, nur=""):
        aufrufe.append((erzwingen, nur))
        return {"ok": True, "gesetzt": 1 if nur else 2, "fehler_liste": []}
    monkeypatch.setattr(owa_signatur, "abgleich", abgleich_attrappe)
    r = client.post("/api/ews-onprem/owa-signatur", json={"adresse": HYBRID}).json()
    assert r["ok"] is False and aufrufe == []
    monkeypatch.setattr(owa_signatur, "aktiv", lambda: True)
    r = client.post("/api/ews-onprem/owa-signatur", json={"adresse": HYBRID}).json()
    assert r["ok"] is True and aufrufe == [(True, HYBRID)]
    r = client.post("/api/ews-onprem/owa-signatur", json={}).json()
    assert r["ok"] is True and r["gestartet"] is True
    import time
    for _ in range(100):                 # Hintergrundaufgabe im Loop des TestClient
        if len(aufrufe) == 2:
            break
        time.sleep(0.02)
    assert aufrufe[-1] == (True, "")
