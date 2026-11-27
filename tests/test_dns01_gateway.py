"""DNS-01 über eine DNS-API im Gateway (seit v1.12.172, Katalog aus dem Relay).

Ersetzt sind nur die Fremdsysteme: die IONOS-API (httpx.MockTransport, nach
der OpenAPI-Beschreibung „DNS API“ 1.0.2), der autoritative Nameserver (ein
echter UDP-Server auf 127.0.0.1, der aus dem Zustand der Fake-IONOS antwortet)
und Let's Encrypt (prüft beim Auslösen der Challenge, ob der TXT-Wert im DNS
steht — wie der echte Validator). Drahtformat, Warten, Aufräumen, Übernahme ans
Listener-Zertifikat, Zeitplaner, Schreibweg und Oberfläche laufen echt.

Invarianten:

1. Erfolg: TXT angelegt → autoritativ sichtbar → Challenge → eigener Wert
   entfernt, fremde TXT-Werte (SPF, anderer ACME-Client) bleiben → Zertifikat
   am Listener-Pfad, Schlüssel 600, `LE_METHODE = dns01`.
2. Aufräumen IMMER (Zeitüberschreitung, ACME-Fehler, Validierung scheitert),
   und ohne sichtbaren TXT keine Challenge (sonst zählt ein Fehlversuch bei LE).
3. Kein Geheimnis in Meldung, Protokoll, API-Antwort oder Seite — auch wenn
   der Provider den Schlüssel in seiner Antwort wiederholt.
4. Erneuerung: `LE_METHODE = dns01` erneuert über DIESELBE Funktion, certbot
   wird nicht gefragt; ohne Provider oder bei importiertem Zertifikat nichts
   überschreiben, sondern den Ablauf melden. `http01` bleibt bei certbot.
5. Kein Knopf ohne Gegenstück: jedes Katalogfeld ist Einstellung, steht in der
   Einrichtung und wird gespeichert; Geheimnisse sind SECRET_KEYS, ein leeres
   oder maskiertes Geheimnis überschreibt das gespeicherte nicht.
"""
import asyncio
import datetime as _dt
import hashlib
import json
import logging
import socket
import struct
import threading
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

PRAEFIX = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
GEHEIM = "GeHeIm-Sekret-xyz-0123456789abcdefGHIJ"
SPF = "v=spf1 include:spf.protection.outlook.com -all"
FREMD_ACME = "fremder-wert-eines-anderen-acme-clients"


# ── Zertifikate (Fake-CA „Let's Encrypt“) ────────────────────────────────────

def _ca(org="Let's Encrypt"):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, org),
                      x509.NameAttribute(NameOID.COMMON_NAME, "R99")])
    jetzt = _dt.datetime.now(_dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(jetzt - _dt.timedelta(days=1)).not_valid_after(jetzt + _dt.timedelta(days=900))
            .sign(key, hashes.SHA256()))
    return key, cert


def _blatt(ca, namen, tage, public_key=None):
    ca_key, ca_cert = ca
    key = None
    if public_key is None:
        key = ec.generate_private_key(ec.SECP256R1())
        public_key = key.public_key()
    namen = [namen] if isinstance(namen, str) else list(namen)
    jetzt = _dt.datetime.now(_dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, namen[0])]))
            .issuer_name(ca_cert.subject).public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(jetzt - _dt.timedelta(days=1)).not_valid_after(jetzt + _dt.timedelta(days=tage))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in namen]), False)
            .sign(ca_key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM)
    kpem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()) if key else b""
    return pem, kpem


# ── Fake IONOS (nach dns.yaml: Zonen, Records, X-API-Key, 401) ──────────────

