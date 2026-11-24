"""Notbetrieb: Post von außen im ausgerufenen Notfall (notbetrieb_eingang).

Die Zusagen: kein offenes Relay (nur Postfächer des eigenen Tenants, auch über
Aliase), Greylisting nur für das Internet, Post anderer Microsoft-Firmen wird
nicht vom Tenant-Gate abgewiesen, eigene Exchange-Post bleibt auf dem
Connector-Weg — und ohne ausgerufenen Notfall ändert sich nichts.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import config  # noqa: E402
import notbetrieb  # noqa: E402
import notbetrieb_eingang as ne  # noqa: E402
import notbetrieb_rechte  # noqa: E402
import settings_store  # noqa: E402

TENANT = "11111111-2222-3333-4444-555555555555"
MS_IP = "40.92.10.10"
NETZ_IP = "203.0.113.7"
ROH = json.dumps({
    "konten": [{"primary": "a@firma.at", "upn": "a@firma.onmicrosoft.com", "typ": "UserMailbox"},
               {"primary": "team@firma.at", "upn": "team@firma.at", "typ": "SharedMailbox"}],
    "rechte": []})


class _Sitzung:
    def __init__(self, ip):
        self.peer = (ip, 5000)
        self.ssl = None
        self.authenticated = False


@pytest.fixture
def notfall(monkeypatch, tmp_path):
    werte = {"NOTBETRIEB_AKTIV": True, "NOTBETRIEB_AUSGERUFEN": True, "TENANT_ID": TENANT,
             "MAILBOX_CONFIG": {}, "REINJECT_MODE": "smtp", "SMTP_RELAY_ENABLED": False}
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "TENANT_ID", "", raising=False)
    monkeypatch.setattr(settings_store, "get", lambda k, d=None, *a, **kw: werte.get(k, d))
    notbetrieb_rechte.speichern(notbetrieb_rechte.parse(ROH))
    with notbetrieb._conn() as conn:
        conn.execute("INSERT INTO profile (adresse, daten) VALUES (?,?)",
                     ("anna.muster@firma.at", json.dumps({"mail": "a@firma.at"})))
    import exo_erkennung
    import smtp_relay
    monkeypatch.setattr(exo_erkennung, "_adresse_reicht", lambda ip: ip == MS_IP)
    monkeypatch.setattr(exo_erkennung, "zertifikat_von_exo", lambda c: False)
    monkeypatch.setattr(exo_erkennung, "_peercert", lambda s: None)
    monkeypatch.setattr(smtp_relay, "ist_vorgelagertes_gateway", lambda ip: False)
    monkeypatch.setattr(smtp_relay, "ist_relay_quelle", lambda ip: False)
    monkeypatch.setattr(ne, "_takt", {})
    monkeypatch.setattr(ne, "_zaehler", {})
    uhr = {"t": 1_800_000_000.0}
    monkeypatch.setattr(ne.time, "time", lambda: uhr["t"])
    werte["_uhr"] = uhr
    return werte


def _rcpt(ip, rcpt, von="x@extern.de", bisher=0, sitzung=None):
    return ne.rcpt_pruefen(ip, sitzung or _Sitzung(ip), von, rcpt, bisher)


# ── Empfänger ───────────────────────────────────────────────────────────────

def test_unbekannter_empfaenger_wird_beim_rcpt_abgewiesen_kein_relay(notfall):
    s = _Sitzung(NETZ_IP)
    for _ in range(ne.MAX_FEHLVERSUCHE):
        assert _rcpt(NETZ_IP, "kunde@anderswo.de", sitzung=s).startswith("550")
    assert _rcpt(NETZ_IP, "a@firma.at", sitzung=s).startswith("421")


@pytest.mark.parametrize("adresse", ["a@firma.at", "A@Firma.AT", "a@firma.onmicrosoft.com",
                                     "anna.muster@firma.at", "team@firma.at"])
def test_postfach_ueber_primaer_upn_und_alias(notfall, adresse):
    assert notbetrieb.postfach_fuer(adresse) in ("a@firma.at", "team@firma.at")


def test_alias_wird_intern_zugestellt_nicht_an_den_eigenen_mx(notfall, monkeypatch):
    import reinject
    raus = []
    monkeypatch.setattr(reinject, "_an_postfix", lambda *a: raus.append(a))
    erg = notbetrieb.zustellen("x@extern.de", ["anna.muster@firma.at"], b"Subject: x\r\n\r\nx\r\n")
    assert erg == {"intern": 1, "extern": 0, "weiter": 0, "eingereiht": 0} and raus == []


# ── Greylisting ─────────────────────────────────────────────────────────────

def test_greylisting_erst_aufschieben_dann_durch_dann_netz_bekannt(notfall):
    assert _rcpt(NETZ_IP, "a@firma.at").startswith("451")
    notfall["_uhr"]["t"] += 60
    assert _rcpt(NETZ_IP, "a@firma.at").startswith("451")          # zu früh
    notfall["_uhr"]["t"] += ne.GREY_WARTEN_S
    assert _rcpt(NETZ_IP, "a@firma.at") is None
    # Anderer Absender, andere Adresse im selben /24: Netz ist bekannt.
    assert _rcpt("203.0.113.99", "team@firma.at", von="y@extern.de") is None
    # Fremdes Netz fängt von vorn an.
    assert _rcpt("198.51.100.1", "a@firma.at").startswith("451")


def test_greylisting_abschaltbar(notfall):
    notfall["NOTBETRIEB_EINGANG_GREYLIST"] = False
    assert _rcpt(NETZ_IP, "a@firma.at") is None


def test_microsoft_ohne_greylisting_und_unbekannte_erst_nach_dem_kopf(notfall):
    """Aus Microsofts Bereich kann es der eigene Connector sein (Post nach
    außen) — beim RCPT also nicht abweisen; nach DATA greift die Prüfung."""
    assert _rcpt(MS_IP, "a@firma.at") is None
    assert _rcpt(MS_IP, "kunde@anderswo.de") is None
    assert ne.data_pruefen(ne.MICROSOFT, MS_IP, ["kunde@anderswo.de"], 100).startswith("550")
    assert ne.quelle_nach_kopf(ne.MICROSOFT, TENANT.upper()) == ne.CONNECTOR
    assert ne.quelle_nach_kopf(ne.MICROSOFT, "99999999-0000-0000-0000-000000000000") == ne.MICROSOFT
    assert ne.quelle_nach_kopf(ne.INTERNET, TENANT) == ne.INTERNET   # Kopf aus dem Internet zählt nicht


# ── Begrenzungen ────────────────────────────────────────────────────────────

def test_begrenzungen(notfall):
    notfall["NOTBETRIEB_EINGANG_GREYLIST"] = False
    notfall["NOTBETRIEB_EINGANG_MAX_EMPF"] = 2
    assert _rcpt(NETZ_IP, "a@firma.at", bisher=2).startswith("452")
    notfall["NOTBETRIEB_EINGANG_MAX_MB"] = 1
    assert ne.data_pruefen(ne.INTERNET, NETZ_IP, ["a@firma.at"], 2 * 1024 * 1024).startswith("552")
    notfall["NOTBETRIEB_EINGANG_JE_STUNDE"] = 2
    assert ne.data_pruefen(ne.INTERNET, NETZ_IP, ["a@firma.at"], 10) is None
    assert ne.data_pruefen(ne.INTERNET, "203.0.113.50", ["a@firma.at"], 10) is None   # selbes Netz
    assert ne.data_pruefen(ne.INTERNET, NETZ_IP, ["a@firma.at"], 10).startswith("451")
    notfall["_uhr"]["t"] += 3601
    assert ne.data_pruefen(ne.INTERNET, NETZ_IP, ["a@firma.at"], 10) is None
    assert ne.zaehler()["gedrosselt"] == 1


# ── Ohne Notfall ändert sich nichts ─────────────────────────────────────────

@pytest.mark.parametrize("aktiv, ausgerufen, angemeldet", [(True, False, False), (False, True, False),
                                                           (True, True, True)])
def test_ohne_notfall_oder_mit_anmeldung_keine_pruefung(notfall, aktiv, ausgerufen, angemeldet):
    notfall["NOTBETRIEB_AKTIV"], notfall["NOTBETRIEB_AUSGERUFEN"] = aktiv, ausgerufen
    s = _Sitzung(NETZ_IP)
    s.authenticated = angemeldet
    assert _rcpt(NETZ_IP, "kunde@anderswo.de", sitzung=s) is None


# ── Durch den echten Handler ────────────────────────────────────────────────

@pytest.fixture
def handler_lauf(notfall, monkeypatch):
    import direktversand
    import handler
    import mail_audit
    import reinject
    raus, exchange = [], []
    monkeypatch.setattr(direktversand, "aktiv", lambda: True)
    monkeypatch.setattr(reinject, "_an_postfix", lambda von, empf, inhalt, h, p: raus.append(empf))
    monkeypatch.setattr(reinject, "_senden", lambda *a, **k: exchange.append(a[1]))
    monkeypatch.setattr(mail_audit, "log_event", lambda **kw: None)
    # Echtheitsprüfung ohne Netz: keine Einträge, SPF none. Das Gerüst `dns`
    # überschreibt das, wo ein Test Einträge braucht.
    import notbetrieb_echtheit
    monkeypatch.setattr(notbetrieb_echtheit, "_txt", lambda name: [])
    monkeypatch.setattr(direktversand, "spf_auswerten", lambda d, ip, helo="": {"ergebnis": "none"})

    class _U:
        def __init__(self, von, empf, inhalt):
            self.mail_from, self.rcpt_tos, self.content = von, empf, inhalt

    def lauf(ip, von, empf, tenant=""):
        kopf = f"X-MS-Exchange-CrossTenant-Id: {tenant}\r\n" if tenant else ""
        inhalt = (f"From: {von}\r\nTo: {', '.join(empf)}\r\nSubject: Notfall\r\n{kopf}"
                  f"Message-ID: <n1@extern.de>\r\n\r\nHallo\r\n").encode()
        return asyncio.run(handler.SignatureHandler().handle_DATA(None, _Sitzung(ip), _U(von, empf, inhalt)))
    return lauf, raus, exchange


def _kopie(pf):
    with notbetrieb._conn() as conn:
        return [dict(z) for z in conn.execute("SELECT ordner, herkunft FROM mails WHERE postfach=?", (pf,))]


def test_handler_internet_an_internes_postfach_landet_in_der_kopie(handler_lauf):
    lauf, raus, exchange = handler_lauf
    assert lauf(NETZ_IP, "x@extern.de", ["anna.muster@firma.at"]).startswith("250")
    assert _kopie("a@firma.at") == [{"ordner": "inbox", "herkunft": "lokal"}]
    assert raus == [] and exchange == []


def test_handler_internet_an_fremde_adresse_ist_kein_relay(handler_lauf):
    lauf, raus, exchange = handler_lauf
    assert lauf(NETZ_IP, "x@extern.de", ["kunde@anderswo.de"]).startswith("550")
    assert raus == [] and exchange == []


def test_handler_andere_microsoft_firma_passiert_das_tenant_gate(handler_lauf):
    """Bisher: fremde Tenant-Kennung → 554. Im Notfall schreibt uns so jede
    andere M365-Firma — sie muss ankommen."""
    lauf, raus, exchange = handler_lauf
    antwort = lauf(MS_IP, "kunde@m365firma.de", ["a@firma.at"], tenant="99999999-0000-0000-0000-000000000000")
    assert antwort.startswith("250"), antwort
    assert _kopie("a@firma.at") == [{"ordner": "inbox", "herkunft": "lokal"}]


def test_handler_eigene_exchange_post_bleibt_connector_weg(handler_lauf):
    """Eigener Tenant aus Microsofts Bereich mit externem Empfänger: kein
    Notfall-Eingang (sonst 550), sondern der normale Weg — und der geht im
    Notfall über den Direktversand hinaus."""
    lauf, raus, exchange = handler_lauf
    assert lauf(MS_IP, "a@firma.at", ["kunde@anderswo.de"], tenant=TENANT).startswith("250")
    assert raus == [["kunde@anderswo.de"]] and exchange == []


def test_handler_ohne_notfall_weist_internet_wie_bisher_ab(handler_lauf, notfall):
    lauf, raus, exchange = handler_lauf
    notfall["NOTBETRIEB_AUSGERUFEN"] = False
    assert lauf(NETZ_IP, "x@extern.de", ["a@firma.at"]).startswith("554")
    assert raus == [] and exchange == []


def test_handler_rcpt_haken_weist_ab_und_uebernimmt_nur_angenommene(notfall):
    import handler

    class _U:
        mail_from = "x@extern.de"

        def __init__(self):
            self.rcpt_tos, self.rcpt_options = [], []
    u = _U()
    h = handler.SignatureHandler()
    notfall["NOTBETRIEB_EINGANG_GREYLIST"] = False
    assert asyncio.run(h.handle_RCPT(None, _Sitzung(NETZ_IP), u, "kunde@anderswo.de", [])).startswith("550")
    assert asyncio.run(h.handle_RCPT(None, _Sitzung(NETZ_IP), u, "a@firma.at", [])) == "250 OK"
    assert u.rcpt_tos == ["a@firma.at"]
    notfall["NOTBETRIEB_AUSGERUFEN"] = False      # ohne Notfall: alles wie aiosmtpd ohne Haken
    assert asyncio.run(h.handle_RCPT(None, _Sitzung(NETZ_IP), u, "kunde@anderswo.de", [])) == "250 OK"


# ── Echtheit: SPF, DKIM, DMARC ──────────────────────────────────────────────

import base64 as _b64  # noqa: E402

import notbetrieb_echtheit as echt  # noqa: E402


@pytest.fixture
def dns(monkeypatch):
    """TXT-Einträge als Wörterbuch; SPF-Ergebnis je Umschlagdomäne."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    schluessel = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    pem = schluessel.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                   serialization.NoEncryption())
    pub = schluessel.public_key().public_bytes(serialization.Encoding.DER,
                                               serialization.PublicFormat.SubjectPublicKeyInfo)
    eintraege = {"sel._domainkey.absender.de": [f"v=DKIM1; k=rsa; p={_b64.b64encode(pub).decode()}"]}
    spf = {}
    monkeypatch.setattr(echt, "_txt", lambda name: (_ for _ in ()).throw(echt.DnsGestoert("weg"))
                        if eintraege.get("_gestoert") == name else eintraege.get(name, []))
    import direktversand
    monkeypatch.setattr(direktversand, "spf_auswerten",
                        lambda domain, ip, helo="": {"ergebnis": spf.get(domain, "none")})
    return {"txt": eintraege, "spf": spf, "pem": pem}


