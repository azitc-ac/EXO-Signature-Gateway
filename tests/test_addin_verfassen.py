"""Verfassen-Add-in (Start-Ereignis + NAA): Token-Prüfung, Endpunkt, Manifest.

Der Signatur-Endpunkt nimmt ein Bearer-Token OHNE Sitzung und ohne nonce an —
das Token ist die ganze Anmeldung. Deshalb muss jede einzelne Bedingung greifen
(Signatur, Aussteller, Audience, azp, Bereich, Tenant, Version, Ablauf), und die
Adresse kommt aus dem Token, nie aus einem Parameter.
"""
import asyncio
import json
import time
import xml.etree.ElementTree as ET

import pytest

import graph_client
import settings_store
import signature_engine
import sso
from webui.routen import addin, addin_verfassen as av


# ── Token ───────────────────────────────────────────────────────────────────────

def _keypair():
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


class _JWKS:
    def __init__(self, pub):
        self._pub = pub

    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": self._pub})()


_PRIV, _PUB = _keypair()


def _token(priv=_PRIV, **over):
    import jwt
    now = int(time.time())
    claims = {"aud": "CID", "iss": "https://login.microsoftonline.com/TID/v2.0",
              "tid": "TID", "ver": "2.0", "azp": "CID", "scp": "access_as_user",
              "exp": now + 300, "iat": now, "oid": "OID-1",
              "preferred_username": "Erika@Zarenko.net"}
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, priv, algorithm="RS256")


@pytest.fixture
def umgebung(monkeypatch):
    daten = {"BOOTSTRAP_CLIENT_ID": "CID", "TENANT_ID": "TID",
             "ADDIN_BASE_URL": "https://sig.example",
             "MAILBOX_CONFIG": {"erika@zarenko.net": {"template": "default"}}}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: daten.get(k, d))
    monkeypatch.setattr(settings_store, "force_update", lambda patch: daten.update(patch))
    monkeypatch.setattr(sso, "_jwks_client", lambda uri: _JWKS(_PUB))
    return daten


def test_gueltiges_token_wird_angenommen(umgebung):
    c = sso.verify_access_token(_token())
    assert c and c["preferred_username"] == "Erika@Zarenko.net"


@pytest.mark.parametrize("abweichung", [
    {"aud": "FREMD"},
    {"aud": "api://CID"},                                   # v1-Form der Audience
    {"iss": "https://sts.windows.net/TID/"},                 # v1-Aussteller
    {"iss": "https://login.microsoftonline.com/ANDERER/v2.0"},
    {"tid": "ANDERER"},
    {"ver": "1.0"},
    {"ver": None},
    {"azp": "FREMDE-APP"},
    {"azp": None},
    {"scp": "User.Read"},
    {"scp": "access_as_user_x"},                            # Teilwort reicht nicht
    {"scp": None},
    {"exp": int(time.time()) - 300},
], ids=lambda d: ",".join(f"{k}={v}" for k, v in d.items()))
def test_jede_abweichung_wird_abgewiesen(umgebung, abweichung):
    assert sso.verify_access_token(_token(**abweichung)) is None


def test_fremder_schluessel_wird_abgewiesen(umgebung):
    fremd, _ = _keypair()
    assert sso.verify_access_token(_token(priv=fremd)) is None


def test_ohne_tenant_kein_rueckfall_auf_organizations(umgebung):
    umgebung["TENANT_ID"] = ""
    import config
    if getattr(config, "TENANT_ID", ""):
        pytest.skip("TENANT_ID aus der Umgebung gesetzt")
    # Ein Token, das zum leeren Tenant „passt“ — ohne den Wächter ginge es durch.
    leer = _token(iss="https://login.microsoftonline.com//v2.0", tid="")
    assert sso.verify_access_token(leer) is None


# ── Endpunkt ────────────────────────────────────────────────────────────────────

class _Req:
    def __init__(self, auth=None):
        self.headers = {"authorization": auth} if auth else {}
        self.url = type("U", (), {"scheme": "https", "netloc": "sig.example",
                                  "hostname": "sig.example"})()