class FakeIonos:
    def __init__(self, echo_key_bei_401=False):
        self.zonen = {"example.com": "z-1", "example.org": "z-2"}
        self.records = [  # vorhandene, FREMDE Records
            {"id": "r-spf", "zone": "z-1", "name": "example.com", "type": "TXT", "content": SPF},
            {"id": "r-fremd", "zone": "z-1", "name": "_acme-challenge.gw.example.com",
             "type": "TXT", "content": FREMD_ACME},
            {"id": "r-a", "zone": "z-1", "name": "gw.example.com", "type": "A", "content": "192.0.2.10"},
        ]
        self.anfragen: list[tuple[str, str]] = []
        self.echo = echo_key_bei_401
        self._n = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.anfragen.append((req.method, req.url.path))
        if req.headers.get("X-API-Key") != f"{PRAEFIX}.{GEHEIM}":
            body = [{"code": "UNAUTHORIZED",
                     "message": "The customer is not authorized to do this operation."
                     + (f" key={req.headers.get('X-API-Key')}" if self.echo else "")}]
            return httpx.Response(401, json=body)
        teile = req.url.path.split("/")          # /dns/v1/zones[/id[/records[/rid]]]
        assert teile[:4] == ["", "dns", "v1", "zones"], req.url
        if len(teile) == 4 and req.method == "GET":
            return httpx.Response(200, json=[{"id": i, "name": n, "type": "NATIVE"}
                                             for n, i in self.zonen.items()])
        zid = teile[4]
        if len(teile) == 5 and req.method == "GET":
            name = req.url.params.get("recordName")
            typ = req.url.params.get("recordType")
            recs = [self._aus(r) for r in self.records if r["zone"] == zid
                    and (not name or r["name"] == name) and (not typ or r["type"] in typ.split(","))]
            return httpx.Response(200, json={"id": zid, "name": "?", "type": "NATIVE", "records": recs})
        if len(teile) == 6 and req.method == "POST":
            neu = json.loads(req.content)
            assert isinstance(neu, list), "IONOS erwartet eine LISTE von Records"
            aus = []
            for rec in neu:
                self._n += 1
                r = {"id": f"r-neu-{self._n}", "zone": zid, "name": rec["name"],
                     "type": rec["type"], "content": rec["content"]}
                self.records.append(r)
                aus.append(self._aus(r))
            return httpx.Response(201, json=aus)
        if len(teile) == 7 and req.method == "DELETE":
            vorher = len(self.records)
            self.records = [r for r in self.records if not (r["zone"] == zid and r["id"] == teile[6])]
            return httpx.Response(200 if len(self.records) < vorher else 404)
        return httpx.Response(405)

    @staticmethod
    def _aus(r):
        inhalt = f'"{r["content"]}"' if r["type"] == "TXT" else r["content"]   # IONOS liefert TXT in "…"
        return {"id": r["id"], "name": r["name"], "rootName": "x", "type": r["type"],
                "content": inhalt, "ttl": 60, "prio": 0, "disabled": False}

    def txt(self, name):
        return [r["content"] for r in self.records if r["type"] == "TXT" and r["name"] == name]


# ── Autoritativer Nameserver auf 127.0.0.1 (echtes Drahtformat) ─────────────

class FakeDns:
    """Antwortet autoritativ (AA) mit den TXT-Werten aus `quelle(name)`."""

    def __init__(self, quelle):
        self.quelle = quelle
        self.fragen = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self._stop = False
        self.faden = threading.Thread(target=self._lauf, daemon=True)
        self.faden.start()

    def _lauf(self):
        self.sock.settimeout(0.2)
        while not self._stop:
            try:
                daten, adr = self.sock.recvfrom(512)
            except (socket.timeout, OSError):
                continue
            self.fragen += 1
            kennung = daten[:2]
            pos, teile = 12, []
            while daten[pos]:
                teile.append(daten[pos + 1:pos + 1 + daten[pos]].decode())
                pos += 1 + daten[pos]
            frage = daten[12:pos + 5]
            werte = list(self.quelle(".".join(teile).lower()))
            kopf = kennung + struct.pack(">HHHHH", 0x8400, 1, len(werte), 0, 0)  # QR|AA
            rr = b""
            for w in werte:
                b = w.encode()
                rdata = b"".join(bytes([len(b[i:i + 255])]) + b[i:i + 255] for i in range(0, len(b), 255))
                rr += b"\xc0\x0c" + struct.pack(">HHIH", 16, 1, 60, len(rdata)) + rdata
            self.sock.sendto(kopf + frage + rr, adr)

    def stop(self):
        self._stop = True
        self.faden.join(2)
        self.sock.close()


# ── Fake Let's Encrypt: prüft beim Auslösen, ob der Wert im DNS steht ───────

class _Resp:
    def __init__(self, code, body, headers):
        self.status_code, self._b, self.headers, self.text = code, body, headers, json.dumps(body)

    def json(self):
        return self._b


