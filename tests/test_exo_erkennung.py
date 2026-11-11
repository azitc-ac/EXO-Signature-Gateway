"""Exchange Online an Adresse und/oder Client-Zertifikat erkennen (v1.11.24).

ANLASS (11.11.2026): Ein Kunden-Gateway hinter einer Firewall mit Source-NAT
sah Exchange Online als 10.99.0.1 und wies jede Mail ab. Exchange Online legt
beim Einliefern ein Client-Zertifikat auf mail.protection.outlook.com vor —
dasselbe, das ein Exchange vor Ort im Hybrid-Empfang prüft (belegt am selben Tag,
Serial 0B3937694B175E269928529C54B662D0).

Geprüft:
* am ECHTEN Listener mit STARTTLS: wann das Zertifikat angefordert wird und was
  `exo_erkennung.pruefe` daraus macht, in allen drei Stufen;
* am Handler: Annahme bzw. 554, Tenant-Gate bleibt auch mit Zertifikat;
* die Alternative ohne Zertifikat: Firewall-Adresse als „vorgelagertes Gateway“.
"""
import asyncio
import datetime
import logging
import smtplib
import socket
import ssl
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import exo_erkennung   # noqa: E402
import handler         # noqa: E402
import main            # noqa: E402
import smtp_cert       # noqa: E402

EXO = "mail.protection.outlook.com"
OWN = "11111111-1111-1111-1111-111111111111"
FREMD = "99999999-9999-9999-9999-999999999999"


# ── Zertifikate ──────────────────────────────────────────────────────────────

def _zert(name, aussteller_key=None, aussteller_name=None, ca=False):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    jetzt = datetime.datetime.now(datetime.timezone.utc)
    b = (x509.CertificateBuilder().subject_name(subj)
         .issuer_name(aussteller_name or subj).public_key(key.public_key())
         .serial_number(x509.random_serial_number())
         .not_valid_before(jetzt - datetime.timedelta(days=1))
         .not_valid_after(jetzt + datetime.timedelta(days=1))
         .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    return key, b.sign(aussteller_key or key, hashes.SHA256()), subj


def _pem(tmp_path, datei, key, cert):
    from cryptography.hazmat.primitives import serialization
    (tmp_path / f"{datei}.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / f"{datei}.key").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return str(tmp_path / f"{datei}.crt"), str(tmp_path / f"{datei}.key")


class _Merker:
    """Handler-Ersatz: hält fest, was exo_erkennung für die Sitzung entscheidet."""
    def __init__(self):
        self.urteile = []

    async def handle_DATA(self, server, session, envelope):
        self.urteile.append(exo_erkennung.pruefe(session.peer[0], session))
        return "250 OK"


@pytest.fixture
def aufbau(monkeypatch, tmp_path):
    import certifi
    import runtime_state
    import settings_store
    import smtp_acl
    import smtp_relay
    werte = {"EXO_ERKENNUNG": "eins"}
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))
    erlaubt = {"ja": False}
    monkeypatch.setattr(smtp_acl, "is_allowed", lambda ip: erlaubt["ja"])
    relay = {"ja": False}
    monkeypatch.setattr(smtp_relay, "ist_relay_quelle", lambda ip: relay["ja"])
    monkeypatch.setattr(smtp_relay, "ist_vorgelagertes_gateway", lambda ip: False)
    ca_key, ca_cert, ca_name = _zert("Test-CA", ca=True)
    ca_datei, _ = _pem(tmp_path, "ca", ca_key, ca_cert)
    monkeypatch.setattr(certifi, "where", lambda: ca_datei)     # „öffentliche“ CAs = Test-CA
    srv = _pem(tmp_path, "server", *_zert("localhost")[:2])
    exo = _pem(tmp_path, "exo", *_zert(EXO, ca_key, ca_name)[:2])
    fremd = _pem(tmp_path, "fremd", *_zert("mail.example.org", ca_key, ca_name)[:2])
    monkeypatch.setattr(runtime_state, "tls_kontext_anfordern",
                        smtp_cert.baue_listener_kontext([srv], client_zert=True), raising=False)
    merker = _Merker()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    ctrl = main._LenientController(merker, hostname="127.0.0.1", port=port,
                                   tls_context=smtp_cert.baue_listener_kontext([srv]),
                                   require_starttls=True)
    ctrl.start()
    yield {"port": port, "werte": werte, "erlaubt": erlaubt, "relay": relay,
           "merker": merker, "exo": exo, "fremd": fremd}
    ctrl.stop()