def _mail(von="info@absender.de", signiert_von=None, dns=None, rumpf=b"Hallo\r\n"):
    roh = (f"From: {von}\r\nTo: a@firma.at\r\nSubject: Echt?\r\nMessage-ID: <e1@absender.de>\r\n"
           f"Date: Sat, 14 Nov 2026 10:00:00 +0000\r\n\r\n").encode() + rumpf
    if signiert_von:
        import dkim
        roh = dkim.sign(roh, b"sel", signiert_von.encode(), dns["pem"],
                        include_headers=[b"from", b"to", b"subject"]) + roh
    return roh


def _urteil(roh, mail_from="bounce@absender.de"):
    return echt.pruefen(roh, mail_from, NETZ_IP, "mx.absender.de")


def test_dmarc_pass_ueber_passendes_dkim(notfall, dns):
    dns["txt"]["_dmarc.absender.de"] = ["v=DMARC1; p=reject"]
    u = _urteil(_mail(signiert_von="absender.de", dns=dns))
    assert u["entscheidung"] == echt.POSTEINGANG
    assert "dkim=pass header.d=absender.de" in u["ar"] and "dmarc=pass" in u["ar"]


def test_dkim_nach_veraenderung_ungueltig_und_p_reject_weist_ab(notfall, dns):
    dns["txt"]["_dmarc.absender.de"] = ["v=DMARC1; p=reject"]
    roh = _mail(signiert_von="absender.de", dns=dns).replace(b"Hallo", b"Zahle")
    u = _urteil(roh)
    assert u["entscheidung"] == echt.ABWEISEN and "dkim=none" in u["ar"]