class FakeAcme:
    ca = None
    dns = None
    aufrufe: list = []
    ausgeloest: list = []
    fehler_beim_ausloesen = None
    letzter_csr = None

    def __init__(self, directory_url, account_key, account_url=""):
        self.account_key, self.account_url = account_key, account_url
        self.directory_url = directory_url
        self._csr = None
        self._authz: dict = {}

    async def ensure_account(self, email=""):
        self.account_url = "https://acme.test/acct/1"
        return self.account_url

    async def _fetch_directory(self):
        return {"newOrder": "https://acme.test/new-order"}

    async def _post(self, url, payload):
        idents = [i["value"] for i in payload["identifiers"]]
        return _Resp(201, self._order(idents), {"Location": "https://acme.test/order/1"})

    async def new_order_dns(self, domain):
        o = self._order([domain])
        o["order_url"] = "https://acme.test/order/1"
        return o

    def _order(self, idents):
        authz = []
        for i, ident in enumerate(idents):
            url = f"https://acme.test/authz/{i}"
            wild = ident.startswith("*.")
            self._authz[url] = {"identifier": {"type": "dns", "value": ident[2:] if wild else ident},
                                "wildcard": wild, "status": "pending",
                                "challenges": [{"type": "http-01", "url": "x", "token": f"h{i}"},
                                               {"type": "dns-01", "url": f"https://acme.test/ch/{i}",
                                                "token": f"tok{i}"}]}
            authz.append(url)
        return {"authorizations": authz, "finalize": "https://acme.test/fin/1"}

    async def get_authorization(self, url):
        return self._authz[url]

    async def trigger_challenge(self, url):
        import acme_client
        FakeAcme.aufrufe.append(("trigger", url))
        if FakeAcme.fehler_beim_ausloesen:
            raise FakeAcme.fehler_beim_ausloesen
        a = next(a for a in self._authz.values() if a["challenges"][1]["url"] == url)
        tok = a["challenges"][1]["token"]
        soll = acme_client.b64url(hashlib.sha256(
            f"{tok}.{acme_client.jwk_thumbprint(self.account_key)}".encode()).digest())
        name = f"_acme-challenge.{a['identifier']['value']}"
        gesehen = FakeAcme.dns.txt(name)
        FakeAcme.ausgeloest.append((name, soll, list(gesehen)))
        a["status"] = "valid" if soll in gesehen else "invalid"
        if a["status"] == "invalid":
            a["challenges"][1]["error"] = {"detail": f"No TXT record found at {name}"}
        return {"status": "pending"}

    async def poll_order_status(self, url, timeout_sec=600):
        if self._csr is not None:
            return {"status": "valid", "certificate": "https://acme.test/cert/1",
                    "authorizations": list(self._authz)}
        st = [a["status"] for a in self._authz.values()]
        return {"status": "invalid" if "invalid" in st else "ready", "authorizations": list(self._authz)}

    async def finalize(self, url, csr_der):
        self._csr = FakeAcme.letzter_csr = x509.load_der_x509_csr(csr_der)
        return {"status": "processing"}

    async def download_certificate(self, url):
        san = self._csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        return _blatt(FakeAcme.ca, san.get_values_for_type(x509.DNSName), 90,
                      public_key=self._csr.public_key())[0]


@pytest.fixture
def einstellungen(tmp_path, monkeypatch):
    """Echter settings_store auf einer Wegwerf-Datei."""
    import settings_store
    monkeypatch.setattr(settings_store, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings_store, "_data", {})
    settings_store.init()
    return settings_store


@pytest.fixture
def dns01(einstellungen, tmp_path, monkeypatch):
    """Gateway mit Fake-IONOS, Fake-Nameserver und Fake-Let's-Encrypt."""
    import acme_client
    import config
    from dns_provider import basis, dnsabfrage
    ionos = FakeIonos()
    monkeypatch.setattr(basis.DnsProvider, "transport", httpx.MockTransport(ionos))
    server = FakeDns(lambda name: ionos.txt(name))
    monkeypatch.setattr(dnsabfrage, "PORT", server.port)
    monkeypatch.setattr(dnsabfrage, "WURZELN", ("127.0.0.1",))
    monkeypatch.setattr(dnsabfrage, "ZEITLIMIT_ABFRAGE", 0.5)
    monkeypatch.setattr(acme_client, "AcmeClient", FakeAcme)
    monkeypatch.setattr(config, "SMTP_TLS_CERT", str(tmp_path / "cert.pem"))
    monkeypatch.setattr(config, "SMTP_TLS_KEY", str(tmp_path / "key.pem"))
    FakeAcme.ca, FakeAcme.dns, FakeAcme.aufrufe, FakeAcme.ausgeloest = _ca(), ionos, [], []
    FakeAcme.fehler_beim_ausloesen = None
    einstellungen.update({"DNS01_PROVIDER": "ionos",
                          "DNS01_IONOS_PREFIX": PRAEFIX, "DNS01_IONOS_SECRET": GEHEIM,
                          "LE_DOMAIN": "gw.example.com", "LE_EMAIL": "it@example.com"})
    yield ionos, server
    server.stop()


