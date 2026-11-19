"""Stille Anmeldung im Aufgabenbereich des Knopf-Add-ins (/self, seit v1.12.95).

Der Aufgabenbereich meldete sich nur über das Office-Anmeldefenster an; still
ging es allein im Teams-/Outlook-Tab (NAA). Jetzt gilt in beiden derselbe
Baustein (static/addin_anmeldung.js, auch im Verfassen-Add-in):

  NAA → Exchange-Identitätstoken (nur Konten auf Exchange lokal) → Fenster.

Geprüft:
* /self liefert die NAA-Angaben auch OHNE ?host=teams (der Aufgabenbereich hat
  kein Kennzeichen — das Manifest bleibt unverändert).
* Der Tausch nimmt ein Exchange-Identitätstoken nur mit `aud` = erste
  FormSettings-Adresse des KNOPF-Manifests, nur für Hybrid-Nutzer des Gateways,
  und gibt nie mehr als ROLE_SELF; das Token steht nie im Protokoll.
* Die echte Seite (self.html) und der echte Baustein laufen in Node gegen
  nachgebaute Office-, MSAL- und fetch-Schnittstellen: Reihenfolge, nur EIN
  stiller Versuch je Seitenaufruf, Tab unverändert.

Testdaten erfunden (firma.at, Max Mustermann).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest

import exchange_identitaet as ei
import settings_store
import sso
from test_exchange_identitaet import (  # noqa: F401 — Fixture und Helfer
    BASE, G_CLOUD, G_FREMD, HYBRID, _Req, _token, umgebung,
)
from webui.routen import addin, addin_verfassen as av

AUD_KNOPF = BASE + addin.KNOPF_FORM_PFAD


# ── Server: NAA-Angaben auf /self ─────────────────────────────────────────────

@pytest.fixture
def seite(monkeypatch):
    """/self rendern (mit NAA-Angaben einer erfundenen Bootstrap-App)."""
    from starlette.testclient import TestClient
    from webui import app as wa
    echt = settings_store.get
    fest = {"BOOTSTRAP_CLIENT_ID": "CID", "TENANT_ID": "TID"}
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a, **kw: fest[k] if k in fest else echt(k, *a, **kw))
    with TestClient(wa.app) as c:
        yield lambda pfad="/self": c.get(pfad).text


def _naa(html: str) -> dict:
    return json.loads(re.search(r"var NAA = (\{.*?\});\n", html).group(1))


@pytest.mark.parametrize("pfad", ["/self", "/self?host=teams"])
def test_self_liefert_naa_auch_ohne_tab_kennzeichen(seite, pfad):
    """Bis v1.12.94 bekam nur der Tab die Angaben — der Aufgabenbereich (dieselbe
    Adresse ohne ?host=teams) konnte gar nicht still anmelden."""
    naa = _naa(seite(pfad))
    assert naa["clientId"] == "CID" and naa["scope"] == "api://CID/access_as_user"
    assert naa["sitzung"] == av.TAB_SITZUNG_API
    assert naa["identkopf"] == ei.KOPF


@pytest.mark.parametrize("pfad", ["/self", "/self?host=teams"])
def test_self_bindet_msal_und_den_baustein_ein(seite, pfad):
    html = seite(pfad)
    assert f'src="/static/vendor/{av.MSAL_DATEI.name}"' in html
    assert f'src="/static/{av.ANMELDUNG_DATEI.name}?v=' in html
    assert av.ANMELDUNG_DATEI.is_file()


def test_verfassen_laufzeit_liefert_den_baustein_vor_dem_handler(monkeypatch):
    """Classic Outlook lädt nur EINE Datei: Baustein und Handler müssen darin
    stehen, der Baustein zuerst (der Handler nutzt ExoAnmeldung beim Laden)."""
    inhalt = av.handler_quelltext()
    assert inhalt.index("var ExoAnmeldung =") < inhalt.index("ExoAnmeldung.erstelle(EXO_VERFASSEN)")
    assert "createNestablePublicClientApplication" not in av.HANDLER_DATEI.read_text("utf-8")


# ── Server: Tausch des Exchange-Identitätstokens ──────────────────────────────

def _knopf_aud_aus_manifest(lokal: bool) -> str:
    """aud laut MS Learn: erste SourceLocation unter FormSettings."""
    root = ET.fromstring(addin.knopf_manifest_xml(BASE, lokal=lokal))
    ns = {"o": "http://schemas.microsoft.com/office/appforoffice/1.1"}
    return root.find("o:FormSettings/o:Form/o:DesktopSettings/o:SourceLocation", ns).get("DefaultValue")


@pytest.mark.parametrize("lokal", [False, True])
def test_erwartetes_aud_ist_die_formsettings_adresse_des_knopf_manifests(umgebung, lokal):
    assert av._aud_erwartet(_Req(), addin.KNOPF_FORM_PFAD) == _knopf_aud_aus_manifest(lokal) == AUD_KNOPF


@pytest.fixture
def tausch(umgebung, monkeypatch):
    daten = umgebung[0]
    daten["SELF_SERVICE_ENABLED"] = True
    monkeypatch.setattr(sso, "_get_secret", lambda: "testgeheimnis")

    def _lauf(kopf):
        r = asyncio.run(av.tab_sitzung(_Req(kopf)))
        return r.status_code, json.loads(r.body)
    return daten, _lauf


def test_identitaetstoken_ergibt_self_sitzung_fuer_genau_dieses_postfach(tausch):
    _d, lauf = tausch
    code, body = lauf({ei.KOPF: _token(aud=AUD_KNOPF)})
    assert code == 200
    s = sso.verify_session_cookie(body["token"])
    assert s["u"] == HYBRID and s["r"] == sso.ROLE_SELF


def test_identitaetstoken_des_verfassen_add_ins_gilt_hier_nicht(tausch):
    """Getrennte Zielgruppen: ein Token mit aud des Verfassen-Add-ins (oder
    einer fremden Adresse) eröffnet keine Self-Service-Sitzung."""
    _d, lauf = tausch
    for aud in (BASE + av.LAUFZEIT_HTML, "https://boese.example" + addin.KNOPF_FORM_PFAD):
        code, body = lauf({ei.KOPF: _token(aud=aud)})
        assert code == 401 and "token" not in body, aud


@pytest.mark.parametrize("guid", [G_CLOUD, G_FREMD])
def test_identitaetstoken_fuer_cloud_oder_unbekanntes_postfach_401(tausch, guid):
    _d, lauf = tausch
    code, body = lauf({ei.KOPF: _token(aud=AUD_KNOPF, ctx={"msexchuid": guid})})
    assert code == 401 and "token" not in body


@pytest.mark.parametrize("wert", ["kaputt", "a.b.c", ""])
def test_ungueltiges_identitaetstoken_401(tausch, wert):
    _d, lauf = tausch
    code, body = lauf({ei.KOPF: wert})
    assert code == 401 and "token" not in body


def test_falsch_signiertes_identitaetstoken_401(tausch):
    from test_exchange_identitaet import KEY2
    _d, lauf = tausch
    code, body = lauf({ei.KOPF: _token(key=KEY2, aud=AUD_KNOPF)})
    assert code == 401 and "token" not in body


def test_kaputtes_bearer_faellt_nicht_auf_das_identitaetstoken_durch(tausch):
    _d, lauf = tausch
    code, _b = lauf({"Authorization": "Bearer kaputt", ei.KOPF: _token(aud=AUD_KNOPF)})
    assert code == 401


def test_ohne_self_service_403(tausch):
    daten, lauf = tausch
    daten["SELF_SERVICE_ENABLED"] = False
    code, body = lauf({ei.KOPF: _token(aud=AUD_KNOPF)})
    assert code == 403 and "token" not in body


def test_kein_token_im_protokoll(tausch, caplog):
    _d, lauf = tausch
    gut, schlecht = _token(aud=AUD_KNOPF), _token(aud=BASE + av.LAUFZEIT_HTML)
    with caplog.at_level(logging.DEBUG):
        assert lauf({ei.KOPF: gut})[0] == 200
        assert lauf({ei.KOPF: schlecht})[0] == 401
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "Exchange-Identitätstoken" in text                       # Protokoll ist da …
    for t in (gut, schlecht):
        for teil in t.split("."):
            assert teil not in text                                 # … das Token nicht


# ── Client: echte Seite und echter Baustein in Node ───────────────────────────

SEITEN_HARNESS = r"""
var fall = JSON.parse(require('fs').readFileSync(0, 'utf8'));
var log = { abrufe: [], dialoge: [], identitaet: 0, naa: 0, fenster: [], konsole: [] };
var elemente = {};
function element(id) {
  if (!elemente[id]) elemente[id] = { id: id, textContent: '', value: '', checked: false, style: {},
    hoerer: {}, addEventListener: function (t, f) { this.hoerer[t] = f; },
    setAttribute: function () {}, getAttribute: function () { return null; }, appendChild: function () {} };
  return elemente[id];
}
globalThis.window = globalThis;
globalThis.self = globalThis;
globalThis.top = fall.rahmen ? {} : globalThis;
globalThis.location = { origin: 'https://sig.firma.at' };
globalThis.document = { getElementById: element, querySelector: function () { return null; },
  querySelectorAll: function () { return []; }, createElement: function () { return element('_neu'); },
  documentElement: { setAttribute: function () {}, getAttribute: function () { return 'light'; } } };
