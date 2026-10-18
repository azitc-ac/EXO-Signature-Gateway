"""Tab in der linken Leiste: Teams-App-Paket für den Self-Service (/self).

ANLASS (11.10.2026): Abwesenheit und Self-Service waren nur über einen Knopf
beim Lesen oder Verfassen einer Mail erreichbar. Ein Teams-App-Paket
(Manifest ≥ 1.13) mit persönlichem Tab legt /self links unter „Apps" ab — in
Outlook (Web, Windows, iOS/Android) und Teams. Dabei fiel auf: /self wurde mit
„X-Frame-Options: DENY" ausgeliefert und konnte weder als Taskpane in Outlook
im Web noch als Tab laden.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import struct
import zipfile
import zlib
from pathlib import Path

import pytest

import settings_store
from webui.routen import addin

VORLAGEN = Path(__file__).resolve().parent.parent / "app" / "webui" / "templates"


class _Req:
    headers: dict = {}
    url = type("U", (), {"scheme": "https", "netloc": "sig.example", "hostname": "sig.example"})()


def _paket(monkeypatch, data=None, abw=True, ver="1.9.148"):
    import config
    data = data if data is not None else {}
    data.update({"GATEWAY_NAME": "Mein GW", "ABWESENHEIT_AKTIV": abw, "ADDIN_BASE_URL": "https://sig.example"})
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: data.get(k, d))
    monkeypatch.setattr(settings_store, "force_update", lambda patch: data.update(patch))
    monkeypatch.setattr(config, "VERSION", ver)
    r = asyncio.run(addin.addin_teams_app(_Req()))
    return zipfile.ZipFile(io.BytesIO(r.body))


def _manifest(monkeypatch, **kw):
    return json.loads(_paket(monkeypatch, **kw).read("manifest.json"))


def _png(daten: bytes):
    """(breite, höhe, farbtyp, pixel als Liste von Tupeln) — nur für unsere unkomprimiert gefilterten PNGs."""
    assert daten[:8] == b"\x89PNG\r\n\x1a\n"
    breite, hoehe, _tiefe, farbtyp = struct.unpack(">IIBB", daten[16:26])
    pos, idat = 8, b""
    while pos < len(daten):
        laenge = struct.unpack(">I", daten[pos:pos + 4])[0]
        art = daten[pos + 4:pos + 8]
        if art == b"IDAT":
            idat += daten[pos + 8:pos + 8 + laenge]
        pos += 12 + laenge
    roh = zlib.decompress(idat)
    k = 4 if farbtyp == 6 else 3
    zeile = 1 + breite * k
    pixel = [tuple(roh[z * zeile + 1 + s * k: z * zeile + 1 + (s + 1) * k])
             for z in range(hoehe) for s in range(breite)]
    return breite, hoehe, farbtyp, pixel


def test_paket_enthaelt_genau_manifest_und_zwei_symbole(monkeypatch):
    assert sorted(_paket(monkeypatch).namelist()) == ["color.png", "manifest.json", "outline.png"]


def test_symbole_haben_die_verlangten_formate(monkeypatch):
    """Teams: color 192×192, outline 32×32 weiß auf transparent."""
    z = _paket(monkeypatch)
    b, h, typ, _ = _png(z.read("color.png"))
    assert (b, h) == (192, 192)
    b, h, typ, pixel = _png(z.read("outline.png"))
    assert (b, h, typ) == (32, 32, 6)
    assert any(p[3] == 0 for p in pixel), "outline braucht transparenten Hintergrund"
    assert all(p in ((0, 0, 0, 0), (255, 255, 255, 255)) for p in pixel), "outline: nur Weiß und Transparenz"


def test_manifest_beschreibt_einen_persoenlichen_tab_auf_self(monkeypatch):
    m = _manifest(monkeypatch)
    vers = tuple(int(x) for x in m["manifestVersion"].split("."))
    assert vers >= (1, 13), "erst ab 1.13 erscheint der Tab in Outlook"
    for feld in ("id", "version", "developer", "name", "description", "icons", "accentColor"):
        assert feld in m, feld
    tab, = m["staticTabs"]
    assert tab["contentUrl"] == "https://sig.example/self?host=teams"
    assert tab["scopes"] == ["personal"]
    assert "sig.example" in m["validDomains"]
    assert len(m["name"]["short"]) <= 30 and len(m["description"]["short"]) <= 80
    assert all(m["developer"][k].startswith("https://") for k in ("websiteUrl", "privacyUrl", "termsOfUseUrl"))


def test_tab_hat_eigene_id_neben_dem_addin(monkeypatch):
    """Zwei Pakete, zwei Kennungen — sonst überschreibt das eine das andere."""
    import xml.etree.ElementTree as ET
    m = _manifest(monkeypatch)
    xml = ET.fromstring(asyncio.run(addin.addin_manifest(_Req())).body)
    addin_id = xml.find("{http://schemas.microsoft.com/office/appforoffice/1.1}Id").text
    assert m["id"] != addin_id


def test_version_dreistellig_und_stabil_ohne_aenderung(monkeypatch):
    data = {}
    assert _manifest(monkeypatch, data=data, ver="1.9.148")["version"] == "1.9.148"
    assert _manifest(monkeypatch, data=data, ver="1.9.160")["version"] == "1.9.148"


def test_aenderung_ohne_gateway_update_zaehlt_die_dritte_stelle(monkeypatch):
    """Das Teams-Schema erlaubt nur n.n.n — keine vierte Stelle wie beim Add-in."""
    data = {}
    _manifest(monkeypatch, data=data, ver="1.9.148")
    v = _manifest(monkeypatch, data=data, abw=False, ver="1.9.148")["version"]
    assert v == "1.9.149" and re.fullmatch(r"\d{1,5}\.\d{1,5}\.\d{1,5}", v)


def test_texte_ohne_xml_maskierung(monkeypatch):
    """Die Texte kommen aus derselben Quelle wie im XML-Manifest — dort maskiert,
    im JSON darf „&amp;" nicht stehen bleiben."""
    m = _manifest(monkeypatch)
    assert m["name"]["short"] == "Signatur & Abwesenheit"
    assert "&amp;" not in json.dumps(m)