def _auto(namen, **kw):
    import tls_acme_dns
    kw.setdefault("intervall", 0.05)
    kw.setdefault("zeitlimit", 5)
    return asyncio.run(tls_acme_dns.automatisch(namen, "it@example.com", **kw))


def _cert_namen(pfad) -> list[str]:
    c = x509.load_pem_x509_certificate(Path(pfad).read_bytes())
    return c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)


# ── 1/2: Ablauf und Aufräumen ────────────────────────────────────────────────

def test_erfolg_sichtbar_validiert_aufgeraeumt_zertifikat_am_listener(dns01, einstellungen):
    import config
    ionos, server = dns01
    info = _auto(["gw.example.com"])
    name, soll, gesehen = FakeAcme.ausgeloest[0]
    assert name == "_acme-challenge.gw.example.com" and soll in gesehen, \
        "Let's Encrypt sah den TXT-Wert beim Auslösen"
    assert server.fragen >= 1, "der Wert wurde vorher autoritativ abgefragt"
    assert ionos.txt(name) == [FREMD_ACME], "eigener Wert entfernt, fremder steht"
    assert ionos.txt("example.com") == [SPF]
    assert info["hostnames"] == ["gw.example.com"] and info["not_after"]
    assert _cert_namen(config.SMTP_TLS_CERT) == ["gw.example.com"]
    key = Path(config.SMTP_TLS_KEY)
    assert (key.stat().st_mode & 0o777) == 0o600
    assert isinstance(serialization.load_pem_private_key(key.read_bytes(), None), ec.EllipticCurvePrivateKey)
    assert einstellungen.get("LE_METHODE") == "dns01", "Erneuerung muss über DNS-01 laufen"


def test_wildcard_und_apex_zwei_werte_unter_einem_namen(dns01):
    ionos, _ = dns01
    _auto(["example.com", "*.example.com"])
    werte = {(n, s) for n, s, _g in FakeAcme.ausgeloest}
    assert {n for n, _ in werte} == {"_acme-challenge.example.com"} and len(werte) == 2
    assert all(s in g for _n, s, g in FakeAcme.ausgeloest), "beide Werte gleichzeitig sichtbar"
    assert ionos.txt("_acme-challenge.example.com") == []
    san = FakeAcme.letzter_csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["example.com", "*.example.com"]


def test_zone_nicht_gefunden_keine_challenge(dns01):
    ionos, _ = dns01
    with pytest.raises(ValueError, match="keine Zone für _acme-challenge.gw.example.net"):
        _auto(["gw.example.net"])
    assert not [a for a in ionos.anfragen if a[0] == "POST"]
    assert FakeAcme.aufrufe == [], "ohne TXT keine Challenge"


def test_zeitueberschreitung_raeumt_auf_und_loest_nicht_aus(dns01):
    ionos, server = dns01
    server.quelle = lambda name: [FREMD_ACME] if name.startswith("_acme") else []   # unser Wert kommt nie an
    with pytest.raises(ValueError, match="nicht auf allen autoritativen Nameservern"):
        _auto(["gw.example.com"], zeitlimit=0.4)
    assert ionos.txt("_acme-challenge.gw.example.com") == [FREMD_ACME], "aufgeräumt"
    assert FakeAcme.aufrufe == [], "Challenge nicht ausgelöst — sonst zählt ein Fehlversuch bei LE"


def test_aufraeumen_auch_wenn_acme_scheitert(dns01):
    import config
    ionos, _ = dns01
    FakeAcme.fehler_beim_ausloesen = RuntimeError("ACME challenge trigger failed: 500")
    with pytest.raises(RuntimeError):
        _auto(["gw.example.com"])
    assert ionos.txt("_acme-challenge.gw.example.com") == [FREMD_ACME]
    assert not Path(config.SMTP_TLS_CERT).exists(), "ohne Zertifikat nichts geschrieben"