var speicher = {};
globalThis.localStorage = { getItem: function (k) { return k in speicher ? speicher[k] : null; },
  setItem: function (k, v) { speicher[k] = String(v); }, removeItem: function (k) { delete speicher[k]; } };
globalThis.matchMedia = function () { return { matches: false, addEventListener: function () {} }; };
console.info = function () { log.konsole.push([].slice.call(arguments).join(' ')); };
function antwort(s, body) {
  return Promise.resolve({ ok: s >= 200 && s < 300, status: s, json: function () { return Promise.resolve(body); } });
}
globalThis.fetch = function (url, opt) {
  opt = opt || {};
  var kopf = opt.headers || {};
  var pfad = String(url).split('?')[0];
  log.abrufe.push({ url: pfad, voll: String(url), methode: opt.method || 'GET', kopf: kopf });
  if (pfad === '/api/self/context') return antwort(kopf['X-Addin-Session'] === 'SITZUNG' ? 403 : 401, {});
  if (pfad === fall.sitzungApi) return antwort(fall.sitzung, fall.sitzung === 200 ? { token: 'SITZUNG' } : {});
  return antwort(404, {});
};
globalThis.msal = { createNestablePublicClientApplication: function () {
  log.naa++;
  return Promise.resolve({ acquireTokenSilent: function () {
    if (fall.naaFehler) { var e = new Error('MSAL'); e.errorCode = fall.naaFehler; return Promise.reject(e); }
    return Promise.resolve({ accessToken: 'NAA.TOKEN' });
  } });
} };
if (fall.office) {
  globalThis.Office = {
    AsyncResultStatus: { Succeeded: 'succeeded', Failed: 'failed' },
    EventType: { DialogMessageReceived: 'm', DialogEventReceived: 'e' },
    onReady: function (cb) { setTimeout(function () { cb({ host: 'Outlook' }); }, 0); },
    context: {
      officeTheme: {},
      diagnostics: { platform: 'PC' },
      requirements: { isSetSupported: function (n) { return n === 'NestedAppAuth' ? fall.naaSet : true; } },
      // 12009 = „Ignorieren“: die Seite zeigt dann den Anmelden-Knopf.
      ui: { displayDialogAsync: function (url, o, cb) { log.dialoge.push(url);
        cb({ status: 'failed', error: { code: 12009, message: 'ignoriert' } }); } },
      mailbox: {
        userProfile: fall.accountType === null ? {} : { accountType: fall.accountType },
        getUserIdentityTokenAsync: function (cb) { log.identitaet++;
          if (fall.identFehler) cb({ status: 'failed', error: { message: 'GenericTokenError' } });
          else cb({ status: 'succeeded', value: 'EXCH.TOKEN.SIG' }); }
      }
    }
  };
}
if (fall.teams) {
  globalThis.microsoftTeams = { app: { initialize: function () { return Promise.resolve(); },
      getContext: function () { return Promise.resolve({ app: { theme: 'default' } }); },
      registerOnThemeChangeHandler: function () {} },
    authentication: { authenticate: function (o) { log.fenster.push(o.url); return new Promise(function () {}); } } };
}
(0, eval)(fall.baustein);
(0, eval)(fall.seite);
setTimeout(function () {
  if (fall.klick) element('tab-anmelden-btn').hoerer.click();   // zweiter Anlauf per Knopf
  setTimeout(function () {
    log.knopf = element('tab-anmelden').hidden === false;
    log.speicher = speicher['exo_addin_session'] || null;
    log.fehler = element('fehler').textContent;
    process.stdout.write(JSON.stringify(log));
    process.exit(0);
  }, 100);
}, 100);
"""


def _seitenskript(html: str) -> str:
    """Das Hauptskript von self.html (inline, mit IM_TEAMS) — nach Jinja."""
    for m in re.finditer(r"<script>(.*?)</script>", html, re.S):
        if "var IM_TEAMS" in m.group(1):
            return m.group(1)
    raise AssertionError("Hauptskript nicht gefunden")


@pytest.fixture
def seite_lauf(seite):
    node = shutil.which("node")
    if not node:
        pytest.skip("node fehlt (in der CI vorhanden)")
    skripte = {p: _seitenskript(seite(p)) for p in ("/self", "/self?host=teams")}

    def _lauf(**fall):
        teams = fall.pop("teams", False)
        f = {"office": not teams, "teams": teams, "rahmen": False, "naaSet": True, "naaFehler": None,
             "accountType": "office365", "identFehler": False, "sitzung": 200, "klick": False,
             "sitzungApi": av.TAB_SITZUNG_API,
             "baustein": av.ANMELDUNG_DATEI.read_text("utf-8"),
             "seite": skripte["/self?host=teams" if teams else "/self"]}
        f.update(fall)
        out = subprocess.run([node, "-e", SEITEN_HARNESS], input=json.dumps(f), capture_output=True,
                             text=True, timeout=30)
        assert out.returncode == 0, out.stderr[-2000:]
        return json.loads(out.stdout)
    return _lauf


def _tausche(p):
    return [a for a in p["abrufe"] if a["url"] == av.TAB_SITZUNG_API]


@pytest.mark.parametrize("rahmen", [False, True])      # Classic (eigenes Fenster) / OWA (Rahmen)
def test_aufgabenbereich_cloud_konto_still_per_naa(seite_lauf, rahmen):
    p = seite_lauf(rahmen=rahmen)
    (t,) = _tausche(p)
    assert t["methode"] == "POST" and t["kopf"] == {"Authorization": "Bearer NAA.TOKEN"}
    # Plattform fürs Protokoll (sonst „unbekannt“ im Log, Desktop ≠ Handy nicht erkennbar)
    assert t["voll"].endswith("?client=PC"), t["voll"]
    assert p["naa"] == 1 and p["identitaet"] == 0 and p["dialoge"] == []
    assert p["speicher"] == "SITZUNG"
    # erst ohne, dann mit Sitzungsmerkmal (403 = Seite hat neu geladen)
    assert [a["kopf"].get("X-Addin-Session") for a in p["abrufe"] if a["url"] == "/api/self/context"] \
        == [None, "SITZUNG"]


@pytest.mark.parametrize("art,fehler", [("enterprise", "no_account_error"),
                                        ("enterprise", "interaction_required"),
                                        (None, "crypto_nonexistent")])
def test_aufgabenbereich_exchange_lokal_still_per_identitaetstoken(seite_lauf, art, fehler):
    p = seite_lauf(accountType=art, naaFehler=fehler)
    assert p["naa"] == 1 and p["identitaet"] == 1                  # NAA zuerst
    (t,) = _tausche(p)
    assert t["kopf"] == {ei.KOPF: "EXCH.TOKEN.SIG"}
    assert p["dialoge"] == [] and p["speicher"] == "SITZUNG"


def test_aufgabenbereich_ohne_naa_gleich_identitaetstoken(seite_lauf):
    p = seite_lauf(accountType="enterprise", naaSet=False)
    assert p["naa"] == 0 and p["identitaet"] == 1 and p["dialoge"] == []


def test_cloud_konto_nimmt_nie_das_identitaetstoken(seite_lauf):
    """Exchange-Online-Tokens sind abgeschaltet — kein sinnloser Abruf."""
    p = seite_lauf(accountType="office365", naaFehler="crypto_nonexistent")
    assert p["identitaet"] == 0 and _tausche(p) == []
    assert len(p["dialoge"]) == 1 and "/auth/login/microsoft" in p["dialoge"][0]


@pytest.mark.parametrize("fall", [
    {"accountType": "enterprise", "naaFehler": "no_account_error", "identFehler": True},  # beides scheitert
    {"accountType": "office365", "sitzung": 403},                     # Gateway lehnt ab
    {"accountType": "enterprise", "naaFehler": "x", "sitzung": 401},  # Token abgelehnt
])
def test_erst_wenn_beides_scheitert_das_fenster_und_nur_ein_stiller_versuch(seite_lauf, fall):
    p = seite_lauf(klick=True, **fall)
    assert p["naa"] <= 1 and p["identitaet"] <= 1
    assert len(_tausche(p)) <= 1
    # erster Versuch still, dann Fenster; nach „Ignorieren“ der Knopf, sein Klick
    # öffnet wieder das Fenster — KEIN zweiter stiller Versuch
    assert len(p["dialoge"]) == 2 and p["knopf"]
    assert any("Stille Anmeldung nicht m" in k for k in p["konsole"])


def test_tab_unveraendert_nur_naa(seite_lauf):
    p = seite_lauf(teams=True)
    (t,) = _tausche(p)
    assert t["kopf"] == {"Authorization": "Bearer NAA.TOKEN"}
    assert p["identitaet"] == 0 and p["fenster"] == [] and p["speicher"] == "SITZUNG"


def test_tab_ohne_naa_knopf_ohne_selbsttaetiges_fenster(seite_lauf):
    p = seite_lauf(teams=True, naaFehler="crypto_nonexistent", klick=True)
    assert _tausche(p) == [] and p["identitaet"] == 0 and p["naa"] == 1
    assert p["knopf"] and len(p["fenster"]) == 1                   # erst auf Klick


# ── Baustein allein ───────────────────────────────────────────────────────────

BAUSTEIN_HARNESS = r"""
var fall = JSON.parse(require('fs').readFileSync(0, 'utf8'));
var abrufe = 0;
globalThis.fetch = function () { abrufe++;
  return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve({ token: 'S' }); } }); };