# ── Rahmen und Seite ──────────────────────────────────────────────────────────

@pytest.fixture
def client():
    from starlette.testclient import TestClient
    from webui import app as wa
    with TestClient(wa.app) as c:
        yield c


def test_self_darf_gerahmt_werden(client):
    """Taskpane (Lesen-Knopf) und Tab rahmen /self — DENY ließ beides nicht laden."""
    assert client.get("/self").headers.get("X-Frame-Options") != "DENY"
    assert client.get("/self?host=teams").headers.get("X-Frame-Options") != "DENY"


def test_nachbarpfade_bleiben_gesperrt(client):
    """Die Freigabe gilt genau /self, nicht allem, was so anfängt."""
    assert client.get("/selfx").headers.get("X-Frame-Options") == "DENY"
    assert client.get("/auth/login").headers.get("X-Frame-Options") == "DENY"


def test_rahmen_eltern_nennen_die_microsoft365_hosts(client):
    csp = client.get("/self").headers.get("Content-Security-Policy-Report-Only", "")
    for host in ("https://*.cloud.microsoft", "https://teams.microsoft.com", "https://outlook.office.com"):
        assert host in csp, host


def test_teamsjs_nur_im_tab_und_mit_integritaetspruefung(client):
    mit = client.get("/self?host=teams").text
    ohne = client.get("/self").text
    assert f'src="{addin.TEAMSJS_URL}"' in mit and f'integrity="{addin.TEAMSJS_SRI}"' in mit
    assert "var IM_TEAMS = true;" in mit
    assert addin.TEAMSJS_URL not in ohne and "var IM_TEAMS = false;" in ohne


def test_tab_navigiert_nie_zur_anmeldeseite():
    """Microsofts Anmeldung lässt sich nicht einbetten: Im Tab muss anmelden()
    VOR jeder Weiterleitung abbiegen, und Abmelden darf nicht nach /auth/logout
    navigieren."""
    from pathlib import Path
    t = (Path(addin.__file__).resolve().parents[1] / "templates" / "self.html").read_text("utf-8")
    anm = t[t.index("function anmelden()"):]
    assert anm.index("if (IM_TEAMS)") < anm.index("window.location = '/auth/login")
    assert "if (!imOffice() && !IM_TEAMS && !IM_RAHMEN) { window.location = '/auth/logout'" in t


def test_aufgabenbereich_navigiert_nie_zur_anmeldeseite():
    """Im Rahmen (Lese-Knopf des Add-ins) endet /auth/login an X-Frame-Options:
    DENY. anmelden() muss vor der Weiterleitung abbiegen, und der Start im
    Rahmen darf keinen Zeitgeber vor Office.onReady haben (der war die Ursache:
    imOffice() noch falsch beim ersten 401). Im Browser prüft das
    test_ui_browser.py ausführlich; dies ist die Fassung für die CI."""
    t = (VORLAGEN / "self.html").read_text("utf-8")
    anm = t[t.index("function anmelden()"):]
    assert anm.index("if (IM_RAHMEN)") < anm.index("window.location = '/auth/login")
    rahmen = t[t.index("} else if (IM_RAHMEN) {"):]
    rahmen = rahmen[:rahmen.index("} else {")]
    assert "setTimeout(start" not in rahmen
    assert "Office.onReady(function () { clearTimeout(addinWache); officeDesign(); start(); })" in rahmen