def test_validierung_scheitert_meldung_und_aufraeumen(dns01, monkeypatch):
    ionos, _ = dns01
    echt = FakeAcme.trigger_challenge

    async def blind(self, url):
        FakeAcme.dns, gemerkt = FakeIonos(), FakeAcme.dns
        try:
            return await echt(self, url)
        finally:
            FakeAcme.dns = gemerkt
    monkeypatch.setattr(FakeAcme, "trigger_challenge", blind)
    with pytest.raises(ValueError, match="No TXT record found"):
        _auto(["gw.example.com"])
    assert ionos.txt("_acme-challenge.gw.example.com") == [FREMD_ACME]


def test_manuell_und_automatisch_teilen_die_uebernahme():
    """EIN Weg ans Listener-Zertifikat (Lebenszyklus): beide rufen _installieren."""
    import inspect
    import tls_acme_dns
    for fn in (tls_acme_dns.finish, tls_acme_dns.automatisch):
        quelle = inspect.getsource(fn)
        assert "_installieren(" in quelle and "_write_atomar" not in quelle, fn.__name__


# ── 3: Geheimnisse ───────────────────────────────────────────────────────────

def test_401_klare_meldung_ohne_geheimnis(dns01, einstellungen, caplog):
    ionos, _ = dns01
    ionos.echo = True                                    # IONOS wiederholt den Schlüssel
    einstellungen.update({"DNS01_IONOS_SECRET": GEHEIM + "-falsch"})
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ValueError) as e:
            _auto(["gw.example.com"])
    assert "Zugang abgelehnt (HTTP 401)" in str(e.value)
    assert GEHEIM not in str(e.value) and GEHEIM not in caplog.text
    assert FakeAcme.aufrufe == []


# ── 4: Erneuerung im Zeitplaner ──────────────────────────────────────────────

@pytest.fixture
def zeitplaner(dns01, monkeypatch):
    import le_certbot
    import main
    import notification
    import scheduler
    import tls_acme_dns
    ereignisse = []
    monkeypatch.setattr(scheduler, "_le_renewed_date", "")
    monkeypatch.setattr(main, "_schedule_self_restart", lambda: ereignisse.append("neustart"))
    monkeypatch.setattr(notification, "send_le_renewed", lambda d, e: ereignisse.append(("erneuert", d, e)))
    monkeypatch.setattr(notification, "send_le_expiry_alert",
                        lambda d, t, e: ereignisse.append(("ablauf", d, t)))
    monkeypatch.setattr(le_certbot, "erneuern",
                        lambda *a, **k: ereignisse.append("certbot") or ("not_managed", ""))
    echt = tls_acme_dns.automatisch
    monkeypatch.setattr(tls_acme_dns, "automatisch",
                        lambda *a, **k: echt(*a, **{**k, "zeitlimit": 5, "intervall": 0.05}))
    return ereignisse


def _bald_ablaufend(namen="gw.example.com", ca=None):
    import config
    pem, key = _blatt(ca or FakeAcme.ca, namen, 5)
    Path(config.SMTP_TLS_CERT).write_bytes(pem)
    Path(config.SMTP_TLS_KEY).write_bytes(key)


def test_erneuerung_ueber_dns01_ohne_certbot(zeitplaner, einstellungen):
    import config
    import scheduler
    einstellungen.update({"LE_METHODE": "dns01"})
    _bald_ablaufend()
    scheduler._try_le_renewal("gw.example.com", 5, "x")
    assert "certbot" not in zeitplaner
    assert "neustart" in zeitplaner and zeitplaner[0][0] == "erneuert", zeitplaner
    neu = x509.load_pem_x509_certificate(Path(config.SMTP_TLS_CERT).read_bytes())
    assert (neu.not_valid_after_utc - _dt.datetime.now(_dt.timezone.utc)).days > 80


def test_erneuerung_dns01_ohne_provider_meldet_ablauf(zeitplaner, einstellungen):
    import scheduler
    einstellungen.update({"LE_METHODE": "dns01", "DNS01_PROVIDER": ""})
    _bald_ablaufend()
    scheduler._try_le_renewal("gw.example.com", 5, "x")
    assert zeitplaner == [("ablauf", "gw.example.com", 5)], zeitplaner
    assert FakeAcme.aufrufe == []


