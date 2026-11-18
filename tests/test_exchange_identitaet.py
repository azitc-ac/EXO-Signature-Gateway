"""Anmeldung per Exchange-Identitätstoken (Konto auf Exchange lokal, seit v1.12.77).

Ein Outlook-Konto in der lokalen Exchange-Organisation bekommt kein NAA; das
Verfassen-Add-in schickt dann das Exchange-Identitätstoken
(`getUserIdentityTokenAsync`). Der Endpunkt nimmt es OHNE Sitzung an — das
Token ist die ganze Anmeldung. Darum muss JEDE Prüfung einzeln greifen
(MS Learn, „Validate an Exchange identity token“): typ/alg/x5t, aud, nbf/exp,
appctx.version, amurl (nur der Host der eingetragenen EWS-Adresse), Signatur
gegen das Metadaten-Dokument — und die Zuordnung NUR zu Hybrid-Nutzern. Ein
Exchange-Online-Postfach darf über diesen Weg nie angemeldet werden.

Testdaten erfunden (firma.at, Max Mustermann); Zertifikat und Token werden im
Test erzeugt, Zeiten relativ zu jetzt.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import json
import time

import pytest

import exchange_identitaet as ei
import exo_mailboxes
import graph_client
import policies
import settings_store
import signature_engine
from webui.routen import addin, addin_verfassen as av

HYBRID = "max.mustermann@firma.at"
CLOUD = "erika.musterfrau@firma.at"
G_HYBRID = "22222222-aaaa-4bbb-8ccc-222222222222"
G_CLOUD = "11111111-aaaa-4bbb-8ccc-111111111111"
G_FREMD = "33333333-aaaa-4bbb-8ccc-333333333333"
BASE = "https://sig.firma.at"
AUD = BASE + av.LAUFZEIT_HTML
AMURL = "https://mail.firma.at:443/autodiscover/metadata/json/1"


# ── Schlüssel, Zertifikat, Metadaten-Dokument im Exchange-Format ─────────────

def _zertifikat():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Exchange Auth Certificate")])
    jetzt = dt.datetime.now(dt.timezone.utc)
    zert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(jetzt - dt.timedelta(days=1))
            .not_valid_after(jetzt + dt.timedelta(days=365))
            .sign(key, hashes.SHA256()))
    return key, zert.public_bytes(serialization.Encoding.DER)


KEY, DER = _zertifikat()
KEY2, DER2 = _zertifikat()


def _x5t(der):
    return base64.urlsafe_b64encode(hashlib.sha1(der).digest()).rstrip(b"=").decode()


def _metadaten(der=DER, x5t=None):
    """Aufbau wie /autodiscover/metadata/json/1 einer Exchange SE (gemessen 18.11.2026)."""
    return {"id": "_test", "version": "1.0", "name": "Exchange", "realm": "firma.at",
            "serviceName": ei.EXCHANGE_PRINZIPAL, "issuer": ei.EXCHANGE_PRINZIPAL + "@firma.at",
            "allowedAudiences": [ei.EXCHANGE_PRINZIPAL + "@firma.at"],
            "keys": [{"usage": "signing", "keyinfo": {"x5t": x5t or _x5t(der)},
                      "keyvalue": {"type": "x509Certificate",
                                   "value": base64.b64encode(der).decode()}}],
            "endpoints": [{"location": "https://ex01.firma.at:444/autodiscover/metadata/json/1",
                           "protocol": "OAuth2", "usage": "metadata"}]}


def _b64(d) -> str:
    roh = d if isinstance(d, bytes) else json.dumps(d, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(roh).rstrip(b"=").decode()


def _signieren(key, daten: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    return key.sign(daten, padding.PKCS1v15(), hashes.SHA256())


def _token(key=KEY, kopf=None, ctx=None, appctx_als_objekt=False, **nutzlast):
    jetzt = int(time.time())
    k = {"typ": "JWT", "alg": "RS256", "x5t": _x5t(DER)}
    k.update(kopf or {})
    c = {"msexchuid": f"{G_HYBRID}@ex01.firma.at", "version": "ExIdTok.V1", "amurl": AMURL}
    c.update(ctx or {})
    c = {a: b for a, b in c.items() if b is not None}
    n = {"aud": AUD, "iss": ei.EXCHANGE_PRINZIPAL + "@ex01.firma.at",
         "nbf": str(jetzt - 60), "exp": str(jetzt + 8 * 3600),
         "appctxsender": ei.EXCHANGE_PRINZIPAL + "@ex01.firma.at", "isbrowserhostedapp": "false",
         # Echte Tokens tragen appctx als JSON-TEXT (siehe exchange_identitaet._appctx).
         "appctx": c if appctx_als_objekt else json.dumps(c)}
    n.update(nutzlast)
    n = {a: b for a, b in n.items() if b is not None}
    k = {a: b for a, b in k.items() if b is not None}
    vorne = f"{_b64(k)}.{_b64(n)}"
    return vorne + "." + _b64(_signieren(key, vorne.encode()))


# ── Umgebung ─────────────────────────────────────────────────────────────────

@pytest.fixture
def umgebung(monkeypatch):
    daten = {"ADDIN_BASE_URL": BASE, "HYBRID_POSTFAECHER": True, "REINJECT_MODE": "smtp",
             "EWS_ONPREM_URL": "https://mail.firma.at/EWS/Exchange.asmx",
             "MAILBOX_CONFIG": {
                 G_HYBRID: {"sig": True, "hybrid": True, "primary": HYBRID,
                            "known_addresses": [HYBRID, "mm@firma.at"]},
                 G_CLOUD: {"sig": True, "primary": CLOUD, "known_addresses": [CLOUD]}}}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: daten.get(k, d))
    monkeypatch.setattr(settings_store, "force_update", lambda p: daten.update(p))
    monkeypatch.setattr(exo_mailboxes, "_cache", [
        {"guid": G_CLOUD, "primary": CLOUD, "addresses": [CLOUD], "type": "UserMailbox"},
        {"guid": G_HYBRID, "primary": HYBRID, "addresses": [HYBRID, "mm@firma.at"],
         "type": exo_mailboxes.HYBRID_TYP}])
    monkeypatch.setattr(exo_mailboxes, "_cache_ts", time.monotonic())
    ei.zwischenspeicher_leeren()
    abrufe: list[str] = []
    meta = {"doc": _metadaten()}

    def _laden(url):
        abrufe.append(url)
        return meta["doc"]
    monkeypatch.setattr(ei, "metadaten_laden", _laden)
    yield daten, abrufe, meta
    ei.zwischenspeicher_leeren()


def _pruefen(token, aud=AUD, jetzt=None):
    return ei.pruefen(token, aud, jetzt=jetzt)


# ── Gültig ───────────────────────────────────────────────────────────────────

def test_gueltiges_token_ergibt_den_hybrid_nutzer(umgebung):
    _d, abrufe, _m = umgebung
    assert _pruefen(_token()) == HYBRID
    # Geladen wird die aus der EWS-Adresse gebaute Adresse, nicht die aus dem Token.
    assert abrufe == ["https://mail.firma.at/autodiscover/metadata/json/1"]


def test_appctx_als_objekt_wie_in_der_dokumentation(umgebung):
    assert _pruefen(_token(appctx_als_objekt=True)) == HYBRID


def test_nach_neustart_nur_aus_der_konfiguration(umgebung, monkeypatch):
    """Leerer Zwischenspeicher: der MAILBOX_CONFIG-Schlüssel (ExchangeGuid) mit
    hybrid=True trägt die Zuordnung."""
    monkeypatch.setattr(exo_mailboxes, "_cache", [])
    assert _pruefen(_token()) == HYBRID


# ── Jede Prüfung einzeln: Mutationen des Tokens ─────────────────────────────

def _manipuliert_nach_signatur():
    """Nutzlast nach dem Signieren getauscht — die GUID eines anderen."""
    t = _token()
    k, _n, s = t.split(".")
    n = json.loads(base64.urlsafe_b64decode(_n + "=" * (-len(_n) % 4)))
    ctx = json.loads(n["appctx"])
    ctx["msexchuid"] = f"{G_FREMD}@ex01.firma.at"
    n["appctx"] = json.dumps(ctx)
    return f"{k}.{_b64(n)}.{s}"


@pytest.mark.parametrize("token, grund", [
    (lambda: _token(key=KEY2), "Signatur"),                                 # fremder Schlüssel, gleiches x5t
    (_manipuliert_nach_signatur, "Signatur"),
    (lambda: _token()[:-4] + "AAAA", "Signatur"),
    (lambda: _token(kopf={"alg": "none"}), "alg"),
    (lambda: _token(kopf={"alg": "HS256"}), "alg"),
    (lambda: _token(kopf={"typ": "JOSE"}), "typ"),
    (lambda: _token(kopf={"x5t": None}), "x5t"),
    (lambda: _token(kopf={"x5t": _x5t(DER2)}), "Signaturschlüssel"),       # unbekannter Schlüssel
    (lambda: _token(aud=BASE + "/addin/compose"), "aud"),                   # anderes Add-in
    (lambda: _token(aud="https://boese.example" + av.LAUFZEIT_HTML), "aud"),
    (lambda: _token(aud=None), "aud"),
    (lambda: _token(iss="11111111-0000-0000-0000-000000000000@ex01.firma.at"), "iss"),
    (lambda: _token(exp=str(int(time.time()) - ei.SPIELRAUM_S - 5)), "abgelaufen"),
    (lambda: _token(nbf=str(int(time.time()) + ei.SPIELRAUM_S + 60)), "noch nicht"),
    (lambda: _token(exp="morgen"), "exp"),
    (lambda: _token(exp=None), "exp"),
    (lambda: _token(ctx={"version": "ExIdTok.V2"}), "version"),
    (lambda: _token(ctx={"version": None}), "version"),
    (lambda: _token(ctx={"amurl": "https://boese.example/autodiscover/metadata/json/1"}), "amurl"),
    (lambda: _token(ctx={"amurl": "http://mail.firma.at/autodiscover/metadata/json/1"}), "amurl"),
    (lambda: _token(ctx={"amurl": "https://mail.firma.at:8443/autodiscover/metadata/json/1"}), "amurl"),
    (lambda: _token(ctx={"amurl": "https://mail.firma.at/owa/boese.json"}), "amurl"),
    (lambda: _token(ctx={"amurl": "https://mail.firma.at@boese.example/autodiscover/metadata/json/1"}), "amurl"),
    (lambda: _token(ctx={"amurl": "https://mail.firma.at.boese.example/autodiscover/metadata/json/1"}), "amurl"),
    (lambda: _token(ctx={"amurl": "https://wer@mail.firma.at/autodiscover/metadata/json/1"}), "amurl"),
    (lambda: _token(ctx={"amurl": None}), "amurl"),
    (lambda: _token(ctx={"msexchuid": f"{G_CLOUD}@ex01.firma.at"}), "Exchange-Online"),
    (lambda: _token(ctx={"msexchuid": f"{G_FREMD}@ex01.firma.at"}), "keinem Hybrid"),
    (lambda: _token(ctx={"msexchuid": HYBRID}), "msexchuid"),               # Adresse statt GUID
    (lambda: _token(ctx={"msexchuid": None}), "msexchuid"),
    (lambda: "a.b", "drei Teile"),
    (lambda: "", "fehlt"),
])
def test_jede_pruefung_greift(umgebung, token, grund):
    with pytest.raises(ei.Abgelehnt) as fehler:
        _pruefen(token())
    assert grund in str(fehler.value), str(fehler.value)


def test_ohne_ews_adresse_kein_aussteller(umgebung):
    daten, abrufe, _m = umgebung
    daten["EWS_ONPREM_URL"] = ""
    with pytest.raises(ei.Abgelehnt, match="keine EWS-Adresse"):
        _pruefen(_token())
    assert abrufe == []


@pytest.mark.parametrize("aenderung", [{"HYBRID_POSTFAECHER": False}, {"REINJECT_MODE": "graph"}])
def test_ohne_hybrid_betrieb_abgelehnt(umgebung, aenderung):
    umgebung[0].update(aenderung)
    with pytest.raises(ei.Abgelehnt, match="Hybrid"):
        _pruefen(_token())


def test_nach_neustart_cloud_eintrag_ohne_hybrid_markierung(umgebung, monkeypatch):
    """Leerer Speicher: ein MAILBOX_CONFIG-Eintrag OHNE hybrid=True (EXO-Postfach)
    taugt nicht als Zuordnung — schon die Suche findet ihn nicht."""
    monkeypatch.setattr(exo_mailboxes, "_cache", [])
    with pytest.raises(ei.Abgelehnt, match="keinem Hybrid-Nutzer des Gateways"):
        _pruefen(_token(ctx={"msexchuid": f"{G_CLOUD}@ex01.firma.at"}))


def test_nach_exo_umgezogen_abgelehnt(umgebung, monkeypatch):
    """Postfach inzwischen in Exchange Online: der Zwischenspeicher kennt die GUID
    als UserMailbox — auch wenn MAILBOX_CONFIG noch hybrid=True trägt."""
    monkeypatch.setattr(exo_mailboxes, "_cache", [
        {"guid": G_HYBRID, "primary": HYBRID, "addresses": [HYBRID], "type": "UserMailbox"}])
    with pytest.raises(ei.Abgelehnt, match="Exchange-Online"):
        _pruefen(_token())


def test_cloud_adresse_in_hybrid_eintrag_abgelehnt(umgebung, monkeypatch):
    """Leerer Speicher; der Konfigurationseintrag behauptet hybrid, die Adresse ist
    aber ein bekanntes EXO-Postfach (ist_hybrid prüft known_addresses)."""
    daten = umgebung[0]
    daten["MAILBOX_CONFIG"][G_FREMD] = {"sig": True, "hybrid": True, "primary": CLOUD}
    monkeypatch.setattr(exo_mailboxes, "_cache", [
        {"guid": G_CLOUD, "primary": CLOUD, "addresses": [CLOUD], "type": "UserMailbox"}])
    with pytest.raises(ei.Abgelehnt, match="kein Hybrid-Nutzer"):
        _pruefen(_token(ctx={"msexchuid": f"{G_FREMD}@ex01.firma.at"}))


def test_falsch_beschrifteter_schluessel_im_dokument(umgebung):
    """Das Dokument nennt das x5t des Tokens, enthält aber ein ANDERES Zertifikat —
    der Fingerabdruck des Zertifikats selbst muss passen."""
    umgebung[2]["doc"] = _metadaten(der=DER2, x5t=_x5t(DER))
    with pytest.raises(ei.Abgelehnt, match="Signaturschlüssel"):
        _pruefen(_token(key=KEY2))


def test_ungueltige_tokens_loesen_keinen_abruf_aus(umgebung):
    """Inhalt vor Netz: ein Token mit falschem aud oder amurl erreicht das
    Metadaten-Dokument nie."""
    _d, abrufe, _m = umgebung
    for t in (_token(aud="https://x.example/a"),
              _token(ctx={"amurl": "https://boese.example/autodiscover/metadata/json/1"}),
              _token(exp=str(int(time.time()) - 3600))):
        with pytest.raises(ei.Abgelehnt):
            _pruefen(t)
    assert abrufe == []


def test_zwischenspeicher_und_schluesselwechsel(umgebung):
    _d, abrufe, meta = umgebung
    jetzt = time.time()
    assert _pruefen(_token(), jetzt=jetzt) == HYBRID
    assert _pruefen(_token(), jetzt=jetzt + 10) == HYBRID
    assert len(abrufe) == 1                                     # aus dem Speicher
    # Neuer Schlüssel (Zertifikatswechsel): innerhalb der Pause kein Neuladen …
    meta["doc"] = _metadaten(der=DER2)
    with pytest.raises(ei.Abgelehnt):
        _pruefen(_token(key=KEY2, kopf={"x5t": _x5t(DER2)}), jetzt=jetzt + 20)
    assert len(abrufe) == 1
    # … danach einmal.
    assert _pruefen(_token(key=KEY2, kopf={"x5t": _x5t(DER2)}),
                    jetzt=jetzt + ei.NEU_LADEN_PAUSE_S + 30) == HYBRID
    assert len(abrufe) == 2


def test_amurl_mit_und_ohne_port_443(umgebung):
    basis = ("https", "mail.firma.at", None)
    for a in (AMURL, "https://MAIL.firma.at/Autodiscover/Metadata/Json/1"):
        assert ei.amurl_pruefen(a, basis) == "https://mail.firma.at/autodiscover/metadata/json/1"


# ── Endpunkt ─────────────────────────────────────────────────────────────────

class _Req:
    def __init__(self, kopf=None):
        self.headers = {k.lower(): v for k, v in (kopf or {}).items()}
        self.url = type("U", (), {"scheme": "https", "netloc": "sig.firma.at",
                                  "hostname": "sig.firma.at"})()
        self.query_params = {"client": "PC"}


@pytest.fixture
def rendern(umgebung, monkeypatch):
    async def _user(email):
        return graph_client.UserData(mail=email)
    monkeypatch.setattr(graph_client, "get_user", _user)
    monkeypatch.setattr(signature_engine, "render",
                        lambda u, t=None, extra=None, template_name=None: (f"<p>SIG {u.mail}</p>", ""))
    monkeypatch.setattr(policies, "aufloesen", lambda e, *a, **kw: {
        "sig": "Voll", "min": "Kurz", "oof": "", "banner": "", "disclaimer": "", "intern": ""})
    return umgebung


def _abruf(kopf):
    r = asyncio.run(av.verfassen_signatur(_Req(kopf)))
    return r.status_code, json.loads(r.body)


def test_endpunkt_liefert_signatur_des_hybrid_nutzers(rendern):
    code, body = _abruf({ei.KOPF: _token()})
    assert code == 200
    assert body["html"] == addin.markiert(f"<p>SIG {HYBRID}</p>")


def test_endpunkt_cloud_guid_401(rendern):
    code, body = _abruf({ei.KOPF: _token(ctx={"msexchuid": f"{G_CLOUD}@ex01.firma.at"})})
    assert code == 401 and "html" not in body


def test_endpunkt_kaputtes_bearer_faellt_nicht_auf_exchange_durch(rendern):
    """Ein Bearer-Kopf, der nicht trägt, bleibt 401 — auch mit gültigem
    Exchange-Token daneben."""
    code, _b = _abruf({"Authorization": "Bearer kaputt", ei.KOPF: _token()})
    assert code == 401


def test_endpunkt_aud_folgt_der_aussenadresse_nicht_der_anfrage(rendern):
    """Mit eingestellter Außenadresse (hier PUBLIC_HOSTNAME) zählt nur sie —
    ein weitergereichter Host der Anfrage macht ein Token mit fremdem aud
    nicht gültig."""
    rendern[0]["ADDIN_BASE_URL"] = ""
    rendern[0]["PUBLIC_HOSTNAME"] = "sig.firma.at"
    boese = "https://boese.example" + av.LAUFZEIT_HTML
    kopf = {"X-Forwarded-Host": "boese.example"}
    assert _abruf({**kopf, ei.KOPF: _token(aud=boese)})[0] == 401
    assert _abruf({**kopf, ei.KOPF: _token()})[0] == 200


def test_vorabanfrage_erlaubt_den_kopf():
    r = asyncio.run(av.verfassen_signatur_vorab())
    assert ei.KOPF in r.headers["access-control-allow-headers"]
    assert "Authorization" in r.headers["access-control-allow-headers"]


def test_laufzeit_kennt_den_kopf(umgebung):
    konf = av._konfiguration(BASE)
    assert konf["identkopf"] == ei.KOPF


def test_ablehnung_nennt_form_aber_nicht_den_wert():
    """Die Ablehnung zeigt die Form von msexchuid und die appctx-Schlüssel, nicht
    die Kennung selbst (Diagnose der ersten Live-Probe, 18.11.2026)."""
    from exchange_identitaet import Abgelehnt, postfach_zuordnen
    geheim = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
    with pytest.raises(Abgelehnt) as fehler:
        postfach_zuordnen(geheim, ["amurl", "msexchuid", "version"])
    text = str(fehler.value)
    assert "8-4-4-4-12" in text and "amurl, msexchuid, version" in text
    assert geheim not in text and "3f2504e0" not in text