def test_anmeldeabschluss_fuer_teams(client):
    r = client.get("/addin/auth-complete-teams")
    assert "authentication.notifySuccess" in r.text
    assert addin.TEAMSJS_SRI in r.text
    assert "no-store" in r.headers.get("Cache-Control", "")   # trägt das Sitzungsmerkmal


def test_fremde_adresse_zaehlt_die_paketversion_nicht_hoch(monkeypatch):
    import config
    data = {"GATEWAY_NAME": "Mein GW", "ABWESENHEIT_AKTIV": True}
    schreib = []
    monkeypatch.setattr(config, "VERSION", "1.9.148")
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: data.get(k, d))
    monkeypatch.setattr(settings_store, "force_update", lambda p: (schreib.append(p), data.update(p)))

    def version(host):
        req = type("R", (), {"headers": {"x-forwarded-host": host, "x-forwarded-proto": "https"},
                             "url": _Req.url})()
        z = zipfile.ZipFile(io.BytesIO(asyncio.run(addin.addin_teams_app(req)).body))
        return json.loads(z.read("manifest.json"))["version"]

    assert {version(h) for h in ("a.example", "b.example", "c.example")} == {"1.9.148"}
    assert len(schreib) == 1


def test_tab_zeichnet_vor_den_fremdskripten(client):
    """Im Tab blieb die Seite in Outlook leer: Skripte im <head> halten das
    Zeichnen an, solange ihr CDN nicht antwortet. Im Tab daher kein office.js
    (dort gibt es keinen Office-Host) und TeamsJS erst hinter dem Inhalt."""
    mit = client.get("/self?host=teams").text
    assert "appsforoffice.microsoft.com" not in mit
    assert mit.index('id="lade"') < mit.index(f'src="{addin.TEAMSJS_URL}"')
    ohne = client.get("/self").text
    assert "appsforoffice.microsoft.com/lib/1.1/hosted/office.js" in ohne


def test_tab_meldet_sich_wenn_der_host_nie_antwortet():
    """Bleibt app.initialize() ohne Antwort, darf „Lädt…" nicht ewig stehen."""
    text = (VORLAGEN / "self.html").read_text(encoding="utf-8")
    assert "clearTimeout(tabWache)" in text
    assert "zeigeFehler(TAB_KEINE_ANTWORT)" in text
    assert 'id="im-browser"' in text


# ── Rechtstexte im Paket ──────────────────────────────────────────────────────

def test_rechtstexte_ohne_eintrag_zeigen_auf_die_oeffentliche_seite(monkeypatch):
    """Bis v1.9.155 zeigten sie auf die Startseite — hinter der Anmeldung."""
    m = json.loads(_paket(monkeypatch).read("manifest.json"))
    assert m["developer"]["privacyUrl"] == "https://sig.example/self/rechtliches#datenschutz"
    assert m["developer"]["termsOfUseUrl"] == "https://sig.example/self/rechtliches#nutzung"


def test_rechtstexte_des_betreibers_gehen_vor(monkeypatch):
    m = json.loads(_paket(monkeypatch, {
        "SELF_DATENSCHUTZ_URL": "https://firma.example/datenschutz",
        "SELF_NUTZUNGSBEDINGUNGEN_URL": "https://firma.example/agb"}).read("manifest.json"))
    assert m["developer"]["privacyUrl"] == "https://firma.example/datenschutz"
    assert m["developer"]["termsOfUseUrl"] == "https://firma.example/agb"


@pytest.mark.parametrize("wert", ["javascript:alert(1)", "http://firma.example/ds", "https://", " "])
def test_nur_https_kommt_ins_paket(monkeypatch, wert):
    m = json.loads(_paket(monkeypatch, {"SELF_DATENSCHUTZ_URL": wert}).read("manifest.json"))
    assert m["developer"]["privacyUrl"].endswith("/self/rechtliches#datenschutz")


def test_eigene_rechtstexte_zaehlen_die_paketversion_hoch(monkeypatch):
    daten = {}
    v1 = json.loads(_paket(monkeypatch, daten).read("manifest.json"))["version"]
    daten["SELF_DATENSCHUTZ_URL"] = "https://firma.example/datenschutz"
    v2 = json.loads(_paket(monkeypatch, daten).read("manifest.json"))["version"]
    assert v1 != v2


def test_rechtsseite_ist_ohne_anmeldung_lesbar(client):
    r = client.get("/self/rechtliches")
    assert r.status_code == 200
    assert 'id="datenschutz"' in r.text and 'id="nutzung"' in r.text