def test_erneuerung_ueberschreibt_kein_importiertes_zertifikat(zeitplaner, einstellungen):
    import config
    import scheduler
    einstellungen.update({"LE_METHODE": "dns01"})
    _bald_ablaufend(ca=_ca("Interne CA GmbH"))
    vorher = Path(config.SMTP_TLS_CERT).read_bytes()
    scheduler._try_le_renewal("gw.example.com", 5, "x")
    assert Path(config.SMTP_TLS_CERT).read_bytes() == vorher
    assert zeitplaner == [("ablauf", "gw.example.com", 5)], zeitplaner


def test_erneuerung_http01_bleibt_bei_certbot(zeitplaner, einstellungen):
    import scheduler
    einstellungen.update({"LE_METHODE": "http01"})
    _bald_ablaufend()
    scheduler._try_le_renewal("gw.example.com", 5, "x")
    assert zeitplaner[0] == "certbot" and FakeAcme.aufrufe == []


def test_certbot_ausstellung_setzt_http01(einstellungen, monkeypatch, tmp_path):
    """Sonst bliebe nach einem Wechsel zurück zu HTTP-01 die DNS-01-Erneuerung stehen."""
    import le_certbot
    import subprocess
    einstellungen.update({"LE_METHODE": "dns01"})
    monkeypatch.setattr(le_certbot, "_dirs", lambda: {"x": tmp_path / "x", "webroot": tmp_path / "w"})
    monkeypatch.setattr(le_certbot, "_dir_flags", lambda: [])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(le_certbot, "uebernehmen", lambda: "01.01.2030")
    assert le_certbot.ausstellen("gw.example.com", "it@example.com") == (True, "01.01.2030")
    assert einstellungen.get("LE_METHODE") == "http01"


# ── 5: Einstellungen, Schreibweg, Oberfläche ─────────────────────────────────

def test_jedes_katalogfeld_ist_einstellung_und_geheimnisse_sind_secret_keys():
    import dns_provider
    import settings_store
    import tls_acme_dns
    for name, cls in dns_provider.PROVIDER.items():
        for f in cls.FELDER:
            k = tls_acme_dns.feld_schluessel(name, f.schluessel)
            assert k in settings_store.DEFAULTS, k
            assert (k in settings_store.SECRET_KEYS) == f.geheim, k
    assert tls_acme_dns.geheime_felder() == set(settings_store.DNS01_GEHEIM)
    assert "DNS01_IONOS_SECRET" in settings_store.SECRET_KEYS


@pytest.fixture
def klient(einstellungen, monkeypatch):
    pytest.importorskip("starlette.testclient")
    from starlette.testclient import TestClient
    from webui import app as wa
    from fastapi import Request

    def _angemeldet(request: Request) -> str:
        request.state.angemeldet = True
        return "testadmin"
    wa.app.dependency_overrides[wa._check_auth] = _angemeldet
    wa.app.dependency_overrides[wa._require_admin] = _angemeldet
    with TestClient(wa.app, raise_server_exceptions=False) as c:
        yield c
    wa.app.dependency_overrides.clear()


def test_maskiertes_oder_leeres_geheimnis_ueberschreibt_nicht(klient, einstellungen):
    einstellungen.update({"DNS01_IONOS_SECRET": GEHEIM})
    for wert in ("", einstellungen.MASK):
        r = klient.post("/settings", json={"DNS01_PROVIDER": "ionos", "DNS01_IONOS_SECRET": wert,
                                           "DNS01_IONOS_PREFIX": " " + PRAEFIX + " "})
        assert r.status_code == 200, r.text
        assert einstellungen.get("DNS01_IONOS_SECRET") == GEHEIM, repr(wert)
    assert einstellungen.get("DNS01_IONOS_PREFIX") == PRAEFIX, "getrimmt"
    klient.post("/settings", json={"DNS01_IONOS_SECRET": "neu-123"})
    assert einstellungen.get("DNS01_IONOS_SECRET") == "neu-123", "eingegebenes Geheimnis wird gespeichert"


@pytest.mark.parametrize("daten", [{"DNS01_PROVIDER": "erfunden"}, {"LE_METHODE": "tls-alpn"},
                                   {"DNS01_WARTEN_S": 5}, {"DNS01_IONOS_PREFIX": "a\nb"}])
