"""Direktversand über den eigenen Postfix — Konfiguration, Routing, DKIM.

Was hier gesichert wird (jeder Test schlägt fehl, wenn man den Fehler zurückbaut):
- Ausgeschaltet lässt Postfix NICHTS hinaus, auch nicht bei kaputter Einstellung.
- Zum Smarthost immer verschlüsselt; per MX opportunistisch.
- Keine Einstellung lässt sich über einen Zeilenumbruch einschleusen.
- Ohne bestandenen Port-25-Test lässt sich der MX-Modus nicht einschalten.
- Nur Empfänger AUSSERHALB der Tenant-Domänen gehen an `direkt`, und bei
  unbekannten Tenant-Domänen niemand.
- Die DKIM-Signatur verifiziert gegen den eigenen öffentlichen Schlüssel und
  bricht bei verändertem Rumpf; ohne Schlüssel bleibt die Mail unverändert.
- Postfix wandelt den Rumpf nicht um (sonst bräche DKIM).
- Exchange-interne Kopfzeilen verlassen das Haus nicht.
"""
import base64
import os
import stat
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import config  # noqa: E402
import direktversand  # noqa: E402
import dkim_signatur  # noqa: E402
import domain_routing  # noqa: E402


@pytest.fixture
def werte(monkeypatch, tmp_path):
    w = {"DIREKT_ENABLED": False, "DIREKT_MODUS": "mx", "DKIM_DOMAINS": {},
         "RELAY_TARGETS": {}, "DOMAIN_ROUTES": {}, "TENANT_ROUTES": {},
         "EXTERN_ROUTE": "", "TENANT_DOMAIN": "contoso.onmicrosoft.com"}
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: w.get(k))
    monkeypatch.setattr(settings_store, "update", lambda d: w.update(d))
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    import exo_mailboxes
    monkeypatch.setattr(exo_mailboxes, "known_addresses",
                        lambda: w.get("_adressen", {"anna@contoso.de"}))
    return w


def _e(**kw):
    e = {"aktiv": True, "modus": "mx", "smarthost": "", "smarthost_port": 587,
         "smarthost_user": ""}
    e.update(kw)
    return e


def _wert(zeilen, schluessel):
    treffer = [z.split(" = ", 1)[1] if " = " in z else ""
               for z in zeilen if z.split(" =", 1)[0] == schluessel]
    assert len(treffer) == 1, (schluessel, treffer)
    return treffer[0]


# ── Postfix-Konfiguration ───────────────────────────────────────────────────

def test_ausgeschaltet_laesst_nichts_hinaus():
    z = direktversand.postconf_zeilen(_e(aktiv=False), "mail.contoso.de")
    assert _wert(z, "default_transport").startswith("error:")


def test_mx_opportunistisch_ohne_relayhost():
    z = direktversand.postconf_zeilen(_e(), "mail.contoso.de")
    assert _wert(z, "default_transport") == "smtp"
    assert _wert(z, "relayhost") == ""
    assert _wert(z, "smtp_tls_security_level") == "may"


def test_smarthost_immer_verschluesselt():
    z = direktversand.postconf_zeilen(
        _e(modus="smarthost", smarthost="smtp.example.net", smarthost_port=2525,
           smarthost_user="u"), "mail.contoso.de")
    assert _wert(z, "relayhost") == "[smtp.example.net]:2525"
    assert _wert(z, "smtp_tls_security_level") == "encrypt"
    assert _wert(z, "smtp_sasl_auth_enable") == "yes"


def test_smarthost_ohne_host_laesst_nichts_hinaus():
    z = direktversand.postconf_zeilen(_e(modus="smarthost", smarthost=""), "x.de")
    assert _wert(z, "default_transport").startswith("error:")


@pytest.mark.parametrize("e", [_e(), _e(aktiv=False),
                               _e(modus="smarthost", smarthost="smtp.example.net")])
def test_keine_mime_umwandlung(e):
    # Sonst wandelt Postfix 8bit nach der Signatur um, und DKIM bricht.
    z = direktversand.postconf_zeilen(e, "mail.contoso.de")
    assert _wert(z, "disable_mime_output_conversion") == "yes"


def test_helo_einschleusen_unmoeglich():
    z = direktversand.postconf_zeilen(_e(), "mail.x.de\nrelayhost = [evil]:25")
    assert all("\n" not in zeile for zeile in z)
    assert _wert(z, "myhostname") == "localhost.localdomain"
    assert _wert(z, "relayhost") == ""