def _senden(port, client=None):
    k = ssl.create_default_context()
    k.check_hostname = False
    k.verify_mode = ssl.CERT_NONE
    if client:
        k.load_cert_chain(*client)
    c = smtplib.SMTP()
    c._host = "localhost"
    c.connect("127.0.0.1", port)
    c.ehlo("exchange.test")
    c.starttls(context=k)
    c.ehlo("exchange.test")
    c.sendmail("a@firma.at", ["b@extern.de"], b"Subject: x\r\n\r\nx\r\n")
    c.quit()


# ── Am Listener ──────────────────────────────────────────────────────────────

def test_hinter_nat_traegt_das_zertifikat(aufbau):
    """Adresse nicht zugelassen (Firewall mit Source-NAT), EXO-Zertifikat → erkannt."""
    _senden(aufbau["port"], aufbau["exo"])
    assert aufbau["merker"].urteile == [(True, "Zertifikat")]


def test_zertifikat_mit_anderem_namen_reicht_nicht(aufbau):
    _senden(aufbau["port"], aufbau["fremd"])
    assert aufbau["merker"].urteile == [(False, "keins")]


def test_ohne_zertifikat_und_ohne_adresse_abgelehnt(aufbau):
    _senden(aufbau["port"])
    assert aufbau["merker"].urteile == [(False, "keins")]


def test_bei_zugelassener_adresse_wird_nichts_angefordert(aufbau):
    """Vorgabe: Wo die Adresse reicht, wird kein Zertifikat angefordert — die
    Anforderung bricht bei unprüfbaren Zertifikaten ab und darf keine
    Verbindung treffen, die ohne sie durchginge."""
    aufbau["erlaubt"]["ja"] = True
    _senden(aufbau["port"], aufbau["exo"])
    assert aufbau["merker"].urteile == [(True, "Adresse")]


def test_relay_geraet_bekommt_keine_anforderung(aufbau):
    aufbau["relay"]["ja"] = True
    _senden(aufbau["port"], aufbau["exo"])
    assert aufbau["merker"].urteile[0][1] == "keins"


def test_streng_verlangt_beides(aufbau):
    aufbau["werte"]["EXO_ERKENNUNG"] = "beide"
    aufbau["erlaubt"]["ja"] = True
    _senden(aufbau["port"], aufbau["exo"])
    _senden(aufbau["port"])
    assert aufbau["merker"].urteile == [(True, "Adresse+Zertifikat"), (False, "Adresse")]


def test_nur_adresse_wie_bisher(aufbau):
    aufbau["werte"]["EXO_ERKENNUNG"] = "adresse"
    _senden(aufbau["port"], aufbau["exo"])
    assert aufbau["merker"].urteile == [(False, "keins")]