def test_unzulaessiges_wird_abgelehnt(klient, einstellungen, daten):
    vorher = {k: einstellungen.get(k) for k in daten}
    r = klient.post("/settings", json=daten)
    assert r.status_code == 400 and not r.json()["ok"], r.text
    assert {k: einstellungen.get(k) for k in daten} == vorher


def test_einrichtung_zeigt_jedes_katalogfeld_ohne_geheimnis(klient, einstellungen):
    import re
    import tls_acme_dns
    einstellungen.update({"DNS01_PROVIDER": "ionos", "DNS01_IONOS_SECRET": GEHEIM,
                          "DNS01_IONOS_PREFIX": PRAEFIX})
    import config
    seite = klient.get("/setup", auth=("admin", config.WEBUI_PASSWORD or "admin")).text
    assert GEHEIM not in seite and einstellungen.MASK not in seite
    assert re.search(r'id="dns01-save-btn"[^>]*data-wache-container="#dns01-api-felder"', seite), \
        "Speichern-Knopf der DNS-01-Felder bewacht den Bereich nicht"
    bereich = re.search(r'<div id="dns01-api-felder">(.*?)<div class="actions"', seite, re.S)
    assert bereich, "Bereich der DNS-01-Felder fehlt"
    bewacht = set(re.findall(r'<(?:input|select)[^>]*\sid="([^"]+)"', bereich.group(1)))
    for p in tls_acme_dns.katalog():
        for f in p["felder"]:
            assert f'id="{f["id"]}"' in seite, f["id"]
            assert f["id"] in bewacht, f'{f["id"]} gehört zu keinem Knopf'
    assert {"dns01-provider", "DNS01_WARTEN_S"} <= bewacht
    assert 'value="' + PRAEFIX + '"' in seite
    assert "gespeichert — leer lassen = unverändert" in seite
    # Gespeichert wird über die Katalogliste der Seite, nicht über eine Handliste
    assert "const DNS01_KATALOG =" in seite and "_dns01Werte(p)" in seite
    assert "/api/setup/tls/dns01/test" in seite and "/api/setup/tls/dns01/auto" in seite


def test_verbindung_testen_route(klient, dns01, caplog):
    r = klient.post("/api/setup/tls/dns01/test", json={"provider": "ionos", "werte": {}})
    assert r.status_code == 200 and r.json()["zonen"] == ["example.com", "example.org"], r.text
    ionos, _ = dns01
    ionos.echo = True
    with caplog.at_level(logging.DEBUG):
        r = klient.post("/api/setup/tls/dns01/test",
                        json={"provider": "ionos", "werte": {"DNS01_IONOS_SECRET": "falsch-" + GEHEIM}})
    assert r.status_code == 502 and "HTTP 401" in r.json()["error"]
    assert GEHEIM not in r.text and GEHEIM not in caplog.text


def test_ausstellen_route(klient, dns01, einstellungen):
    import config
    r = klient.post("/api/setup/tls/dns01/auto", json={"domain": "gw.example.com, *.gw.example.com",
                                                       "email": "it@example.com"})
    assert r.status_code == 200, r.text
    assert sorted(_cert_namen(config.SMTP_TLS_CERT)) == ["*.gw.example.com", "gw.example.com"]
    assert einstellungen.get("LE_DOMAIN") == "gw.example.com, *.gw.example.com"
    assert GEHEIM not in r.text


def test_ausstellen_ohne_provider_abgelehnt(klient, einstellungen):
    einstellungen.update({"DNS01_PROVIDER": ""})
    r = klient.post("/api/setup/tls/dns01/auto", json={"domain": "gw.example.com"})
    assert r.status_code == 400 and "Kein DNS-Provider" in r.json()["error"]


@pytest.mark.parametrize("text, erwartet", [
    ("gw.example.com", ["gw.example.com"]),
    (" GW.example.com., *.example.com ;gw.example.com", ["gw.example.com", "*.example.com"]),
    ("", []),
])
def test_namen_aus(text, erwartet):
    import tls_acme_dns
    assert tls_acme_dns.namen_aus(text) == erwartet


@pytest.mark.parametrize("falsch", ["localhost", "a.*.example.com", "-x.example.com", "x_y.example.com"])
def test_namen_aus_lehnt_ab(falsch):
    import tls_acme_dns
    with pytest.raises(ValueError):
        tls_acme_dns.namen_aus(falsch)
