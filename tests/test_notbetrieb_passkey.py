"""Notbetrieb: Passkeys (WebAuthn) als zweiter Faktor.

Zusagen: Ein Passkey gilt nur für den Hostnamen, unter dem er eingerichtet
wurde, und nur mit einer Herkunft (origin) genau dieses Namens; jede
Herausforderung gilt einmal; ein zurückgedrehter Signaturzähler wird
abgewiesen; Passkeys laufen durch DENSELBEN Prüfrahmen wie der Code (Sperre);
wer nur Passkeys hat, zählt als eingerichtet (Übersicht, Bereitschaft).

Geprüft wird mit einem Software-Authenticator (echte P-256-Signaturen), damit
die Prüfung der Bibliothek tatsächlich durchläuft — nicht gegen Attrappen.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import struct

import pytest

import notbetrieb_faktor as nf
from test_notbetrieb_faktor import faktor  # noqa: F401
from test_notbetrieb_webmail import client, schatten  # noqa: F401

cbor2 = pytest.importorskip("cbor2")
pytest.importorskip("webauthn")
from cryptography.hazmat.primitives import hashes                   # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec            # noqa: E402

PF = "a@firma.at"
HOST = "gw.firma.at"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


class Passkey:
    """Ein Authenticator im Test: Schlüsselpaar, Kennung, Zähler."""

    def __init__(self, rp: str = HOST):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.id = secrets.token_bytes(16)
        self.rp = rp
        self.zaehler = 0

    def _cd(self, typ: str, optionen: dict, herkunft: str | None) -> bytes:
        return json.dumps({"type": typ, "challenge": optionen["challenge"],
                           "origin": herkunft or f"https://{self.rp}", "crossOrigin": False}).encode()

    def erstellen(self, optionen: dict, herkunft: str | None = None) -> dict:
        n = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})
        auth = (hashlib.sha256(optionen["rp"]["id"].encode()).digest() + bytes([0x45]) + struct.pack(">I", 0)
                + bytes(16) + struct.pack(">H", len(self.id)) + self.id + cose)
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth})
        cd = self._cd("webauthn.create", optionen, herkunft)
        return {"id": _b64(self.id), "rawId": _b64(self.id), "type": "public-key",
                "response": {"clientDataJSON": _b64(cd), "attestationObject": _b64(att)}}

    def signieren(self, optionen: dict, herkunft: str | None = None, rp: str | None = None,
                  zaehler: int | None = None) -> dict:
        self.zaehler = self.zaehler + 1 if zaehler is None else zaehler
        auth = hashlib.sha256((rp or optionen["rpId"]).encode()).digest() + bytes([0x05]) \
            + struct.pack(">I", self.zaehler)
        cd = self._cd("webauthn.get", optionen, herkunft)
        sig = self.key.sign(auth + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": _b64(self.id), "rawId": _b64(self.id), "type": "public-key",
                "response": {"clientDataJSON": _b64(cd), "authenticatorData": _b64(auth),
                             "signature": _b64(sig), "userHandle": None}}


def _einrichten(pk: Passkey | None = None, host: str = HOST) -> tuple[Passkey, list[str]]:
    pk = pk or Passkey(host)
    codes = nf.passkey_bestaetigen(PF, pk.erstellen(nf.passkey_beginnen(PF, host)), "Testgerät")
    return pk, codes


def test_einrichten_und_anmelden(faktor):  # noqa: F811
    assert not nf.stand(PF)["aktiv"]
    pk, codes = _einrichten()
    assert len(codes) == nf.ANZAHL_CODES                            # erster Faktor → Wiederherstellungscodes
    st = nf.stand(PF)
    assert st["aktiv"] and not st["totp"] and [p["rp"] for p in st["passkeys"]] == [HOST]
    assert "pk" not in st["passkeys"][0]                            # kein Schlüsselmaterial nach außen
    ok, _ = nf.passkey_pruefen(PF, pk.signieren(nf.passkey_anmelden_optionen(PF, HOST)))
    assert ok is True and nf.stand(PF)["passkeys"][0]["zuletzt"]
    # Wiederherstellungscode trägt auch ohne TOTP.
    assert nf.pruefen(PF, codes[0])[0] is True


def test_anmelden_wartet_hoechstens_eine_minute(faktor):  # noqa: F811
    """Ein übersehener Passkey-Dialog sperrte den Knopf knapp 3 Minuten
    (Klicktest) — beim Anmelden wartet der Browser höchstens 60 s."""
    _einrichten()
    assert nf.passkey_anmelden_optionen(PF, HOST)["timeout"] <= 60_000
    assert nf.passkey_beginnen(PF, HOST)["timeout"] > 60_000       # Einrichten darf länger


def test_herausforderung_gilt_einmal(faktor):  # noqa: F811
    pk, _ = _einrichten()
    # Zähler 0: synchronisierte Passkeys (Apple, Google) zählen nicht — dann
    # schützt allein die Einmaligkeit der Herausforderung vor Wiederholung.
    antwort = pk.signieren(nf.passkey_anmelden_optionen(PF, HOST), zaehler=0)
    assert nf.passkey_pruefen(PF, antwort)[0] is True
    assert nf.passkey_pruefen(PF, antwort)[0] is False              # dieselbe Antwort noch einmal


def test_zaehler_zurueck_abgewiesen(faktor):  # noqa: F811
    pk, _ = _einrichten()
    assert nf.passkey_pruefen(PF, pk.signieren(nf.passkey_anmelden_optionen(PF, HOST), zaehler=5))[0] is True
    assert nf.passkey_pruefen(PF, pk.signieren(nf.passkey_anmelden_optionen(PF, HOST), zaehler=5))[0] is False


def test_fremde_herkunft_und_fremder_name(faktor):  # noqa: F811
    pk, _ = _einrichten()
    # Herkunft einer anderen Seite (Phishing-Seite, die die Antwort weiterreicht).
    assert nf.passkey_pruefen(PF, pk.signieren(nf.passkey_anmelden_optionen(PF, HOST),
                                               herkunft="https://gw.firma.at.evil.example"))[0] is False
    assert nf.passkey_pruefen(PF, pk.signieren(nf.passkey_anmelden_optionen(PF, HOST),
                                               herkunft="http://gw.firma.at"))[0] is False
    # Signatur für einen anderen rp.
    assert nf.passkey_pruefen(PF, pk.signieren(nf.passkey_anmelden_optionen(PF, HOST), rp="evil.example"))[0] is False
    # Port spielt keine Rolle (Gateway läuft oft auf :9443).
    assert nf.passkey_pruefen(PF, pk.signieren(nf.passkey_anmelden_optionen(PF, HOST),
                                               herkunft="https://gw.firma.at:9443"))[0] is True
    # Unter einem anderen Namen oder per IP: kein Passkey angeboten.
    assert nf.passkey_anmelden_optionen(PF, "anders.firma.at") is None
    assert nf.passkey_anmelden_optionen(PF, "10.99.0.5") is None
    with pytest.raises(nf.FaktorFehler):
        nf.passkey_beginnen(PF, "10.99.0.5")


def test_registrierung_mit_fremder_herkunft_abgewiesen(faktor):  # noqa: F811
    pk = Passkey()
    optionen = nf.passkey_beginnen(PF, HOST)
    with pytest.raises(nf.FaktorFehler):
        nf.passkey_bestaetigen(PF, pk.erstellen(optionen, herkunft="https://evil.example"))
    with pytest.raises(nf.FaktorFehler):                            # Herausforderung verbraucht
        nf.passkey_bestaetigen(PF, pk.erstellen(optionen))
    assert not nf.stand(PF)["aktiv"]


def test_zweiter_passkey_behaelt_die_codes(faktor):  # noqa: F811
    _, codes = _einrichten()
    pk2, codes2 = _einrichten()
    assert codes2 == [] and nf.stand(PF)["codes_uebrig"] == nf.ANZAHL_CODES
    assert nf.pruefen(PF, codes[1])[0] is True
    assert nf.passkey_pruefen(PF, pk2.signieren(nf.passkey_anmelden_optionen(PF, HOST)))[0] is True
    # Entfernen: der andere bleibt.
    assert nf.passkey_entfernen(PF, _b64(pk2.id)) is True
    assert nf.passkey_entfernen(PF, _b64(pk2.id)) is False
    assert len(nf.stand(PF)["passkeys"]) == 1 and nf.stand(PF)["aktiv"]


def test_sperre_gilt_auch_fuer_passkeys(faktor, monkeypatch):  # noqa: F811
    import login_drossel
    monkeypatch.setattr(login_drossel, "gesperrt", lambda s: False)
    pk, _ = _einrichten()
    for _ in range(10):
        assert nf.pruefen(PF, "kein-code")[0] is False
    assert nf.passkey_anmelden_optionen(PF, HOST) is None
    # Auch eine vorher geholte Herausforderung hilft nicht mehr.
    nf.zuruecksetzen(PF, "test")
    pk, _ = _einrichten()
    optionen = nf.passkey_anmelden_optionen(PF, HOST)
    for _ in range(10):
        nf.pruefen(PF, "kein-code")
    ok, meldung = nf.passkey_pruefen(PF, pk.signieren(optionen))
    assert ok is False and "gesperrt" in meldung


def test_nur_passkey_zaehlt_als_eingerichtet(faktor):  # noqa: F811
    import abnahme
    import notbetrieb_bereitschaft as nbb
    _einrichten()
    assert [f["postfach"] for f in nf.uebersicht()] == [PF] and nf.uebersicht()[0]["passkeys"] == 1
    z = nbb._zweitfaktor()
    assert z["zustand"] == abnahme.OK and z["befund"].startswith("1 von ")


# ── Über die Routen ─────────────────────────────────────────────────────────

def test_anmeldung_in_zwei_schritten_mit_passkey(client, faktor):  # noqa: F811
    faktor["PUBLIC_HOSTNAME"] = HOST                                # sonst weist die Herkunftsprüfung ab
    pk, _ = _einrichten()
    kopf = {"Origin": f"https://{HOST}"}
    r = client.post("/notfall/anmelden", json={"benutzer": "a.ad@firma.local", "passwort": "richtig"}, headers=kopf)
    assert r.status_code == 200 and r.json()["passkey"] is True and r.json()["totp"] is False
    vorgang = r.json()["vorgang"]
    # Unter einem anderen Namen (hier: testserver, ohne Origin) kein Passkey-Angebot.
    r2 = client.post("/notfall/anmelden", json={"benutzer": "a.ad@firma.local", "passwort": "richtig"})
    assert r2.json()["passkey"] is False
    assert client.post("/notfall/anmelden/passkey/optionen", json={"vorgang": vorgang}).status_code == 404
    assert client.post("/notfall/anmelden/passkey/optionen", json={"vorgang": vorgang + "x"},
                       headers=kopf).status_code == 401
    o = client.post("/notfall/anmelden/passkey/optionen", json={"vorgang": vorgang}, headers=kopf).json()["optionen"]
    assert client.get("/notfall/api/ich").status_code == 401
    r = client.post("/notfall/anmelden/passkey", json={"vorgang": vorgang, "antwort": pk.signieren(o)}, headers=kopf)
    assert r.status_code == 200 and "nb_sitzung" in r.headers["set-cookie"]
    assert client.get("/notfall/api/ich").status_code == 200


def test_passkey_einrichten_nur_mit_bescheinigung(client, faktor):  # noqa: F811
    from webui.routen import notfall
    faktor["PUBLIC_HOSTNAME"] = HOST
    kopf = {"Origin": f"https://{HOST}"}
    assert client.post("/notfall/api/faktor/passkey/beginnen", json={}, headers=kopf).status_code == 401
    client.cookies.set(notfall.EINRICHTEN_COOKIE, notfall._einrichten_signierer().dumps({"u": PF}), path="/notfall")
    o = client.post("/notfall/api/faktor/passkey/beginnen", json={}, headers=kopf).json()["optionen"]
    assert o["rp"]["id"] == HOST
    pk = Passkey()
    r = client.post("/notfall/api/faktor/passkey/bestaetigen", json={"antwort": pk.erstellen(o), "name": "iPhone"},
                    headers=kopf)
    assert r.status_code == 200 and len(r.json()["codes"]) == nf.ANZAHL_CODES
    st = client.get("/notfall/api/faktor").json()
    assert st["aktiv"] and st["passkeys"][0]["name"] == "iPhone"
    pid = st["passkeys"][0]["id"]
    assert client.post("/notfall/api/faktor/passkey/entfernen", json={"id": pid}).status_code == 200
    assert client.post("/notfall/api/faktor/passkey/entfernen", json={"id": pid}).status_code == 404