@pytest.fixture
def rendern(monkeypatch):
    aufrufe = []

    async def _user(email):
        aufrufe.append(email)
        return graph_client.UserData(mail=email)
    monkeypatch.setattr(graph_client, "get_user", _user)
    monkeypatch.setattr(signature_engine, "render", lambda u, t: (f"<p>SIG {u.mail} {t}</p>", ""))
    return aufrufe


def _abruf(auth):
    r = asyncio.run(av.verfassen_signatur(_Req(auth)))
    return r.status_code, json.loads(r.body), r.headers


def test_signatur_des_token_inhabers_markiert(umgebung, rendern):
    code, body, kopf = _abruf("Bearer " + _token())
    assert code == 200
    assert rendern == ["erika@zarenko.net"]                  # aus dem Token, kleingeschrieben
    assert body["html"] == addin.markiert("<p>SIG erika@zarenko.net default</p>")
    assert body["vorlage"] == "default"                      # für den Hinweis auf dem Handy
    assert 'id="exo-sig-s"' in body["html"]                  # Gateway erkennt sie wieder
    assert kopf["access-control-allow-origin"] == "*"


@pytest.mark.parametrize("auth", [None, "Bearer ", "Basic abc", "Bearer kaputt"])
def test_ohne_gueltiges_token_401(umgebung, rendern, auth):
    code, _body, kopf = _abruf(auth)
    assert code == 401 and rendern == []
    assert kopf["access-control-allow-origin"] == "*"        # sonst sieht das Skript nur „Netzfehler“


def test_falsches_token_401_auch_mit_bekannter_adresse(umgebung, rendern):
    code, _b, _k = _abruf("Bearer " + _token(azp="FREMDE-APP"))
    assert code == 401 and rendern == []


def test_nicht_betreutes_postfach_bekommt_nichts(umgebung, rendern):
    umgebung["MAILBOX_CONFIG"] = {"andere@zarenko.net": {}}
    code, body, _k = _abruf("Bearer " + _token())
    assert code == 200 and body == {"html": ""} and rendern == []


def test_vorabanfrage_erlaubt_authorization(umgebung):
    r = asyncio.run(av.verfassen_signatur_vorab())
    assert r.status_code == 204
    assert "Authorization" in r.headers["access-control-allow-headers"]


# ── Manifest, Laufzeit, well-known ──────────────────────────────────────────────

NS = {"o": "http://schemas.microsoft.com/office/appforoffice/1.1",
      "bt": "http://schemas.microsoft.com/office/officeappbasictypes/1.0"}


def _manifest():
    return ET.fromstring(asyncio.run(av.verfassen_manifest(_Req())).body)


def _urls(root):
    return {e.get("id"): e.get("DefaultValue") for e in root.iter() if e.tag.endswith("}Url")}


def test_eigene_kennung_nicht_die_des_einstell_addins(umgebung):
    root = _manifest()
    eigene = root.find("o:Id", NS).text
    assert eigene == av.addin_id("sig.example")
    bisher = ET.fromstring(asyncio.run(addin.addin_manifest(_Req())).body).find("o:Id", NS).text
    assert eigene != bisher


def test_start_ereignis_und_laufzeit(umgebung):
    root = _manifest()
    ereignisse = [e for e in root.iter() if e.tag.endswith("}LaunchEvent")]
    assert [(e.get("Type"), e.get("FunctionName")) for e in ereignisse] == [
        ("OnNewMessageCompose", "beimVerfassen")] * 2      # Desktop + Mobil
    handler = av.HANDLER_DATEI.read_text(encoding="utf-8")
    assert "Office.actions.associate('beimVerfassen'" in handler
    override = next(e for e in root.iter() if e.tag.endswith("}Override"))
    assert override.get("type") == "javascript"
    assert _urls(root)[override.get("resid")] == "https://sig.example" + av.LAUFZEIT_JS


V11 = "{http://schemas.microsoft.com/office/mailappversionoverrides/1.1}"