globalThis.msal = { createNestablePublicClientApplication: function () {
  return Promise.resolve({ acquireTokenSilent: function () { return Promise.resolve({ accessToken: 'T' }); } }); } };
(0, eval)(fall.baustein);
var weg = ExoAnmeldung.erstelle({ clientId: 'C', authority: 'A', scope: 'S', sitzung: '/s', identkopf: 'X-I' });
var erg = [];
weg.sitzung().then(function (t) { erg.push(t); return weg.sitzung(); })
  .then(function () { erg.push('zweimal'); }, function (e) { erg.push(e.message); })
  .then(function () { process.stdout.write(JSON.stringify({ erg: erg, abrufe: abrufe,
    kopf: weg.kopf({ art: 'exchange', wert: 'W' }) })); });
"""


def test_baustein_eine_sitzung_je_instanz_ohne_office_host():
    node = shutil.which("node")
    if not node:
        pytest.skip("node fehlt (in der CI vorhanden)")
    out = subprocess.run([node, "-e", BAUSTEIN_HARNESS],
                         input=json.dumps({"baustein": av.ANMELDUNG_DATEI.read_text("utf-8")}),
                         capture_output=True, text=True, timeout=30, check=True)
    p = json.loads(out.stdout)
    assert p["erg"] == ["S", "stille Anmeldung schon versucht"] and p["abrufe"] == 1
    assert p["kopf"] == {"X-I": "W"}


# ── Verfassen-Aufgabenbereich /addin/compose (seit v1.12.97) ───────────────────
# Derselbe Knopf-Add-in, zweiter Aufgabenbereich: bis v1.12.96 nur das
# Anmeldefenster. Jetzt dieselben Angaben, derselbe Baustein, derselbe Tausch.

def test_compose_liefert_dieselben_naa_angaben_wie_self(seite):
    naa = _naa(seite(addin.KNOPF_FORM_PFAD))
    assert naa == _naa(seite("/self"))                     # EINE Quelle
    assert naa["sitzung"] == av.TAB_SITZUNG_API and naa["identkopf"] == ei.KOPF
    assert naa["clientId"] == "CID"


def test_compose_bindet_msal_und_den_baustein_vor_dem_seitenskript_ein(seite):
    html = seite(addin.KNOPF_FORM_PFAD)
    msal = html.index(f'src="/static/vendor/{av.MSAL_DATEI.name}"')
    baustein = html.index(f'src="/static/{av.ANMELDUNG_DATEI.name}?v=')
    assert msal < baustein < html.index("var NAA =")


COMPOSE_HARNESS = r"""
var fall = JSON.parse(require('fs').readFileSync(0, 'utf8'));
var log = { abrufe: [], dialoge: [], identitaet: 0, naa: 0, konsole: [] };
var elemente = {};
function element(id) {
  if (!elemente[id]) elemente[id] = { id: id, textContent: '', innerHTML: '', value: '', disabled: false,
    style: {}, className: '', appendChild: function () {} };
  return elemente[id];
}
element('login-required').style.display = 'none';
globalThis.window = globalThis;
globalThis.location = { origin: 'https://sig.firma.at' };
globalThis.document = { getElementById: element, createElement: function () { return element('_neu'); } };
var speicher = {};
if (fall.gespeichert) speicher['exo_addin_session'] = fall.gespeichert;
globalThis.localStorage = { getItem: function (k) { return k in speicher ? speicher[k] : null; },
  setItem: function (k, v) { speicher[k] = String(v); }, removeItem: function (k) { delete speicher[k]; } };