def test_dmarc_quarantine_in_junk_und_spf_pass_rettet(notfall, dns):
    dns["txt"]["_dmarc.absender.de"] = ["v=DMARC1; p=quarantine"]
    assert _urteil(_mail())["entscheidung"] == echt.JUNK
    dns["spf"]["absender.de"] = "pass"
    assert _urteil(_mail())["entscheidung"] == echt.POSTEINGANG
    # SPF für eine FREMDE Umschlagdomäne passt nicht zum Absender → kein DMARC-pass.
    dns["spf"]["massenversand.example"] = "pass"
    assert _urteil(_mail(), mail_from="b@massenversand.example")["entscheidung"] == echt.JUNK


def test_ohne_dmarc_nur_spf_fail_in_junk(notfall, dns):
    assert _urteil(_mail())["entscheidung"] == echt.POSTEINGANG
    dns["spf"]["absender.de"] = "fail"
    assert _urteil(_mail())["entscheidung"] == echt.JUNK


def test_baumsuche_findet_richtlinie_der_elterndomain_mit_sp(notfall, dns):
    dns["txt"]["_dmarc.absender.de"] = ["v=DMARC1; p=none; sp=reject"]
    u = _urteil(_mail(von="info@news.absender.de"), mail_from="b@news.absender.de")
    assert u["entscheidung"] == echt.ABWEISEN


