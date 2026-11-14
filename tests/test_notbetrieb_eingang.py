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
    assert erg == {"intern": 1, "extern": 0} and raus == []


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