def test_sasl_ohne_zeilenumbruch():
    e = _e(modus="smarthost", smarthost="smtp.example.net", smarthost_user="u")
    assert direktversand.sasl_zeile(e, "geheim") == "[smtp.example.net]:587 u:geheim\n"
    with pytest.raises(ValueError):
        direktversand.sasl_zeile(e, "a\n[x]:25 y:z")


def test_schreiben_mit_rechten_600(werte):
    werte.update(DIREKT_ENABLED=True, DIREKT_MODUS="smarthost",
                 DIREKT_SMARTHOST="smtp.example.net", DIREKT_SMARTHOST_USER="u",
                 DIREKT_SMARTHOST_PW="geheim", DIREKT_HELO="mail.contoso.de")
    direktversand.schreiben()
    d = Path(config.DATA_DIR) / "mta"
    for name in ("postconf.cf", "sasl_passwd"):
        modus = stat.S_IMODE(os.stat(d / name).st_mode)
        assert modus == 0o600, (name, oct(modus))
    assert "relayhost = [smtp.example.net]:587" in (d / "postconf.cf").read_text()
    assert "u:geheim" in (d / "sasl_passwd").read_text()


def test_mx_einschalten_braucht_port25(werte, monkeypatch):
    monkeypatch.setattr(direktversand, "port25_test",
                        lambda **kw: {"ok": False, "error": "gesperrt"})
    with pytest.raises(ValueError, match="Port 25"):
        direktversand.speichern({"aktiv": True, "modus": "mx"})
    assert werte["DIREKT_ENABLED"] is False, "darf ohne Test nicht einschalten"


def test_smarthost_einschalten_ohne_port25(werte, monkeypatch):
    monkeypatch.setattr(direktversand, "port25_test",
                        lambda **kw: pytest.fail("Smarthost braucht keinen Port-25-Test"))
    direktversand.speichern({"aktiv": True, "modus": "smarthost",
                             "smarthost": "smtp.example.net", "smarthost_port": 587})
    assert werte["DIREKT_ENABLED"] is True


def test_passwort_leer_bleibt_unveraendert(werte, monkeypatch):
    werte["DIREKT_SMARTHOST_PW"] = "alt"
    direktversand.speichern({"aktiv": False, "modus": "smarthost",
                             "smarthost": "smtp.example.net", "smarthost_passwort": ""})
    assert werte["DIREKT_SMARTHOST_PW"] == "alt"


# ── Routing ─────────────────────────────────────────────────────────────────

def test_externe_empfaenger_gehen_direkt(werte):
    werte.update(DIREKT_ENABLED=True, EXTERN_ROUTE="direkt")
    g = domain_routing.gruppiere(["kunde@example.com", "anna@contoso.de",
                                  "x@contoso.onmicrosoft.com"])
    assert g == {"direkt": ["kunde@example.com"],
                 "exo": ["anna@contoso.de", "x@contoso.onmicrosoft.com"]}


def test_ohne_bekannte_domaenen_gilt_niemand_als_extern(werte):
    werte.update(DIREKT_ENABLED=True, EXTERN_ROUTE="direkt", TENANT_DOMAIN="",
                 _adressen=set())
    assert domain_routing.gruppiere(["kunde@example.com"]) == {"exo": ["kunde@example.com"]}


def test_ohne_postfachliste_gilt_niemand_als_extern_trotz_tenant_domain(werte):
    # Realfall: TENANT_DOMAIN ist nach jeder Einrichtung gesetzt, die
    # Postfachliste aber leer (Graph-Token fehlt). Die Aliasdomäne contoso.de
    # ist dann unbekannt — ohne Sicherung ginge interne Post ins Internet.
    werte.update(DIREKT_ENABLED=True, EXTERN_ROUTE="direkt",
                 TENANT_DOMAIN="contoso.onmicrosoft.com", _adressen=set())
    assert domain_routing.gruppiere(["anna@contoso.de", "kunde@example.com"]) == \
        {"exo": ["anna@contoso.de", "kunde@example.com"]}


def test_ausgeschalteter_direktversand_faellt_auf_exo(werte):
    werte.update(DIREKT_ENABLED=False, EXTERN_ROUTE="direkt",
                 DOMAIN_ROUTES={"partner.de": "direkt"})
    assert domain_routing.gruppiere(["a@example.com", "b@partner.de"]) == \
        {"exo": ["a@example.com", "b@partner.de"]}