def test_wurzel_des_exo_clientzertifikats_liegt_bei():
    """Die Wurzel fehlt in certifi; ohne die mitgelieferte Datei scheiterte die
    Prüfung des echten EXO-Zertifikats (Raspi, 11.11.2026)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    cert = x509.load_pem_x509_certificate(
        (WURZEL / "app" / "smtp_clientzert_wurzeln.pem").read_bytes())
    assert cert.fingerprint(hashes.SHA1()).hex().upper() == \
        "A8985D3A65E5E5C4B2D7D66D40C6DD2FB19C5436"


@pytest.mark.parametrize("peercert,erwartet", [
    ({"subjectAltName": (("DNS", EXO),)}, True),
    ({"subjectAltName": (("DNS", "MAIL.Protection.Outlook.com"),)}, True),
    ({"subject": ((("commonName", EXO),),)}, True),
    ({"subjectAltName": (("DNS", "*.mail.protection.outlook.com"),)}, False),
    ({"subjectAltName": (("DNS", "mail.protection.outlook.com.evil.example"),)}, False),
    ({}, False),
    (None, False),
])
def test_name_wird_exakt_geprueft(peercert, erwartet):
    assert exo_erkennung.zertifikat_von_exo(peercert) is erwartet


# ── Am Handler ───────────────────────────────────────────────────────────────

class _Sitzung:
    def __init__(self, ip, peercert=None):
        self.peer = (ip, 5000)
        self.ssl = {"peercert": peercert} if peercert else None


class _Umschlag:
    def __init__(self, content):
        self.mail_from = "wer@example.com"
        self.rcpt_tos = ["ziel@example.com"]
        self.content = content


def _nachricht(tenant):
    return (f"Subject: Hallo\r\nX-MS-Exchange-CrossTenant-Id: {tenant}\r\n\r\nText\r\n").encode()


@pytest.fixture
def anlage(monkeypatch, tmp_path):
    werte = {"TENANT_ID": OWN, "RELAY_TENANT_CHECK": True, "SMTP_RELAY_ENABLED": True,
             "REINJECT_MODE": "smtp", "MAILBOX_CONFIG": {}, "EXO_ERKENNUNG": "eins",
             "SMTP_RELAY_LERN_NETZE": [], "SMTP_RELAY_LERN_BIS": ""}
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))
    import smtp_acl
    monkeypatch.setattr(smtp_acl, "is_allowed", lambda ip: ip.startswith("40."))
    monkeypatch.setattr(smtp_acl, "record_reject", lambda ip: None)
    import relay_hosts
    monkeypatch.setattr(relay_hosts, "DB_PATH", tmp_path / "relay_hosts.db")
    zugestellt = []
    import reinject
    monkeypatch.setattr(reinject, "send", lambda *a, **kw: zugestellt.append(a) or True)
    import mail_audit
    monkeypatch.setattr(mail_audit, "log_event", lambda **kw: None)
    import config
    monkeypatch.setattr(config, "TENANT_ID", "")
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    werte["_zugestellt"] = zugestellt
    return werte


def _lauf(ip, peercert=None, tenant=OWN):
    h = handler.SignatureHandler()
    return asyncio.run(h.handle_DATA(None, _Sitzung(ip, peercert), _Umschlag(_nachricht(tenant))))


EXO_PEERCERT = {"subject": ((("commonName", EXO),),), "subjectAltName": (("DNS", EXO),)}


def test_handler_nimmt_exo_hinter_nat_am_zertifikat_an(anlage):
    assert _lauf("10.99.0.1", EXO_PEERCERT).startswith("250")
    assert anlage["_zugestellt"]


def test_handler_weist_ohne_zertifikat_ab(anlage):
    assert _lauf("10.99.0.1") == "554 5.7.1 Access denied"
    assert not anlage["_zugestellt"]


def test_tenant_gate_bleibt_auch_mit_zertifikat(anlage):
    """Das Zertifikat sagt „aus Exchange Online“, nicht „aus diesem Tenant“."""
    assert _lauf("10.99.0.1", EXO_PEERCERT, tenant=FREMD).startswith("554")
    assert not anlage["_zugestellt"]


def test_alternative_firewall_als_vorgelagertes_gateway(anlage):
    """Ohne Zertifikat: Firewall-Adresse mit Rolle „vorgelagertes Gateway“ →
    wie der Connector angenommen. Ohne den Eintrag: 554 (der Fall vom 11.11.)."""
    assert _lauf("10.99.0.1") == "554 5.7.1 Access denied"
    import relay_hosts
    relay_hosts.speichern("10.99.0.1", gateway=True, kommentar="Firewall")
    assert _lauf("10.99.0.1").startswith("250")
    assert anlage["_zugestellt"]


# ── Abgewiesene eigene Exchange-Post → Band und Abnahme ──────────────────────

def test_abgewiesene_eigene_post_wird_gemeldet(anlage):
    assert exo_erkennung.abweisungen_gemeldet() is None
    assert _lauf("10.99.0.1") == "554 5.7.1 Access denied"
    ab = exo_erkennung.abweisungen_gemeldet()
    assert ab and ab["anzahl"] == 1 and ab["ip"] == "10.99.0.1"
    import abnahme
    assert abnahme._exo_erkennung()["zustand"] == abnahme.OFFEN


def test_fremde_abgewiesene_post_meldet_nichts(anlage):
    """Scanner und fremde Tenants gehören nicht ins Band — nur eigene Post."""
    assert _lauf("10.99.0.1", tenant=FREMD).startswith("554")
    assert exo_erkennung.abweisungen_gemeldet() is None


def test_alte_abweisungen_fallen_heraus(anlage, tmp_path):
    import json
    from datetime import timedelta, timezone
    alt = (datetime.datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    (tmp_path / "exo_abgewiesen.json").write_text(json.dumps([{"ts": alt, "ip": "1.2.3.4"}]))
    assert exo_erkennung.abweisungen_gemeldet() is None
    import abnahme
    assert abnahme._exo_erkennung()["zustand"] == abnahme.OK