def test_eigene_domaene_aus_dem_internet_ohne_bestaetigung_abgewiesen(notfall, dns):
    """Ohne DMARC-Eintrag — die Fälschung eines internen Absenders käme sonst an."""
    assert _urteil(_mail(von="chef@firma.at"), mail_from="chef@firma.at")["entscheidung"] == echt.ABWEISEN
    dns["spf"]["firma.at"] = "pass"
    assert _urteil(_mail(von="chef@firma.at"), mail_from="chef@firma.at")["entscheidung"] == echt.POSTEINGANG


def test_dns_gestoert_spaeter(notfall, dns):
    dns["txt"]["_gestoert"] = "_dmarc.absender.de"
    assert _urteil(_mail())["entscheidung"] == echt.SPAETER


def test_gefaelschte_authentication_results_werden_entfernt():
    roh = (b"Authentication-Results: gw.firma.at; dmarc=pass\r\n  header.from=bank.de\r\n"
           b"Authentication-Results: anderer.example; spf=pass\r\nFrom: x@bank.de\r\n\r\nx\r\n")
    neu = echt.kopf_setzen(roh, "gw.firma.at", "dmarc=fail")
    assert neu.startswith(b"Authentication-Results: gw.firma.at; dmarc=fail\r\n")
    assert neu.count(b"gw.firma.at") == 1 and b"header.from=bank.de" not in neu
    assert b"Authentication-Results: anderer.example; spf=pass" in neu


def test_handler_junk_ordner_und_zweite_mail_erbt_nichts(handler_lauf, dns, monkeypatch):
    """Junk landet in Junk-E-Mail. Microsoft liefert über EINE Verbindung oft
    Post mehrerer Firmen: Kommt danach eigene Exchange-Post (Connector-Weg, ohne
    Echtheitsprüfung), darf sie die Entscheidung „Junk“ nicht erben."""
    import handler
    dns["txt"]["_dmarc.absender.de"] = ["v=DMARC1; p=quarantine"]

    class _U:
        def __init__(self, von, inhalt):
            self.mail_from, self.rcpt_tos, self.content = von, ["a@firma.at"], inhalt
    sitzung = _Sitzung(MS_IP)
    fremd = _mail().replace(b"Subject:", b"X-MS-Exchange-CrossTenant-Id: 99999999-0000-0000-0000-000000000000\r\nSubject:")
    eigen = (f"From: team@firma.at\r\nTo: a@firma.at\r\nSubject: intern\r\n"
             f"X-MS-Exchange-CrossTenant-Id: {TENANT}\r\nMessage-ID: <i1@firma.at>\r\n\r\nx\r\n").encode()

    async def zwei():
        h = handler.SignatureHandler()
        return (await h.handle_DATA(None, sitzung, _U("bounce@absender.de", fremd)),
                await h.handle_DATA(None, sitzung, _U("team@firma.at", eigen)))
    assert asyncio.run(zwei()) == ("250 OK", "250 OK")
    assert sorted(z["ordner"] for z in _kopie("a@firma.at")) == ["inbox", "junkemail"]