@pytest.mark.parametrize("formfaktor", ["DesktopFormFactor", "MobileFormFactor"])
def test_beide_formfaktoren_starten_dieselbe_laufzeit(umgebung, formfaktor):
    """Outlook mobil (iOS/Android) braucht einen eigenen MobileFormFactor; ohne
    ihn passiert auf dem Handy beim Verfassen nichts."""
    root = _manifest()
    ff = next(e for e in root.iter() if e.tag == V11 + formfaktor)
    punkt = ff.find(V11 + "ExtensionPoint")
    assert punkt.get("{http://www.w3.org/2001/XMLSchema-instance}type") == "LaunchEvent"
    ereignis = punkt.find(f"{V11}LaunchEvents/{V11}LaunchEvent")
    assert (ereignis.get("Type"), ereignis.get("FunctionName")) == (
        "OnNewMessageCompose", "beimVerfassen")
    laufzeit = next(e for e in root.iter() if e.tag == V11 + "Runtime")
    assert punkt.find(V11 + "SourceLocation").get("resid") == laufzeit.get("resid")


def test_mindestversion_laesst_outlook_mobil_zu(umgebung):
    """Outlook mobil meldet nur Mailbox 1.5. Verlangt das Manifest mehr, wird
    das Add-in dort nie geladen — trotz MobileFormFactor."""
    root = _manifest()
    v11 = next(e for e in root.iter() if e.tag == V11 + "VersionOverrides")
    sets = v11.find(f"{V11}Requirements/{{{NS['bt']}}}Sets")
    assert sets.get("DefaultMinVersion") == "1.5"


def test_well_known_nennt_genau_das_skript_aus_dem_manifest(umgebung):
    """Classic Outlook gibt SSO nur für Skripte frei, die dort aufgeführt sind —
    weicht die Adresse ab, scheitert die Anmeldung still."""
    root = _manifest()
    override = next(e for e in root.iter() if e.tag.endswith("}Override"))
    erlaubt = json.loads(asyncio.run(av.officeaddins_allowed(_Req())).body)["allowed"]
    assert erlaubt == [_urls(root)[override.get("resid")]]


def test_laufzeit_skript_ist_eine_datei_mit_msal_konfig_und_handler(umgebung):
    inhalt = asyncio.run(av.verfassen_laufzeit_js(_Req())).body.decode("utf-8")
    assert inhalt.startswith("/*! @azure/msal-browser v3.30.0")
    assert "createNestablePublicClientApplication" in inhalt
    konf = json.loads(inhalt.split("var EXO_VERFASSEN = ", 1)[1].split(";\n", 1)[0])
    assert konf == {"clientId": "CID",
                    "authority": "https://login.microsoftonline.com/TID",
                    "scope": "api://CID/access_as_user",
                    "api": "https://sig.example" + av.SIGNATUR_API,
                    "kettenkopf": "X-Exo-Sig-Kette",
                    "app": "Signatur"}          # Abwesenheit aus → App heißt „Signatur“
    assert "\nimport " not in inhalt and "\nexport " not in inhalt   # Classic: keine Importe


def test_msal_datei_unveraendert():
    """Abgelegt aus dem npm-Paket @azure/msal-browser 3.30.0 (Integrität laut
    Register geprüft). Eine stille Änderung der Datei soll auffallen."""
    import hashlib
    assert hashlib.sha256(av.MSAL_DATEI.read_bytes()).hexdigest() == \
        "273d446b381814e563442267a072904fb3b7fd0164f4a688ede80f0d74565b3e"


def test_handler_beendet_immer():
    """Statisch, weil Office.js hier nicht läuft: Abschluss über
    event.completed() am Ende der Kette. Antworten: tests/test_addin_antwort.py."""
    js = av.HANDLER_DATEI.read_text(encoding="utf-8")
    assert js.count("event.completed();") == 2          # Kommentar oben nennt es ohne „;“
    assert ".then(function () { event.completed(); });" in js
    assert "acquireTokenPopup(" not in js and "loginPopup(" not in js   # kein Fenster im Ereignis


def _handler_lauf(item_js: str, plattform: str = "iOS", antwort: str = "{html: '<p>SIG</p>'}") -> dict:
    """Den echten Handler in Node ausführen, mit nachgebautem Office-Objekt."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node fehlt (in der CI vorhanden)")
    js = """
