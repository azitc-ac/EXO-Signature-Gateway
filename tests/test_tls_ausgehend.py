"""Ausgehendes TLS prüft das Zertifikat der Gegenstelle (v1.10.112).

Bis v1.10.111 bauten Smarthost-Rückweg, Routing-Ziele und Unzustellbarkeits-
meldung ihren TLS-Kontext mit `CERT_NONE`, und die IMAP-Ablage nahm die
Vorgabe von `imaplib`, die ebenfalls nichts prüft (darüber geht ein
Bearer-Token). Siehe `app/tls_ausgehend.py`.
"""
from __future__ import annotations

import ast
import ssl
from pathlib import Path

import pytest

import abnahme
import domain_routing
import reinject
import settings_store
import tls_ausgehend

APP = Path(__file__).resolve().parent.parent / "app"

# Wo „ohne Prüfung“ stehen darf — jeweils mit Grund.
ERLAUBT_OHNE_PRUEFUNG = {
    "tls_ausgehend.py": "der eine Ort, an dem das Abschalten gebaut wird",
}


def _quellen():
    for p in sorted(APP.rglob("*.py")):
        yield p, p.read_text(encoding="utf-8")


# ── Code-Form ────────────────────────────────────────────────────────────────

def test_cert_none_nur_im_gemeinsamen_baustein():
    treffer = []
    for p, text in _quellen():
        if p.name in ERLAUBT_OHNE_PRUEFUNG:
            continue
        for nr, zeile in enumerate(text.splitlines(), 1):
            code = zeile.split("#", 1)[0]
            if "CERT_NONE" in code or "check_hostname = False" in code \
                    or "_create_unverified_context" in code:
                treffer.append(f"{p.relative_to(APP)}:{nr}")
    assert not treffer, f"TLS ohne Prüfung ausserhalb von tls_ausgehend: {treffer}"


def _aufrufe(baum, name):
    for k in ast.walk(baum):
        if isinstance(k, ast.Call):
            f = k.func
            n = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if n == name:
                yield k


def test_starttls_und_imap_immer_mit_eigenem_kontext():
    """`smtp.starttls()` ohne `context=` und `IMAP4_SSL` ohne `ssl_context=`
    nehmen Pythons Vorgabe — und die prüft nichts."""
    fehlt = []
    for p, text in _quellen():
        baum = ast.parse(text)
        for k in _aufrufe(baum, "starttls"):
            if not any(kw.arg == "context" for kw in k.keywords):
                fehlt.append(f"{p.relative_to(APP)}:{k.lineno} starttls")
        for k in _aufrufe(baum, "IMAP4_SSL"):
            if not any(kw.arg == "ssl_context" for kw in k.keywords):
                fehlt.append(f"{p.relative_to(APP)}:{k.lineno} IMAP4_SSL")
        for k in _aufrufe(baum, "SMTP_SSL"):
            if not any(kw.arg == "context" for kw in k.keywords):
                fehlt.append(f"{p.relative_to(APP)}:{k.lineno} SMTP_SSL")
    assert not fehlt, fehlt


def test_kontext_prueft_ab_werk():
    ctx = tls_ausgehend.kontext()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    aus = tls_ausgehend.kontext(pruefen=False)
    assert aus.verify_mode == ssl.CERT_NONE and not aus.check_hostname


@pytest.mark.parametrize("host,ip", [("10.0.0.5", True), ("[2001:db8::1]", True),
                                     ("x.mail.protection.outlook.com", False), ("", False)])
def test_ist_ip(host, ip):
    assert tls_ausgehend.ist_ip(host) is ip


# ── Die Wege selbst ──────────────────────────────────────────────────────────

class _FakeSMTP:
    letzte: dict = {}

    def __init__(self, host, port, timeout=None, local_hostname=None):
        _FakeSMTP.letzte = {"host": host}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        _FakeSMTP.letzte["ctx"] = context

    def login(self, u, p):
        pass

    def sendmail(self, *a):
        pass


@pytest.fixture
def smtp(monkeypatch):
    monkeypatch.setattr(reinject.smtplib, "SMTP", _FakeSMTP)
    import aussenadresse
    monkeypatch.setattr(aussenadresse, "ehlo_hostname", lambda: "gw.test")
    return _FakeSMTP


def _werte(monkeypatch, werte):
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))