def test_handler_weist_gefaelschten_internen_absender_ab(handler_lauf, dns, monkeypatch):
    import notbetrieb_eingang
    monkeypatch.setattr(notbetrieb_eingang, "greylisting_an", lambda: False)
    lauf, raus, exchange = handler_lauf
    assert lauf(NETZ_IP, "chef@firma.at", ["a@firma.at"]).startswith("550 5.7.1")
    assert _kopie("a@firma.at") == []


# ── Verteiler, Gruppen, externe Benutzer ────────────────────────────────────

GAL = json.loads(ROH) | {
    "konten": json.loads(ROH)["konten"] + [{"primary": "b@firma.at", "upn": "b@firma.at", "typ": "UserMailbox"}],
    "gruppen": [
        {"adresse": "alle@firma.at", "aliase": ["alle@firma.at", "alle.leute@firma.at"], "name": "Alle",
         "art": "verteiler", "versteckt": False, "nur_intern": True,
         "mitglieder": ["a@firma.at", "innen@firma.at", "partner@firma.at", None]},
        # Verschachtelt und im Kreis: innen enthält alle, alle enthält innen.
        {"adresse": "innen@firma.at", "aliase": "innen@firma.at", "name": "Innen", "art": "verteiler",
         "versteckt": True, "nur_intern": False, "mitglieder": ["b@firma.at", "alle@firma.at"]},
        {"adresse": "projekt@firma.at", "aliase": [], "name": "Projekt", "art": "gruppe",
         "versteckt": False, "nur_intern": False, "mitglieder": "team@firma.at"},
        {"adresse": "leer@firma.at", "aliase": [None], "name": "Leer", "art": "gruppe",
         "versteckt": False, "nur_intern": False, "mitglieder": [None]},
        {"adresse": "falsch@firma.at", "art": "unbekannt", "mitglieder": ["a@firma.at"]}],
    "extern": [
        # Die externe Adresse steht (wie live gesehen) auch unter den Proxyadressen.
        {"adresse": "partner@firma.at", "aliase": ["partner@firma.at", "partner@anderswo.de"], "name": "Partner",
         "art": "extern", "versteckt": False, "ziel": "Partner@Anderswo.de"},
        {"adresse": "lieferant@lieferant.de", "aliase": ["lieferant@lieferant.de"], "name": "Lieferant",
         "art": "kontakt", "versteckt": False, "ziel": "lieferant@lieferant.de"},
        {"adresse": "ohne@firma.at", "aliase": [], "name": "Ohne", "art": "extern", "versteckt": False, "ziel": ""},
        # Gemeinsamer Namensraum mit on-prem (live gesehen): externe Adresse = eigene Adresse.
        {"adresse": "hybrid@firma.at", "aliase": ["hybrid@firma.at"], "name": "Hybrid", "art": "extern",
         "versteckt": False, "ziel": "hybrid@firma.at"}]}


@pytest.fixture
def gal(notfall):
    notbetrieb_rechte.speichern(notbetrieb_rechte.parse(json.dumps(GAL)))
    notfall["NOTBETRIEB_EINGANG_GREYLIST"] = False
    return notfall


def test_gruppen_werden_flach_aufgeloest_mit_zyklenschutz():
    e = {x["adresse"]: x for x in notbetrieb_rechte.parse(json.dumps(GAL))["empfaenger"]}
    assert e["alle@firma.at"]["ziele"] == ["a@firma.at", "b@firma.at", "partner@anderswo.de"]
    assert e["innen@firma.at"]["ziele"] == ["b@firma.at", "a@firma.at", "partner@anderswo.de"]
    assert e["projekt@firma.at"]["ziele"] == ["team@firma.at"]
    assert e["leer@firma.at"]["ziele"] == [] and e["ohne@firma.at"]["ziele"] == []
    # On-Prem-Postfach: die eigene Adresse ist das Ziel (Weiterleitung entscheidet aufloesen).
    assert e["hybrid@firma.at"]["ziele"] == ["hybrid@firma.at"] and e["hybrid@firma.at"]["onprem"]
    assert not e["partner@firma.at"]["onprem"] and not e["lieferant@lieferant.de"]["onprem"]
    assert e["alle@firma.at"]["aliase"] == ["alle.leute@firma.at", "alle@firma.at"]
    assert e["partner@firma.at"]["aliase"] == ["partner@firma.at"]
    assert e["lieferant@lieferant.de"]["aliase"] == [] and e["lieferant@lieferant.de"]["ziele"] == ["lieferant@lieferant.de"]
    assert "falsch@firma.at" not in e


def test_aufloesen_verteiler_alias_und_externer_benutzer(gal):
    intern, extern, weiter, ohne = notbetrieb.aufloesen(["alle.leute@firma.at", "Partner@firma.at", "kunde@x.de",
                                                         "a@firma.at"])
    assert weiter == {} and ohne == []
    assert intern == ["a@firma.at", "b@firma.at"]
    assert extern == ["partner@anderswo.de", "kunde@x.de"]


