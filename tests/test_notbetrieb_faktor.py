"""Notbetrieb: zweiter Faktor (AD-Kennwort + TOTP).

Zusagen: TOTP nach RFC 6238; jeder Code nur einmal; Wiederherstellungscodes
je einmal; harte Sperre nach MAX_FEHLER; Einrichten nur nach frischer
Entra-Anmeldung (eigene Bescheinigung, keine Sitzung des Gateways); mit
Pflicht kein Webmail-Zugang ohne Faktor und kein reines Kennwort für IMAP/587.
"""
from __future__ import annotations

import base64
import os
import stat

import pytest

import notbetrieb
import notbetrieb_faktor as nf
from test_notbetrieb_webmail import client, schatten  # noqa: F401


@pytest.fixture
def faktor(schatten):  # noqa: F811
    schatten["NOTBETRIEB_ZWEITFAKTOR"] = True
    import login_drossel
    login_drossel._FEHLER.clear()
    return schatten


def _einrichten(postfach="a@firma.at"):
    g = nf.totp_beginnen(postfach)["geheimnis"]
    codes = nf.totp_bestaetigen(postfach, nf._code(g, nf._schritt()))
    return g, codes


def test_totp_rfc6238_testvektor():
    # RFC 6238 Anhang B: Schlüssel "12345678901234567890", T=59 → 94287082 (8 Stellen).
    g = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    assert nf._code(g, 59 // 30) == "287082"


def test_einrichten_aktiviert_erst_nach_gueltigem_code(faktor):
    g = nf.totp_beginnen("a@firma.at")["geheimnis"]
    assert not nf.stand("a@firma.at")["aktiv"]                       # begonnen ≠ aktiv
    with pytest.raises(nf.FaktorFehler):
        nf.totp_bestaetigen("a@firma.at", "000000" if nf._code(g, nf._schritt()) != "000000" else "111111")
    codes = nf.totp_bestaetigen("a@firma.at", nf._code(g, nf._schritt()))
    assert len(codes) == nf.ANZAHL_CODES and nf.stand("a@firma.at")["totp"]
    assert stat.S_IMODE(os.stat(nf._pfad()).st_mode) == 0o600
    roh = nf._pfad().read_text()
    assert codes[0] not in roh                                      # nur Hash gespeichert


def test_pruefen_einmal_je_code_und_wiederherstellung(faktor):
    g, codes = _einrichten()
    # Der Code beim Einrichten verbraucht seinen Zeitschritt — angemeldet wird
    # mit dem nächsten (im Fenster ±1).
    assert nf.pruefen("a@firma.at", nf._code(g, nf._schritt()))[0] is False
    jetzt = nf._code(g, nf._schritt() + 1)
    assert nf.pruefen("a@firma.at", jetzt)[0] is True
    assert nf.pruefen("a@firma.at", jetzt)[0] is False             # Wiederverwendung
    assert nf.pruefen("a@firma.at", codes[0].upper())[0] is True    # Wiederherstellungscode
    assert nf.pruefen("a@firma.at", codes[0])[0] is False           # nur einmal
    assert nf.stand("a@firma.at")["codes_uebrig"] == nf.ANZAHL_CODES - 1


def test_harte_sperre_nach_fehlversuchen(faktor, monkeypatch):
    import login_drossel
    monkeypatch.setattr(login_drossel, "gesperrt", lambda s: False)     # Backoff hier nicht abwarten
    g, _ = _einrichten()
    falsch = "999999" if nf._code(g, nf._schritt()) != "999999" else "888888"
    for _ in range(10):                                         # fest, nicht nf.MAX_FEHLER
        assert nf.pruefen("a@firma.at", falsch)[0] is False
    ok, meldung = nf.pruefen("a@firma.at", nf._code(g, nf._schritt() + 1))
    assert ok is False and "gesperrt" in meldung                    # auch der richtige Code nicht mehr
    assert nf.stand("a@firma.at")["gesperrt"]
    # Neu einrichten hebt die Sperre auf.
    _einrichten()
    assert not nf.stand("a@firma.at")["gesperrt"]


def test_zuruecksetzen(faktor):
    _einrichten()
    assert nf.zuruecksetzen("a@firma.at", "admin") is True
    assert not nf.stand("a@firma.at")["aktiv"] and nf.zuruecksetzen("a@firma.at", "admin") is False


# ── Webmail ─────────────────────────────────────────────────────────────────

def _kennwort(c):
    return c.post("/notfall/anmelden", json={"benutzer": "a.ad@firma.local", "passwort": "richtig"})


def test_webmail_ohne_faktor_kein_zugang_mit_faktor_zwei_schritte(client, faktor):  # noqa: F811
    r = _kennwort(client)
    assert r.status_code == 403 and r.json()["einrichten"] and "nb_sitzung" not in r.headers.get("set-cookie", "")
    assert client.post("/notfall/anmelden", json={"benutzer": "x", "passwort": "falsch"}).status_code == 401
    g, _ = _einrichten()
    r = _kennwort(client)
    assert r.status_code == 200 and r.json()["faktor"] and "nb_sitzung" not in r.headers.get("set-cookie", "")
    vorgang = r.json()["vorgang"]
    assert client.post("/notfall/anmelden/faktor", json={"vorgang": vorgang, "code": "12"}).status_code == 401
    assert client.get("/notfall/api/ich").status_code == 401
    assert client.post("/notfall/anmelden/faktor", json={"vorgang": vorgang + "x",
                                                         "code": nf._code(g, nf._schritt())}).status_code == 401
    r = client.post("/notfall/anmelden/faktor", json={"vorgang": vorgang, "code": nf._code(g, nf._schritt() + 1)})
    assert r.status_code == 200 and "nb_sitzung" in r.headers["set-cookie"]
    assert client.get("/notfall/api/ich").status_code == 200


def test_webmail_im_notfall_ohne_faktor_nur_wenn_erlaubt(client, faktor):  # noqa: F811
    faktor["NOTBETRIEB_AUSGERUFEN"] = True
    assert _kennwort(client).status_code == 403
    faktor["NOTBETRIEB_ZWEITFAKTOR_NOTFALL_OHNE"] = True
    assert _kennwort(client).status_code == 200
    faktor["NOTBETRIEB_AUSGERUFEN"] = False                         # Verzicht gilt nur im Notfall
    assert _kennwort(client).status_code == 403


def test_einrichten_nur_mit_frischer_entra_bescheinigung(client, faktor):  # noqa: F811
    import sso
    from webui.routen import notfall
    assert client.get("/notfall/api/faktor").status_code == 401
    # Eine gültige Sitzung des Gateways (Self-Service per Entra, auch Tage alt)
    # ersetzt die frische Bescheinigung nicht.
    wert = sso.create_session_cookie("a@firma.at", local=False, role=sso.ROLE_SELF)
    assert sso.verify_session_cookie(wert)                          # die Sitzung selbst IST gültig
    client.cookies.set(sso.SESSION_COOKIE, wert)
    assert client.get("/notfall/api/faktor").status_code == 401
    client.cookies.set(notfall.EINRICHTEN_COOKIE, "gefaelscht", path="/notfall")
    assert client.post("/notfall/api/faktor/beginnen", json={}).status_code == 401
    client.cookies.set(notfall.EINRICHTEN_COOKIE, notfall._einrichten_signierer().dumps({"u": "a@firma.at"}),
                       path="/notfall")
    r = client.post("/notfall/api/faktor/beginnen", json={})
    assert r.status_code == 200 and "<svg" in r.json()["qr_svg"] and r.json()["uri"].startswith("otpauth://totp/")
    code = nf._code(r.json()["geheimnis"], nf._schritt())
    r = client.post("/notfall/api/faktor/bestaetigen", json={"code": code})
    assert r.status_code == 200 and len(r.json()["codes"]) == nf.ANZAHL_CODES
    # Ohne Postfach im Notbetrieb kein Einrichten.
    client.cookies.set(notfall.EINRICHTEN_COOKIE, notfall._einrichten_signierer().dumps({"u": "fremd@x.de"}),
                       path="/notfall")
    assert client.get("/notfall/api/faktor").status_code == 403


def test_entra_rueckkehr_gibt_nur_die_bescheinigung(client, faktor, monkeypatch):  # noqa: F811
    """/auth/id-callback mit Ziel /notfall/einrichten: Bescheinigung, KEINE
    Sitzung des Gateways — auch ohne Rolle und ohne Self-Service."""
    import pkce as pkce_mod
    import sso
    monkeypatch.setattr(pkce_mod, "pop_session", lambda s: {"nonce": "n", "next_url": "/notfall/einrichten"})
    monkeypatch.setattr(sso, "verify_id_token", lambda t, n: {"preferred_username": "a@firma.at", "oid": "o1"})
    r = client.post("/auth/id-callback", content=b"id_token=x&state=s",
                    headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/notfall/einrichten"
    kekse = r.headers.get("set-cookie", "")
    assert "nb_einrichten=" in kekse and sso.SESSION_COOKIE + "=" not in kekse


def test_bereitschaft_zaehlt_mit_bezugsgroesse(faktor):
    import abnahme
    import notbetrieb_bereitschaft as nbb
    z = nbb._zweitfaktor()
    assert z["zustand"] == abnahme.OFFEN and "0 von" in z["befund"]
    _einrichten()
    z = nbb._zweitfaktor()
    assert z["zustand"] == abnahme.OK and z["befund"].startswith("1 von ")
    faktor["NOTBETRIEB_ZWEITFAKTOR"] = False
    assert nbb._zweitfaktor()["zustand"] == abnahme.ENTFAELLT


def test_abdeckung_zaehlt_nur_benutzerpostfaecher_ohne_gesperrte(faktor):
    """Bezugsgröße sind Benutzerpostfächer — ein geteiltes Postfach meldet sich
    nicht an. Ein gesperrter Faktor hilft im Notfall nicht."""
    with notbetrieb._lock, notbetrieb._conn() as conn:
        typen = [z["typ"] for z in conn.execute("SELECT typ FROM konten")]
    nutzer = typen.count("UserMailbox")
    assert nutzer >= 1 and len(typen) > nutzer                    # Netz scharf: es gibt auch andere
    geteilt = next(z["primary_adr"] for z in notbetrieb._conn().execute(
        "SELECT primary_adr FROM konten WHERE typ!='UserMailbox'"))
    assert nf.abdeckung() == {"eingerichtet": 0, "nutzer": nutzer}
    _einrichten("a@firma.at")
    _einrichten(geteilt)
    assert nf.abdeckung() == {"eingerichtet": 1, "nutzer": nutzer}
    daten = nf._laden()
    daten[nf._schluessel("a@firma.at")]["gesperrt"] = True
    nf._speichern(daten)
    assert nf.abdeckung()["eingerichtet"] == 0


def test_kopie_loeschen_laesst_faktoren_stehen(faktor):
    _einrichten()
    assert "schatten" not in str(nf._pfad()) and nf._pfad().parent != notbetrieb._db_pfad().parent