def test_domaenenroute_vor_externroute(werte):
    werte.update(DIREKT_ENABLED=True, EXTERN_ROUTE="direkt",
                 RELAY_TARGETS={"onprem": {"host": "h"}},
                 DOMAIN_ROUTES={"partner.de": "onprem"})
    assert domain_routing.gruppiere(["b@partner.de"]) == {"onprem": ["b@partner.de"]}


def test_tenant_route_auf_ausgeschaltetes_direkt_weist_ab(werte):
    t = "ca5a81bc-ba86-4b1f-986a-ccb3a5c6b8ea"
    werte.update(TENANT_ROUTES={t: "direkt"})
    assert domain_routing.route_fuer_tenant(t) == ""
    werte["DIREKT_ENABLED"] = True
    assert domain_routing.route_fuer_tenant(t) == "direkt"


def test_direkt_ist_eingebaut(werte):
    with pytest.raises(ValueError):
        domain_routing.setze_ziel("direkt", "h", 25, True)
    domain_routing.setze_extern_route("direkt")          # vorbereitbar, auch wenn aus
    assert werte["EXTERN_ROUTE"] == "direkt"
    with pytest.raises(ValueError):
        domain_routing.setze_extern_route("gibtsnicht")
    assert domain_routing.aufloesen("direkt")["host"] == direktversand.MTA_HOST


def test_ziel_loeschen_nimmt_externroute_mit(werte):
    werte.update(RELAY_TARGETS={"sh": {"host": "h"}}, EXTERN_ROUTE="sh", RELAY_TARGET_PW={})
    domain_routing.loesche_ziel("sh")
    assert werte["EXTERN_ROUTE"] == ""


# ── DKIM ────────────────────────────────────────────────────────────────────

_MAIL = (b"From: Anna <anna@contoso.de>\r\nTo: kunde@example.com\r\n"
         b"Subject: Hallo\r\nMessage-ID: <1@contoso.de>\r\n"
         b"Content-Type: text/plain; charset=utf-8\r\n"
         b"Content-Transfer-Encoding: 8bit\r\n\r\nGr\xc3\xbc\xc3\x9fe  aus  K\xc3\xb6ln\r\n")


def _dns_aus(eintrag):
    def dnsfunc(name, timeout=5):
        assert name.decode().rstrip(".") == eintrag["name"]
        return eintrag["wert"].encode()
    return dnsfunc


def test_signatur_verifiziert_und_bricht_bei_aenderung(werte):
    import dkim
    eintrag = dkim_signatur.schluessel_erzeugen("contoso.de", "test1")
    signiert, ergebnis = dkim_signatur.signieren(_MAIL)
    assert ergebnis == "signiert:contoso.de"
    assert signiert.startswith(b"DKIM-Signature:")
    assert dkim.verify(signiert, dnsfunc=_dns_aus(eintrag))
    verfaelscht = signiert.replace(b"K\xc3\xb6ln", b"Bonn")
    assert not dkim.verify(verfaelscht, dnsfunc=_dns_aus(eintrag))