def test_verteiler_wird_an_mitglieder_zugestellt_nicht_an_den_eigenen_mx(gal, monkeypatch):
    import direktversand
    import domain_routing
    import reinject
    raus = []
    monkeypatch.setattr(direktversand, "aktiv", lambda: True)
    monkeypatch.setattr(domain_routing, "aufloesen", lambda z: {"host": "127.0.0.1", "port": 10025})
    monkeypatch.setattr(reinject, "_an_postfix", lambda von, empf, *a: raus.append(empf))
    erg = notbetrieb.zustellen("x@extern.de", ["alle@firma.at", "projekt@firma.at"], b"Subject: x\r\n\r\nx\r\n")
    assert raus == [["partner@anderswo.de"]]
    assert erg == {"intern": 3, "extern": 1, "weiter": 0, "eingereiht": 0}
    with notbetrieb._conn() as conn:
        bei = {z["postfach"] for z in conn.execute("SELECT postfach FROM mails WHERE herkunft='lokal'")}
    assert bei == {"a@firma.at", "b@firma.at", "team@firma.at"}


def test_eingang_nimmt_gruppen_an_und_achtet_nur_intern(gal):
    assert _rcpt(NETZ_IP, "projekt@firma.at") is None
    assert _rcpt(NETZ_IP, "partner@firma.at") is None
    assert _rcpt(NETZ_IP, "alle.leute@firma.at").startswith("550 5.7.1")
    assert ne.data_pruefen(ne.INTERNET, NETZ_IP, ["projekt@firma.at"], 10) is None
    assert ne.data_pruefen(ne.INTERNET, NETZ_IP, ["a@firma.at", "alle@firma.at"], 10).startswith("550 5.7.1")
    # Aus Microsofts Bereich erst nach dem Kopf — dann gilt die Sperre auch.
    assert _rcpt(MS_IP, "alle@firma.at") is None
    assert ne.data_pruefen(ne.MICROSOFT, MS_IP, ["alle@firma.at"], 10).startswith("550 5.7.1")
    assert _rcpt(NETZ_IP, "unbekannt@firma.at").startswith("550 5.1.1")
    # Fremde Domänen von externen Benutzern und Kontakten: kein Relay nach außen.
    for fremd in ("partner@anderswo.de", "lieferant@lieferant.de"):
        assert _rcpt(NETZ_IP, fremd).startswith("550 5.1.1"), fremd
        assert ne.data_pruefen(ne.INTERNET, NETZ_IP, [fremd], 10).startswith("550 5.1.1"), fremd
    assert notbetrieb.aufloesen(["lieferant@lieferant.de"]) == ([], ["lieferant@lieferant.de"], {}, [])
    # Ohne erreichbares Ziel: nicht annehmen (Post käme nirgends an), beim Senden wie bisher nach außen.
    for leer in ("leer@firma.at", "ohne@firma.at"):
        assert _rcpt(NETZ_IP, leer).startswith("550 5.1.1"), leer
        assert notbetrieb.aufloesen([leer]) == ([], [leer], {}, []), leer
    # On-Prem-Postfach ohne Notfall-Ziel: abweisen, und NICHT nach außen (eigener MX).
    assert _rcpt(NETZ_IP, "hybrid@firma.at").startswith("550 5.1.1")
    assert notbetrieb.aufloesen(["hybrid@firma.at"]) == ([], [], {}, ["hybrid@firma.at"])


# ── On-Prem-Postfächer (gemeinsamer Namensraum) ─────────────────────────────

GAL_ONPREM = GAL | {"gruppen": GAL["gruppen"] + [
    {"adresse": "mitonprem@firma.at", "aliase": ["mitonprem@firma.at"], "name": "Mit On-Prem", "art": "verteiler",
     "versteckt": False, "nur_intern": False, "mitglieder": ["a@firma.at", "hybrid@firma.at"]}]}
MID = b"Message-ID: <onprem-1@extern.de>\r\nSubject: x\r\n\r\nx\r\n"


@pytest.fixture
def onprem(notfall, monkeypatch):
    """Abbild mit On-Prem-Postfach hybrid@ (auch im Verteiler mitonprem@),
    Zielserver „onprem“; Weiterleitung, Warteschlange und Statistik abgefangen."""
    import domain_routing
    import reinject
    import relay_stats
    notbetrieb_rechte.speichern(notbetrieb_rechte.parse(json.dumps(GAL_ONPREM)))
    notfall["NOTBETRIEB_EINGANG_GREYLIST"] = False
    notfall["RELAY_TARGETS"] = {"onprem": {"host": "192.0.2.25", "port": 25}}
    monkeypatch.setattr(domain_routing, "_weitergeleitet", {})
    lauf = {"weiter": [], "warteschlange": [], "statistik": []}
    monkeypatch.setattr(reinject, "_relay_to_target", lambda zid, von, empf, inhalt, *a: lauf["weiter"].append((zid, empf)))
    monkeypatch.setattr(reinject, "_fail_delivery",
                        lambda von, empf, inhalt, grund, queue: lauf["warteschlange"].append((empf, grund)))
    monkeypatch.setattr(relay_stats, "merke_routing", lambda zid, n, ok=True, **kw: lauf["statistik"].append((zid, ok)))
    notfall["_lauf"] = lauf
    return notfall