console.info = function () { log.konsole.push([].slice.call(arguments).join(' ')); };
var widerrufen = false;
function antwort(s, body) {
  return Promise.resolve({ ok: s >= 200 && s < 300, status: s, json: function () { return Promise.resolve(body); } });
}
globalThis.fetch = function (url, opt) {
  opt = opt || {};
  var kopf = opt.headers || {};
  var pfad = url.split('?')[0];
  log.abrufe.push({ url: pfad, methode: opt.method || 'GET', kopf: kopf });
  var gueltig = kopf['X-Addin-Session'] === 'SITZUNG' && !widerrufen;
  if (pfad === '/api/addin/templates')
    return antwort(gueltig ? 200 : 401, { templates: ['Standard', 'Kurz'], default: 'Standard' });
  if (pfad === '/api/addin/signature')
    return antwort(gueltig ? 200 : 401, { marked_html: '<p>Max</p>', preview_html: '<p>Max</p>' });
  if (pfad === fall.sitzungApi) return antwort(fall.sitzung, fall.sitzung === 200 ? { token: 'SITZUNG' } : {});
  return antwort(204, {});
};
globalThis.msal = { createNestablePublicClientApplication: function () {
  log.naa++;
  return Promise.resolve({ acquireTokenSilent: function () {
    if (fall.naaFehler) { var e = new Error('MSAL'); e.errorCode = fall.naaFehler; return Promise.reject(e); }
    return Promise.resolve({ accessToken: 'NAA.TOKEN' });
  } });
} };
globalThis.Office = {
  AsyncResultStatus: { Succeeded: 'succeeded', Failed: 'failed' },
  HostType: { Outlook: 'Outlook' },
  EventType: { DialogMessageReceived: 'm', DialogEventReceived: 'e' },
  CoercionType: { Html: 'html', Text: 'text' },
  onReady: function (cb) { setTimeout(function () { cb({ host: 'Outlook' }); }, 0); },
  context: {
    diagnostics: { platform: 'PC' },
    requirements: { isSetSupported: function (n) { return n === 'NestedAppAuth' ? fall.naaSet : true; } },
    // 12009 = „Ignorieren“ im Office-Hinweis — das Fenster bleibt zu.
    ui: { displayDialogAsync: function (url, o, cb) { log.dialoge.push(url);
      cb({ status: 'failed', error: { code: 12009, message: 'ignoriert' } }); } },
    mailbox: {
      userProfile: { emailAddress: 'max.mustermann@firma.at', accountType: fall.accountType },
      getUserIdentityTokenAsync: function (cb) { log.identitaet++;
        if (fall.identFehler) cb({ status: 'failed', error: { message: 'GenericTokenError' } });
        else cb({ status: 'succeeded', value: 'EXCH.TOKEN.SIG' }); }
    }
  }
};
(0, eval)(fall.baustein);
(0, eval)(fall.seite);
var schritte = (fall.aktionen || []).slice();
function weiter() {
  setTimeout(function () {
    if (!schritte.length) {
      log.knopf = element('login-required').style.display === '';
      log.speicher = speicher['exo_addin_session'] || null;
      process.stdout.write(JSON.stringify(log));
      process.exit(0);
    }
    var a = schritte.shift();
    log.abrufe.push({ url: '#' + a });
    if (a === 'widerrufen') widerrufen = true; else globalThis[a]();
    weiter();
  }, 100);
}
weiter();
"""


def _compose_skript(html: str) -> str:
    for m in re.finditer(r"<script>(.*?)</script>", html, re.S):
        if "var NAA =" in m.group(1):
            return m.group(1)
    raise AssertionError("Seitenskript von addin_compose.html nicht gefunden")


@pytest.fixture
def compose_lauf(seite):
    node = shutil.which("node")
    if not node:
        pytest.skip("node fehlt (in der CI vorhanden)")
    skript = _compose_skript(seite(addin.KNOPF_FORM_PFAD))

    def _lauf(**fall):
        f = {"naaSet": True, "naaFehler": None, "accountType": "office365", "identFehler": False,
             "sitzung": 200, "gespeichert": None, "aktionen": [],
             "sitzungApi": av.TAB_SITZUNG_API,
             "baustein": av.ANMELDUNG_DATEI.read_text("utf-8"), "seite": skript}
        f.update(fall)
        out = subprocess.run([node, "-e", COMPOSE_HARNESS], input=json.dumps(f), capture_output=True,
                             text=True, timeout=30)
        assert out.returncode == 0, out.stderr[-2000:]
        return json.loads(out.stdout)
    return _lauf


def _pfade(p):
    return [a["url"] for a in p["abrufe"]]


def _sig_kopfe(p):
    return [a["kopf"].get("X-Addin-Session") for a in p["abrufe"] if a["url"] == "/api/addin/signature"]


def test_compose_ohne_merkmal_meldet_sich_vor_dem_ersten_abruf_still_an(compose_lauf):
    p = compose_lauf()
    (t,) = _tausche(p)
    assert t["methode"] == "POST" and t["kopf"] == {"Authorization": "Bearer NAA.TOKEN"}
    pf = _pfade(p)
    assert pf.index(av.TAB_SITZUNG_API) < pf.index("/api/addin/templates")
    assert _sig_kopfe(p) == ["SITZUNG"]                    # kein Abruf ohne Merkmal
    assert p["dialoge"] == [] and not p["knopf"] and p["speicher"] == "SITZUNG"


@pytest.mark.parametrize("art,fehler", [("enterprise", "no_account_error"), (None, "crypto_nonexistent")])
def test_compose_exchange_lokal_still_per_identitaetstoken(compose_lauf, art, fehler):
    p = compose_lauf(accountType=art, naaFehler=fehler)
    assert p["naa"] == 1 and p["identitaet"] == 1
    (t,) = _tausche(p)
    assert t["kopf"] == {ei.KOPF: "EXCH.TOKEN.SIG"}
    assert p["dialoge"] == [] and p["speicher"] == "SITZUNG"


def test_compose_abgelaufenes_merkmal_401_dann_einmal_still(compose_lauf):
    """Gespeichertes Merkmal gilt nicht mehr: kein Vorab-Versuch, erst der 401
    löst den stillen Weg aus, danach lädt die Seite mit dem neuen Merkmal."""
    p = compose_lauf(gespeichert="ALT")
    pf = _pfade(p)
    assert pf.index("/api/addin/signature") < pf.index(av.TAB_SITZUNG_API)
    assert len(_tausche(p)) == 1
    assert _sig_kopfe(p) == ["ALT", "SITZUNG"]
    assert p["dialoge"] == [] and not p["knopf"] and p["speicher"] == "SITZUNG"


@pytest.mark.parametrize("fall", [
    {"sitzung": 403},                                                   # Gateway lehnt ab
    {"accountType": "office365", "naaFehler": "crypto_nonexistent"},    # kein NAA, Cloud-Konto
    {"accountType": "enterprise", "naaFehler": "x", "identFehler": True},  # beides scheitert
    {"gespeichert": "ALT", "sitzung": 401},                             # nach 401 still gescheitert
])
def test_compose_scheitert_still_dann_knopf_kein_selbsttaetiges_fenster(compose_lauf, fall):
    p = compose_lauf(aktionen=["loadSig", "doLogin"], **fall)
    assert len(_tausche(p)) <= 1 and p["naa"] <= 1 and p["identitaet"] <= 1
    # „Erneut versuchen“ (loadSig) startet KEINEN zweiten stillen Versuch;
    # erst der Klick auf „Jetzt anmelden“ öffnet das Fenster — genau einmal.
    vor_klick = _pfade(p)[:_pfade(p).index("#doLogin")]
    assert vor_klick.count(av.TAB_SITZUNG_API) == len(_tausche(p))
    assert len(p["dialoge"]) == 1 and "/auth/login/microsoft" in p["dialoge"][0]
    assert p["knopf"]
    assert any("Stille Anmeldung nicht m" in k for k in p["konsole"])


def test_compose_kein_kreislauf_wenn_das_neue_merkmal_spaeter_401_bekommt(compose_lauf):
    """Nach erfolgreichem stillem Versuch wird das Merkmal ungültig (Widerruf):
    ein 401 führt zum Knopf — nicht erneut still und nicht in eine Schleife."""
    p = compose_lauf(aktionen=["widerrufen", "loadSig"])
    nach = _pfade(p)[_pfade(p).index("#widerrufen"):]
    assert nach.count("/api/addin/signature") == 1 and av.TAB_SITZUNG_API not in nach
    assert len(_tausche(p)) == 1 and p["knopf"] and p["dialoge"] == []


def test_compose_nach_abmelden_kein_stiller_versuch(compose_lauf):
    """Bewusst abgemeldet: Der nächste 401 meldet nicht still wieder an."""
    p = compose_lauf(gespeichert="SITZUNG", aktionen=["abmelden", "loadSig"])
    assert _tausche(p) == [] and p["knopf"] and p["speicher"] is None


def test_compose_knopf_vor_dem_stillen_versuch_versucht_erst_still(compose_lauf):
    """„Jetzt anmelden“, solange noch kein stiller Versuch lief (gültiges
    Merkmal geladen): erst still, das Fenster nur, wenn das scheitert."""
    p = compose_lauf(gespeichert="SITZUNG", aktionen=["doLogin"])
    assert len(_tausche(p)) == 1 and p["dialoge"] == []
    p = compose_lauf(gespeichert="SITZUNG", sitzung=403, aktionen=["doLogin"])
    assert len(_tausche(p)) == 1 and len(p["dialoge"]) == 1


# ── Die von /addin/compose genutzten Endpunkte nehmen die ausgestellte Sitzung ──

@pytest.fixture
def compose_api(tausch, monkeypatch):
    """Merkmal aus dem echten Tausch (Exchange-Identitätstoken → ROLE_SELF),
    dann die Endpunkte des Verfassen-Aufgabenbereichs über die App — nur mit
    dem Kopf X-Addin-Session, ohne Keks (so schickt es der Aufgabenbereich)."""
    from starlette.testclient import TestClient
    import graph_client
    import signature_engine
    from webui import app as wa
    _d, lauf = tausch
    code, body = lauf({ei.KOPF: _token(aud=AUD_KNOPF)})
    assert code == 200
    monkeypatch.setattr(sso, "_noch_gueltig", lambda p: True)

    async def _nutzer(email):
        return graph_client.UserData(mail=email)
    monkeypatch.setattr(graph_client, "get_user", _nutzer)
    monkeypatch.setattr(signature_engine, "render", lambda u, t: ("<p>Max Mustermann</p>", "Max"))
    with TestClient(wa.app) as c:
        yield c, {"X-Addin-Session": body["token"]}


@pytest.mark.parametrize("pfad", ["/api/addin/templates", "/api/addin/signature"])
def test_ausgestellte_sitzung_traegt_die_compose_endpunkte(compose_api, pfad):
    c, kopf = compose_api
    r = c.get(pfad, params={"email": HYBRID}, headers=kopf)
    assert r.status_code == 200, r.text[:200]
    assert c.get(pfad, params={"email": HYBRID}).status_code == 401          # Gegenprobe ohne Kopf
    # ROLE_SELF: nur das eigene Postfach, nie ein fremdes
    assert c.get(pfad, params={"email": "erika.musterfrau@firma.at"}, headers=kopf).status_code == 403


def test_ausgestellte_sitzung_traegt_die_meldung(compose_api, caplog):
    c, kopf = compose_api
    with caplog.at_level(logging.WARNING):
        r = c.post(av.MELDUNG_API, json={"quelle": "aufgabenbereich", "text": "Test"},
                   headers={**kopf, "Content-Type": "application/json"})
    assert r.status_code == 204
    assert any(HYBRID in rec.getMessage() for rec in caplog.records)


def test_classic_aufgabenbereich_gilt_als_office_auch_ohne_rahmen():
    """Classic Outlook (Win32) zeigt den Aufgabenbereich in einem eigenen
    Fenster: window.self === window.top. Ohne Erkennung über _host_Info lief
    /self in den Browser-Zweig mit 1,5-s-Zeitgeber und sprang bei langsamem
    office.js zur Anmeldeseite (Lab, 1 von 3 Erstaufrufen)."""
    from pathlib import Path
    seite = (Path(__file__).resolve().parent.parent / "app/webui/templates/self.html").read_text("utf-8")
    m = re.search(r"var IM_OFFICE_HOST = ([^;]+);", seite)
    assert m and "_host_info" in m.group(1).lower(), "Erkennung über _host_Info fehlt"
    rahmen = re.search(r"var IM_RAHMEN = ([^;]+);", seite, re.S)
    assert rahmen and "IM_OFFICE_HOST" in rahmen.group(1)
    # Der Zeitgeber-Zweig darf nur für echte Browserfenster bleiben.
    zweig = seite[seite.index("} else if (IM_RAHMEN) {"):]
    assert zweig.index("} else {") < zweig.index("setTimeout(start, 1500)")


def test_jede_zeile_der_stillen_anmeldung_nennt_die_plattform(tausch, caplog):
    """Lab 19.11.: Die Identitätstoken-Zeile nannte „PC“, die Zeile „Stille
    Anmeldung per …“ (auch bei NAA) nicht — Desktop und Web waren dort nicht
    zu unterscheiden, obwohl ?client= mitkam."""
    daten, lauf = tausch
    with caplog.at_level(logging.INFO):
        assert lauf({ei.KOPF: _token(aud=AUD_KNOPF)})[0] == 200
        daten["SELF_SERVICE_ENABLED"] = False
        assert lauf({ei.KOPF: _token(aud=AUD_KNOPF)})[0] == 403
    zeilen = [r.getMessage() for r in caplog.records if "Stille Anmeldung per" in r.getMessage()]
    assert len(zeilen) == 2 and all("(PC)" in z for z in zeilen), zeilen