var protokoll = {fertig: 0, abrufe: 0, hinweise: [], gesetzt: null};
var handler = null;
var item = %s;
item.notificationMessages = {replaceAsync: function (id, m) { protokoll.hinweise.push(m.message); }};
globalThis.Office = {
  AsyncResultStatus: {Succeeded: 'succeeded', Failed: 'failed'},
  CoercionType: {Html: 'html'},
  MailboxEnums: {ItemNotificationMessageType: {InformationalMessage: 'info'}},
  context: {mailbox: {item: item}, diagnostics: {platform: PLATTFORM}},
  actions: {associate: function (n, f) { handler = f; }}
};
globalThis.msal = {createNestablePublicClientApplication: function () {
  return Promise.resolve({acquireTokenSilent: function () {
    return Promise.resolve({accessToken: 'T'}); }}); }};
globalThis.EXO_VERFASSEN = {clientId: 'C', authority: 'A', scope: 'S', api: 'https://x/api', app: 'Signatur & Abwesenheit'};
globalThis.fetch = function (url) { protokoll.abrufe++; protokoll.url = url;
  return Promise.resolve({ok: true, json: function () { return Promise.resolve(ANTWORT); }}); };
""".replace("PLATTFORM", json.dumps(plattform)).replace("ANTWORT", antwort) % item_js + av.HANDLER_DATEI.read_text(encoding="utf-8") + """
handler({completed: function () { protokoll.fertig++; }});
setTimeout(function () { process.stdout.write(JSON.stringify(protokoll)); }, 50);
"""
    out = subprocess.run([node, "-e", js], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


_OK = "function (cb) { cb({status: 'succeeded', value: %s}); }"
_VOLL = ("{getComposeTypeAsync: " + _OK % "{composeType: 'newMail'}" +
         ", disableClientSignatureAsync: " + _OK % "null" +
         ", body: {setSignatureAsync: function (h, o, cb) { protokoll.gesetzt = h; "
         "cb({status: 'succeeded'}); }}}")


def test_handler_setzt_die_signatur_und_meldet_die_plattform():
    p = _handler_lauf(_VOLL)
    assert p["gesetzt"] == "<p>SIG</p>" and p["fertig"] == 1 and not p["hinweise"]
    assert p["url"] == "https://x/api?client=iOS"


@pytest.mark.parametrize("item", [
    "{body: {}}",                                                    # beides fehlt
    "{getComposeTypeAsync: " + _OK % "{composeType: 'newMail'}" + ", body: {}}",
    "{body: {setSignatureAsync: function () {}}}",
])
def test_ohne_signatur_apis_still_beenden(item):
    """Das Manifest verlangt nur Mailbox 1.5 (Outlook mobil). Fehlen die APIs,
    beendet der Handler sofort — ohne Abruf und ohne Fehlerhinweis."""
    p = _handler_lauf(item)
    assert p == {"fertig": 1, "abrufe": 0, "hinweise": [], "gesetzt": None}


@pytest.mark.parametrize("roh, erwartet", [
    ("iOS", "iOS"), ("Android", "Android"), ("PC", "PC"), (None, "unbekannt"),
    ("iOS\nFAKE 200 OK", "iOSFAKE200OK"), ("<script>", "script"), ("x" * 50, "x" * 20),
])
def test_plattform_im_protokoll_beschnitten(umgebung, rendern, caplog, roh, erwartet):
    req = _Req("Bearer " + _token())
    req.query_params = {"client": roh} if roh is not None else {}
    import logging
    with caplog.at_level(logging.INFO):
        asyncio.run(av.verfassen_signatur(req))
    zeile = next(r.getMessage() for r in caplog.records if "geliefert" in r.getMessage())
    assert zeile.endswith(f"({erwartet})")


# ── Tab: NAA-Token gegen Sitzungsmerkmal ───────────────────────────────────────

def _tab(auth, monkeypatch, self_service=True, admins=None):
    from webui import deps
    daten_get = settings_store.get

    def get(k, d=None):
        if k == "SELF_SERVICE_ENABLED":
            return self_service
        if k == "ADMIN_USERS":
            return admins or []
        return daten_get(k, d)
    monkeypatch.setattr(settings_store, "get", get)
    monkeypatch.setattr(sso, "_get_secret", lambda: "testgeheimnis")
    r = asyncio.run(av.tab_sitzung(_Req(auth)))
    return r.status_code, json.loads(r.body), r.headers, deps


def test_tab_tauscht_naa_token_gegen_self_sitzung(umgebung, monkeypatch):
    code, body, kopf, deps = _tab("Bearer " + _token(), monkeypatch)
    assert code == 200
    sitzung = sso.verify_session_cookie(body["token"])
    assert sitzung["u"] == "erika@zarenko.net"                 # aus dem Token, klein
    assert sitzung["r"] == sso.ROLE_SELF
    assert "no-store" in kopf["cache-control"]


def test_tab_gibt_auch_verwaltern_nur_self(umgebung, monkeypatch):
    """Ein Bearer-Token eröffnet keine Verwaltungssitzung."""
    code, body, _k, _d = _tab("Bearer " + _token(), monkeypatch,
                              admins=[{"upn": "erika@zarenko.net", "role": "admin"}])
    assert code == 200 and sso.verify_session_cookie(body["token"])["r"] == sso.ROLE_SELF


@pytest.mark.parametrize("auth", [None, "Bearer ", "Basic abc", "Bearer kaputt"])
def test_tab_ohne_gueltiges_token_401(umgebung, monkeypatch, auth):
    code, body, _k, _d = _tab(auth, monkeypatch)
    assert code == 401 and "token" not in body


def test_tab_fremde_app_401(umgebung, monkeypatch):
    code, _b, _k, _d = _tab("Bearer " + _token(azp="FREMD"), monkeypatch)
    assert code == 401


def test_tab_ohne_self_service_403(umgebung, monkeypatch):
    code, body, _k, _d = _tab("Bearer " + _token(), monkeypatch, self_service=False)
    assert code == 403 and "token" not in body


def test_tab_nicht_betreutes_postfach_403(umgebung, monkeypatch):
    code, body, _k, _d = _tab("Bearer " + _token(preferred_username="fremd@zarenko.net"), monkeypatch)
    assert code == 403 and "token" not in body


def test_tab_sitzung_ist_eine_post_route():
    routen = {(r.path, m) for r in av.router.routes for m in getattr(r, "methods", ())}
    assert (av.TAB_SITZUNG_API, "POST") in routen


_MIT_VORLAGE = "{html: '<p>SIG</p>', vorlage: 'Kompakt'}"


def test_handy_meldet_eingefuegte_signatur_und_wo_man_sie_aendert():
    """Auf dem Handy gibt es keinen Knopf beim Verfassen — die Zeile ist der
    einzige Hinweis, welche Signatur kam und wo man sie ändert."""
    for plattform in ("iOS", "Android"):
        p = _handler_lauf(_VOLL, plattform, _MIT_VORLAGE)
        assert p["gesetzt"] == "<p>SIG</p>" and p["fertig"] == 1
        assert p["hinweise"] == ["Signatur \u201eKompakt\u201c eingef\u00fcgt \u2013 "
                                 "\u00e4ndern unter Apps \u2192 Signatur & Abwesenheit"], plattform


@pytest.mark.parametrize("plattform", ["PC", "OfficeOnline", "Mac"])
def test_am_pc_kein_hinweis(plattform):
    """Dort steht der Knopf bereit — keine Zeile bei jeder neuen Mail."""
    p = _handler_lauf(_VOLL, plattform, _MIT_VORLAGE)
    assert p["gesetzt"] == "<p>SIG</p>" and p["hinweise"] == []


def test_kein_hinweis_ohne_eingefuegte_signatur():
    leer = _handler_lauf(_VOLL, "iOS", "{html: '', vorlage: ''}")
    assert leer["gesetzt"] is None and leer["hinweise"] == []
    fehl = _VOLL.replace("cb({status: 'succeeded'}); }}}", "cb({status: 'failed', error: {message: 'X'}}); }}}")
    assert fehl != _VOLL
    p = _handler_lauf(fehl, "iOS", _MIT_VORLAGE)
    assert len(p["hinweise"]) == 1 and "nicht eingef" in p["hinweise"][0]