def test_onprem_postfach_geht_an_das_notfall_ziel(onprem):
    """(a) Ein On-Prem-Postfach mit Ziel wird weitergeleitet — nicht „extern“
    an den eigenen MX, nicht abgewiesen."""
    assert notbetrieb.aufloesen(["hybrid@firma.at"]) == ([], [], {}, ["hybrid@firma.at"])   # noch ohne Ziel
    onprem["NOTBETRIEB_ONPREM_ZIEL"] = "onprem"
    assert notbetrieb.aufloesen(["Hybrid@Firma.at", "kunde@x.de"]) == ([], ["kunde@x.de"],
                                                                        {"onprem": ["hybrid@firma.at"]}, [])
    assert notbetrieb.bekannt("hybrid@firma.at") and _rcpt(NETZ_IP, "hybrid@firma.at") is None
    erg = notbetrieb.zustellen("x@extern.de", ["hybrid@firma.at"], MID)
    assert onprem["_lauf"]["weiter"] == [("onprem", ["hybrid@firma.at"])]
    assert erg == {"intern": 0, "extern": 0, "weiter": 1, "eingereiht": 0}
    assert onprem["_lauf"]["statistik"] == [("onprem", True)]


def test_domaenen_route_hat_vorrang_exo_und_direkt_nie(onprem, monkeypatch):
    import direktversand
    monkeypatch.setattr(direktversand, "aktiv", lambda: True)   # sonst verwirft route_fuer „direkt“ schon selbst
    onprem["NOTBETRIEB_ONPREM_ZIEL"] = "onprem"
    onprem["RELAY_TARGETS"]["standort"] = {"host": "192.0.2.26", "port": 25}
    onprem["DOMAIN_ROUTES"] = {"firma.at": "standort"}
    assert notbetrieb.onprem_ziel("hybrid@firma.at") == "standort"
    # Route auf EXO oder Direktversand: im Notfall kein Weg zum On-Prem-Server → Notfall-Ziel.
    for route in ("exo", "direkt"):
        onprem["DOMAIN_ROUTES"] = {"firma.at": route}
        assert notbetrieb.onprem_ziel("hybrid@firma.at") == "onprem", route
    # Ein Notfall-Ziel, das es nicht (mehr) gibt, ist keines.
    onprem["NOTBETRIEB_ONPREM_ZIEL"] = "geloescht"
    assert notbetrieb.onprem_ziel("hybrid@firma.at") is None


def test_verteiler_behaelt_onprem_mitglied(onprem):
    """(b) Bis 11/2026 verlor ein Verteiler ein On-Prem-Mitglied still."""
    e = {x["adresse"]: x for x in notbetrieb_rechte.parse(json.dumps(GAL_ONPREM))["empfaenger"]}
    assert e["mitonprem@firma.at"]["ziele"] == ["a@firma.at", "hybrid@firma.at"]
    onprem["NOTBETRIEB_ONPREM_ZIEL"] = "onprem"
    erg = notbetrieb.zustellen("x@extern.de", ["mitonprem@firma.at"], MID)
    assert erg == {"intern": 1, "extern": 0, "weiter": 1, "eingereiht": 0}
    assert onprem["_lauf"]["weiter"] == [("onprem", ["hybrid@firma.at"])]


def test_onprem_ohne_ziel_wird_abgewiesen_nie_an_den_eigenen_mx(onprem, monkeypatch):
    """(c) Ohne Ziel: Eingang 550; ein Verteiler bleibt annehmbar, das
    On-Prem-Mitglied geht in die Warteschlange — nie per Direktversand an den
    eigenen MX (der gehört im Notfall zum ausgefallenen Exchange)."""
    import direktversand
    import reinject
    raus = []
    monkeypatch.setattr(direktversand, "aktiv", lambda: True)
    monkeypatch.setattr(reinject, "_an_postfix", lambda von, empf, *a: raus.append(empf))
    assert _rcpt(NETZ_IP, "hybrid@firma.at").startswith("550 5.1.1")
    assert ne.data_pruefen(ne.INTERNET, NETZ_IP, ["hybrid@firma.at"], 10).startswith("550 5.1.1")
    assert _rcpt(NETZ_IP, "mitonprem@firma.at") is None
    erg = notbetrieb.zustellen("x@extern.de", ["mitonprem@firma.at", "hybrid@firma.at"], MID)
    assert raus == [] and onprem["_lauf"]["weiter"] == []
    assert onprem["_lauf"]["warteschlange"] == [(["hybrid@firma.at"], "notbetrieb: On-Prem-Postfach ohne Ziel")]
    assert erg == {"intern": 1, "extern": 0, "weiter": 0, "eingereiht": 1}


def test_unbekannte_adresse_der_onprem_domaene_wird_nicht_weitergeleitet(onprem):
    """(d) Nur gelistete On-Prem-Postfächer — kein offenes Relay in die Domäne."""
    onprem["NOTBETRIEB_ONPREM_ZIEL"] = "onprem"
    onprem["DOMAIN_ROUTES"] = {"firma.at": "onprem"}
    assert _rcpt(NETZ_IP, "gibtsnicht@firma.at").startswith("550 5.1.1")
    assert not notbetrieb.bekannt("gibtsnicht@firma.at")
    assert notbetrieb.aufloesen(["gibtsnicht@firma.at"]).weiter == {}