def test_schluessel_mit_rechten_600(werte):
    dkim_signatur.schluessel_erzeugen("contoso.de", "test1")
    p = Path(config.DATA_DIR) / "dkim" / "contoso.de.test1.pem"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_dns_eintrag_ist_der_oeffentliche_schluessel(werte):
    from cryptography.hazmat.primitives import serialization
    eintrag = dkim_signatur.schluessel_erzeugen("contoso.de", "test1")
    pem = (Path(config.DATA_DIR) / "dkim" / "contoso.de.test1.pem").read_bytes()
    erwartet = base64.b64encode(serialization.load_pem_private_key(pem, None)
                                .public_key().public_bytes(
                                    serialization.Encoding.DER,
                                    serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
    assert eintrag["name"] == "test1._domainkey.contoso.de"
    assert eintrag["wert"] == f"v=DKIM1; k=rsa; p={erwartet}"


def test_ohne_schluessel_unveraendert(werte):
    inhalt, ergebnis = dkim_signatur.signieren(_MAIL)
    assert (inhalt, ergebnis) == (_MAIL, "ohne_schluessel")


def test_kein_stiller_schluesselwechsel(werte):
    dkim_signatur.schluessel_erzeugen("contoso.de", "test1")
    with pytest.raises(ValueError):
        dkim_signatur.schluessel_erzeugen("contoso.de", "test2")
    assert werte["DKIM_DOMAINS"] == {"contoso.de": "test1"}


def test_entfernen_loescht_schluessel(werte):
    dkim_signatur.schluessel_erzeugen("contoso.de", "test1")
    assert dkim_signatur.entfernen("contoso.de")
    assert not list((Path(config.DATA_DIR) / "dkim").glob("contoso.de.*"))
    assert werte["DKIM_DOMAINS"] == {}


@pytest.mark.parametrize("d", ["", "localhost", "a..de", "x@y.de\n", "-a.de"])
def test_nur_gueltige_domaenen(werte, d):
    with pytest.raises(ValueError):
        dkim_signatur.schluessel_erzeugen(d)


# ── Kopfzeilen und Übergabe ─────────────────────────────────────────────────

def test_organisationskoepfe_verlassen_das_haus_nicht():
    roh = (b"From: a@contoso.de\r\n"
           b"X-MS-Exchange-Organization-SCL: -1\r\n"
           b"X-MS-Exchange-Organization-AuthAs: Internal\r\n"
           b"\tFortsetzung\r\n"
           b"X-MS-Exchange-CrossTenant-Id: abc\r\n"
           b"Subject: x\r\n\r\nRumpf\r\nX-MS-Exchange-Organization-SCL: bleibt im Rumpf\r\n")
    raus = direktversand.kopf_bereinigen(roh)
    kopf, rumpf = raus.split(b"\r\n\r\n", 1)
    assert b"organization" not in kopf.lower()
    assert b"Fortsetzung" not in kopf
    assert b"X-MS-Exchange-CrossTenant-Id: abc" in kopf
    assert rumpf == roh.split(b"\r\n\r\n", 1)[1], "Rumpf muss byte-genau bleiben"


def test_uebergabe_an_postfix_signiert_ohne_tls(werte, monkeypatch):
    werte["DIREKT_ENABLED"] = True
    eintrag = dkim_signatur.schluessel_erzeugen("contoso.de", "test1")
    gesendet = {}

    class _SMTP:
        def __init__(self, host, port, **kw):
            gesendet["ziel"] = (host, port)
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def ehlo(self): pass
        def starttls(self, **kw): gesendet["tls"] = True
        def login(self, *a): gesendet["login"] = True
        def sendmail(self, von, an, inhalt): gesendet.update(von=von, an=an, inhalt=inhalt)

    import reinject
    monkeypatch.setattr(reinject.smtplib, "SMTP", _SMTP)
    reinject._relay_to_target("direkt", "anna@contoso.de", ["kunde@example.com"], _MAIL)
    import dkim
    assert gesendet["ziel"] == (direktversand.MTA_HOST, direktversand.MTA_PORT)
    assert "tls" not in gesendet and "login" not in gesendet
    assert gesendet["an"] == ["kunde@example.com"]
    assert dkim.verify(gesendet["inhalt"], dnsfunc=_dns_aus(eintrag))


# ── SPF-Auswertung (pyspf, gegen ein erfundenes DNS) ────────────────────────

IP = "212.117.95.233"
MS = "spf.protection.outlook.com"


@pytest.fixture
def spf_dns(monkeypatch):
    """Erfundenes DNS: {name: [TXT, …]}. Fremde Namen → leer (NXDOMAIN)."""
    zonen = {MS: [f"v=spf1 ip4:40.92.0.0/15 -all"]}

    def abfrage(name, qtype, strict=True, timeout=20):
        if qtype != "TXT":
            return []
        return [((name, qtype), (t.encode(),)) for t in zonen.get(name.rstrip("."), [])]
    monkeypatch.setattr(direktversand, "_spf_dns", abfrage)
    return zonen


@pytest.mark.parametrize("eintrag,ergebnis,erlaubt", [
    # der Anlassfall: nur per include auf Microsoft — die Gateway-Adresse fehlt
    (f"v=spf1 include:{MS} ~all", "softfail", False),
    (f"v=spf1 include:{MS} -all", "fail", False),
    (f"v=spf1 include:{MS} ip4:{IP} ~all", "pass", True),
    ("v=spf1 ip4:212.117.95.0/24 -all", "pass", True),
    # ausdrücklich verboten bleibt verboten, auch wenn die Adresse dasteht
    (f"v=spf1 -ip4:{IP} +all", "fail", False),
    # bares `all` (ohne Qualifier) = pass für jeden
    ("v=spf1 all", "pass", True),
    ("v=spf1 ?all", "neutral", False),
    # redirect auf einen Eintrag, der die Adresse erlaubt
    ("v=spf1 redirect=_spf.example.com", "pass", True),
])
def test_spf_auswertung(spf_dns, eintrag, ergebnis, erlaubt):
    spf_dns["example.com"] = [eintrag]
    spf_dns["_spf.example.com"] = [f"v=spf1 ip4:{IP} -all"]
    r = direktversand.spf_auswerten("example.com", IP, "mail.example.com")
    assert (r["ergebnis"], r["erlaubt"]) == (ergebnis, erlaubt), r


def test_spf_ohne_eintrag_ist_none_und_nicht_erlaubt(spf_dns):
    r = direktversand.spf_auswerten("example.com", IP)
    assert (r["ergebnis"], r["erlaubt"]) == ("none", False)


def test_spf_mehrere_eintraege_permerror(spf_dns):
    spf_dns["example.com"] = [f"v=spf1 ip4:{IP} -all", f"v=spf1 include:{MS} ~all"]
    r = direktversand.spf_auswerten("example.com", IP)
    assert (r["ergebnis"], r["erlaubt"]) == ("permerror", False)


def test_spf_dns_stoerung_ist_unentschieden(monkeypatch):
    import spf

    def gestoert(*a, **kw):
        raise spf.TempError("DNS timeout")
    monkeypatch.setattr(direktversand, "_spf_dns", gestoert)
    r = direktversand.spf_auswerten("example.com", IP)
    assert r["ergebnis"] == "temperror" and r["erlaubt"] is None


def test_spf_absturz_der_bibliothek_ist_unentschieden(monkeypatch):
    # pyspf fängt DNS-Störungen selbst ab (Test oben); dieser Zweig deckt alles
    # andere — er darf nicht „nicht erlaubt“ behaupten, was niemand geprüft hat.
    import spf

    def kaputt(*a, **kw):
        raise RuntimeError("unerwartet")
    monkeypatch.setattr(spf, "query", kaputt)
    r = direktversand.spf_auswerten("example.com", IP)
    assert (r["ergebnis"], r["erlaubt"]) == ("temperror", None)


def test_spf_ohne_ip_keine_auswertung(monkeypatch):
    monkeypatch.setattr(direktversand, "_spf_dns",
                        lambda *a, **kw: pytest.fail("ohne Adresse keine DNS-Abfrage"))
    assert direktversand.spf_auswerten("example.com", "")["erlaubt"] is None


def test_spf_dns_ohne_veraltete_abfrage(monkeypatch):
    # pyspfs eigene Abfrage ruft dns.resolver.query() (veraltet). Unsere muss
    # resolve() benutzen und beim Auswerten tatsächlich eingesetzt sein.
    import dns.resolver
    import spf
    gerufen = []
    monkeypatch.setattr(dns.resolver, "query",
                        lambda *a, **kw: pytest.fail("veraltetes query() benutzt"))
    monkeypatch.setattr(dns.resolver, "resolve",
                        lambda name, qtype, **kw: gerufen.append((name, qtype)) or
                        (_ for _ in ()).throw(dns.resolver.NXDOMAIN()))
    direktversand.spf_auswerten("example.com", IP)
    assert ("example.com", "TXT") in gerufen
    assert spf.DNSLookup is direktversand._spf_dns




@pytest.mark.parametrize("eintrag,erwartet", [
    # der Anlassfall: ip4 VOR das abschließende all, Qualifier bleibt
    ("v=spf1 include:spf.protection.outlook.com ~all",
     f"v=spf1 include:spf.protection.outlook.com ip4:{IP} ~all"),
    ("v=spf1 include:spf.protection.outlook.com -all",
     f"v=spf1 include:spf.protection.outlook.com ip4:{IP} -all"),
    ("v=spf1 mx ?all", f"v=spf1 mx ip4:{IP} ?all"),
    # ohne all: vor redirect= (Mechanismen werden vor dem Umleiten geprüft)
    ("v=spf1 redirect=_spf.example.com", f"v=spf1 ip4:{IP} redirect=_spf.example.com"),
    # weder all noch redirect: hinten anfügen
    ("v=spf1 include:x.example", f"v=spf1 include:x.example ip4:{IP}"),
    # ausdrücklich verboten zählt nicht als erlaubt → Vorschlag
    (f"v=spf1 -ip4:{IP} ~all", f"v=spf1 -ip4:{IP} ip4:{IP} ~all"),
    # kein Eintrag: Exchange Online gehört dazu, sonst fiele dessen Post durch
    ("", f"v=spf1 ip4:{IP} include:spf.protection.outlook.com ~all"),
])
def test_spf_empfehlung(eintrag, erwartet):
    assert direktversand._spf_empfehlung(eintrag, IP) == erwartet


def test_spf_empfehlung_ohne_ip_leer():
    assert direktversand._spf_empfehlung("v=spf1 ~all", "") == ""


def test_spf_empfehlung_ergibt_pass(spf_dns):
    # Wer den Vorschlag einträgt, muss danach `pass` sehen — ausgewertet von
    # derselben Funktion wie die Anzeige, sonst schickt die Oberfläche im Kreis.
    spf_dns["_spf.example.com"] = ["v=spf1 -all"]
    for eintrag in (f"v=spf1 include:{MS} ~all", "", "v=spf1 redirect=_spf.example.com",
                    "v=spf1 mx -all", f"v=spf1 -all ip4:{IP}"):
        soll = direktversand._spf_empfehlung(eintrag, IP)
        spf_dns["example.com"] = [soll]
        r = direktversand.spf_auswerten("example.com", IP)
        assert r["ergebnis"] == "pass", (eintrag, soll, r)


def test_dns_pruefen_mehrere_spf_eintraege(werte, monkeypatch, spf_dns):
    werte["DIREKT_AUSSEN_IP"] = ""
    monkeypatch.setattr(direktversand, "_txt", lambda n: [
        "v=spf1 include:spf.protection.outlook.com ~all", "google-site-verification=x",
        f"v=spf1 ip4:{IP} ~all"])
    werte["DIREKT_AUSSEN_IP"] = IP
    import dns.resolver

    def _kein_netz(*a, **kw):
        raise dns.resolver.NoNameservers()
    monkeypatch.setattr(dns.resolver, "resolve", _kein_netz)   # PTR-Teil ohne Netz
    spf = direktversand.dns_pruefen("example.com")["spf"]
    assert len(spf["mehrere"]) == 2
    assert spf["erlaubt"] is False and spf["soll"] == "", \
        "zwei SPF-Einträge = permerror: kein Urteil „erlaubt“, kein Vorschlag"


def _ohne_ptr(monkeypatch):
    import dns.resolver

    def _kein_netz(*a, **kw):
        raise dns.resolver.NoNameservers()
    monkeypatch.setattr(dns.resolver, "resolve", _kein_netz)


def test_dns_pruefen_ohne_ip_kein_vorschlag(werte, monkeypatch, spf_dns):
    werte["DIREKT_AUSSEN_IP"] = ""
    monkeypatch.setattr(direktversand, "_txt", lambda n: [f"v=spf1 include:{MS} ~all"])
    spf = direktversand.dns_pruefen("example.com")["spf"]
    assert spf["soll"] == "" and spf["mehrere"] == [] and spf["erlaubt"] is None


def test_dns_pruefen_softfail_liefert_vorschlag(werte, monkeypatch, spf_dns):
    werte["DIREKT_AUSSEN_IP"] = IP
    spf_dns["example.com"] = [f"v=spf1 include:{MS} ~all"]
    monkeypatch.setattr(direktversand, "_txt", lambda n: spf_dns.get(n, []))
    _ohne_ptr(monkeypatch)
    spf = direktversand.dns_pruefen("example.com")["spf"]
    assert (spf["ergebnis"], spf["erlaubt"]) == ("softfail", False)
    assert spf["soll"] == f"v=spf1 include:{MS} ip4:{IP} ~all"


def test_dns_pruefen_pass_ohne_vorschlag(werte, monkeypatch, spf_dns):
    # erlaubt per include (nicht ausdrücklich per ip4): trotzdem nichts zu tun
    werte["DIREKT_AUSSEN_IP"] = IP
    spf_dns["example.com"] = ["v=spf1 include:_spf.example.com ~all"]
    spf_dns["_spf.example.com"] = [f"v=spf1 ip4:{IP} -all"]
    monkeypatch.setattr(direktversand, "_txt", lambda n: spf_dns.get(n, []))
    _ohne_ptr(monkeypatch)
    spf = direktversand.dns_pruefen("example.com")["spf"]
    assert (spf["ergebnis"], spf["erlaubt"], spf["soll"]) == ("pass", True, "")