def test_smarthost_wird_geprueft(monkeypatch, smtp):
    _werte(monkeypatch, {"EXO_SMARTHOST": "x.mail.protection.outlook.com", "EXO_PORT": 25})
    monkeypatch.setattr(reinject.config, "EXO_SMARTHOST", "")
    reinject._send_smtp("a@x.de", ["b@y.de"], b"From: a@x.de\r\n\r\nx")
    ctx = smtp.letzte["ctx"]
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


@pytest.mark.parametrize("ziel,erwartet", [
    ({"host": "ex.local", "port": 25, "starttls": True}, ssl.CERT_REQUIRED),
    ({"host": "ex.local", "port": 25, "starttls": True, "tls_pruefen": True}, ssl.CERT_REQUIRED),
    ({"host": "10.0.0.5", "port": 25, "starttls": True, "tls_pruefen": False}, ssl.CERT_NONE),
])
def test_routing_ziel_prueft_nach_einstellung(monkeypatch, smtp, ziel, erwartet):
    _werte(monkeypatch, {"RELAY_TARGETS": {"onprem": ziel}, "RELAY_TARGET_PW": {}})
    reinject._relay_to_target("onprem", "a@x.de", ["b@contoso.de"], b"x")
    assert smtp.letzte["ctx"].verify_mode == erwartet


# ── Bestand: Migration, Speichern, Aufgabe ───────────────────────────────────

def test_migration_uebernimmt_bestandsziele_ohne_pruefung():
    alt = {"RELAY_TARGETS": {"onprem": {"host": "192.168.1.31", "port": 25, "starttls": True},
                             "neu": {"host": "h", "tls_pruefen": True}}}
    neu = settings_store._migrate_v6_to_v7(alt)
    assert neu["RELAY_TARGETS"]["onprem"]["tls_pruefen"] is False
    assert neu["RELAY_TARGETS"]["onprem"]["tls_uebernommen"] is True
    assert "tls_uebernommen" not in neu["RELAY_TARGETS"]["neu"]
    assert settings_store._migrate_v6_to_v7({}) == {}


def test_speichern_ist_eine_bewusste_wahl(monkeypatch):
    stand = {"RELAY_TARGETS": {"onprem": {"host": "192.168.1.31", "port": 25, "starttls": True,
                                          "tls_pruefen": False, "tls_uebernommen": True}}}
    _werte(monkeypatch, stand)
    monkeypatch.setattr(settings_store, "update", lambda d: stand.update(d))
    domain_routing.setze_ziel("onprem", "192.168.1.31", 25, True, tls_pruefen=False)
    assert stand["RELAY_TARGETS"]["onprem"] == {"host": "192.168.1.31", "port": 25,
                                                "starttls": True, "tls_pruefen": False,
                                                "user": ""}
    domain_routing.setze_ziel("zwei", "mx.contoso.de", 25, True)
    assert stand["RELAY_TARGETS"]["zwei"]["tls_pruefen"] is True


def test_aufgabe_meldet_uebernommene_ziele_und_ip_smarthost(monkeypatch):
    werte = {"RELAY_TARGETS": {}, "EXO_SMARTHOST": "x.mail.protection.outlook.com"}
    _werte(monkeypatch, werte)
    import config
    monkeypatch.setattr(config, "EXO_SMARTHOST", "")
    assert abnahme._tls_rueckweg()["zustand"] == abnahme.OK
    werte["RELAY_TARGETS"] = {"onprem": {"host": "192.168.1.31", "tls_pruefen": False,
                                         "tls_uebernommen": True}}
    p = abnahme._tls_rueckweg()
    assert p["zustand"] == abnahme.OFFEN and "onprem" in p["befund"]
    werte["RELAY_TARGETS"]["onprem"].pop("tls_uebernommen")
    assert abnahme._tls_rueckweg()["zustand"] == abnahme.OK, "bewusst gewählt = erledigt"
    werte["EXO_SMARTHOST"] = "52.101.1.1"
    p = abnahme._tls_rueckweg()
    assert p["zustand"] == abnahme.OFFEN and "IP" in p["befund"]


def test_aufgabe_ist_eingetragen():
    import aufgaben
    assert any(a.pruefung is abnahme._tls_rueckweg and a.seit == "1.10.112"
               for a in aufgaben.AUFGABEN)