def test_schleifenbremse_und_quelle_gleich_ziel(onprem):
    """(e) Dieselbe Mail an dieselben Empfänger höchstens SCHLEIFE_MAX-mal;
    was vom On-Prem-Ziel selbst kommt, geht nie dorthin zurück."""
    import domain_routing
    onprem["NOTBETRIEB_ONPREM_ZIEL"] = "onprem"
    for _ in range(domain_routing.SCHLEIFE_MAX):
        assert notbetrieb.zustellen("x@extern.de", ["hybrid@firma.at"], MID)["weiter"] == 1
    erg = notbetrieb.zustellen("x@extern.de", ["hybrid@firma.at"], MID)
    assert erg["weiter"] == 0 and erg["eingereiht"] == 1
    assert "Schleife" in onprem["_lauf"]["warteschlange"][-1][1]
    # Andere Mail, aber eingeliefert vom Ziel-Host selbst.
    marke = notbetrieb.eingang_quelle.set("192.0.2.25")
    try:
        erg = notbetrieb.zustellen("x@extern.de", ["hybrid@firma.at"], MID.replace(b"onprem-1", b"onprem-2"))
    finally:
        notbetrieb.eingang_quelle.reset(marke)
    assert erg["weiter"] == 0 and "kommt vom On-Prem-Ziel" in onprem["_lauf"]["warteschlange"][-1][1]
    assert len(onprem["_lauf"]["weiter"]) == domain_routing.SCHLEIFE_MAX


def test_ziel_aus_wird_eingereiht_und_gezaehlt(onprem, monkeypatch):
    import reinject

    def aus(*a):
        raise ConnectionRefusedError("aus")
    monkeypatch.setattr(reinject, "_relay_to_target", aus)
    onprem["NOTBETRIEB_ONPREM_ZIEL"] = "onprem"
    erg = notbetrieb.zustellen("x@extern.de", ["hybrid@firma.at", "a@firma.at"], MID)
    assert erg == {"intern": 1, "extern": 0, "weiter": 0, "eingereiht": 1}
    assert onprem["_lauf"]["statistik"] == [("onprem", False)]
    assert onprem["_lauf"]["warteschlange"][0][0] == ["hybrid@firma.at"]


def test_bereitschaft_zeigt_onprem_immer_mit_bezugsgroesse(onprem, monkeypatch):
    import abnahme
    import notbetrieb_bereitschaft as nbb
    p = nbb._onprem()
    assert p["zustand"] == abnahme.OFFEN and "1 Postfächer liegen on-prem, 1 davon ohne Ziel" in p["befund"]
    assert "nicht erreichbar" in p["befund"] and "Ziel wählen" in p["zu_tun"]
    onprem["NOTBETRIEB_ONPREM_ZIEL"] = "onprem"
    p = nbb._onprem()
    assert p["zustand"] == abnahme.OFFEN and "noch nie geprüft" in p["befund"]
    # Probe: Verbindung wie die Zustellung, bis RCPT, ohne DATA.
    import reinject
    gesprochen = []

    class _S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def mail(self, von): gesprochen.append(("MAIL", von))
        def rcpt(self, an): gesprochen.append(("RCPT", an)); return 250, b"2.1.5 Recipient OK"
        def rset(self): gesprochen.append(("RSET",))
        def sendmail(self, *a): raise AssertionError("Probe darf nichts senden")
        def data(self, *a): raise AssertionError("Probe darf nichts senden")
    monkeypatch.setattr(reinject, "_ziel_sitzung", lambda zid, cfg: _S())
    erg = notbetrieb.onprem_probe()
    assert erg["ok"] and erg["ziel"] == "onprem" and erg["adresse"] == "hybrid@firma.at"
    assert gesprochen == [("MAIL", ""), ("RCPT", "hybrid@firma.at"), ("RSET",)]
    p = nbb._onprem()
    assert p["zustand"] == abnahme.OK and "1 Postfächer liegen on-prem, 0 davon ohne Ziel" in p["befund"]
    # Ohne On-Prem-Postfächer: entfällt, aber die Zeile steht (mit 0).
    notbetrieb_rechte.speichern(notbetrieb_rechte.parse(ROH))
    p = nbb._onprem()
    assert p["zustand"] == abnahme.ENTFAELLT and p["befund"].startswith("0 Postfächer liegen on-prem")


def test_stand_zaehlt_je_art(gal):
    assert notbetrieb_rechte.stand()["empfaenger"] == {"verteiler": 2, "gruppe": 2, "extern": 3, "kontakt": 1}


def test_rechte_skript_liest_gruppen_und_externe():
    s = notbetrieb_rechte._ps_script("app", "firma.onmicrosoft.com")
    for teil in ("Get-DistributionGroupMember", "Get-DynamicDistributionGroupMember",
                 "Get-UnifiedGroupLinks", "-LinkType Members", "RequireSenderAuthenticationEnabled",
                 "ExternalEmailAddress", "MailUser,MailContact", "RoomList", "gruppen = $gruppen"):
        assert teil in s, teil
