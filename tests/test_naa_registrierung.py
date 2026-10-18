"""NAA an der Bootstrap-App: Setzer (Einrichtungs-Login) und Prüfung teilen EIN Soll.

ANLASS (18.10.2026): Verfassen-Add-in und stille Tab-Anmeldung brauchen an der
Bootstrap-App vier Einträge (SPA-Umleitung brk-multihub://, API-Adresse
api://<id>, Bereich access_as_user mit Vorabberechtigung der App selbst,
Tokenversion 2). Sie standen nur als Text in einem Docstring und wurden auf
der VM von Hand eingetragen — neue Installationen hatten sie nie, und die
Anmeldeprüfung meldete trotzdem alles grün.
"""
import asyncio
import json

import pytest

import settings_store
import setup_wizard
from setup_wizard import _naa_patches

CID = "f8a0f52a-0000-0000-0000-000000000001"

# So sieht die von Hand eingerichtete Registrierung auf der VM aus (live
# gelesen 18.10.2026) — das Soll, das der Code nun selbst herstellt.
VOLLSTAENDIG = {
    "identifierUris": [f"api://{CID}"],
    "spa": {"redirectUris": ["brk-multihub://sig.example"]},
    "api": {"requestedAccessTokenVersion": 2, "knownClientApplications": [],
            "oauth2PermissionScopes": [{"id": "S1", "value": "access_as_user", "type": "User",
                                        "isEnabled": True}],
            "preAuthorizedApplications": [{"appId": CID, "delegatedPermissionIds": ["S1"]}]},
}


def _anwenden(app, patches):
    """Patches wie Graph anwenden: oberste Felder ersetzen (api ist Komplextyp)."""
    app = json.loads(json.dumps(app))
    for p in patches:
        app.update(json.loads(json.dumps(p)))
    return app


def test_vollstaendige_registrierung_braucht_nichts():
    assert _naa_patches(VOLLSTAENDIG, CID, "sig.example") == []


def test_leere_registrierung_wird_in_zwei_schritten_vollstaendig():
    patches = _naa_patches({}, CID, "sig.example")
    assert len(patches) == 2
    erster, zweiter = patches
    # Schritt 1 speichert den Bereich OHNE die Vorabberechtigung, die auf ihn zeigt.
    assert erster["api"]["preAuthorizedApplications"] == []
    bereich = erster["api"]["oauth2PermissionScopes"][0]
    assert bereich["value"] == "access_as_user" and bereich["isEnabled"] and bereich["type"] == "User"
    assert zweiter["api"]["preAuthorizedApplications"] == [
        {"appId": CID, "delegatedPermissionIds": [bereich["id"]]}]
    assert zweiter["api"]["oauth2PermissionScopes"] == erster["api"]["oauth2PermissionScopes"]
    fertig = _anwenden({}, patches)
    assert fertig["spa"]["redirectUris"] == ["brk-multihub://sig.example"]
    assert fertig["identifierUris"] == [f"api://{CID}"]
    assert fertig["api"]["requestedAccessTokenVersion"] == 2
    assert _naa_patches(fertig, CID, "sig.example") == []     # danach ruhig


def test_vorhandenes_bleibt_erhalten():
    app = {"identifierUris": ["api://firma.example/gw"],
           "spa": {"redirectUris": ["https://andere.example/spa"]},
           "api": {"requestedAccessTokenVersion": 2, "acceptMappedClaims": None,
                   "oauth2PermissionScopes": [{"id": "X", "value": "anderes", "isEnabled": True}],
                   "preAuthorizedApplications": [{"appId": "FREMD", "delegatedPermissionIds": ["X"]}]}}
    fertig = _anwenden(app, _naa_patches(app, CID, "sig.example"))
    assert "api://firma.example/gw" in fertig["identifierUris"]
    assert "https://andere.example/spa" in fertig["spa"]["redirectUris"]
    assert {b["value"] for b in fertig["api"]["oauth2PermissionScopes"]} == {"anderes", "access_as_user"}
    assert {"appId": "FREMD", "delegatedPermissionIds": ["X"]} in fertig["api"]["preAuthorizedApplications"]
    assert "acceptMappedClaims" in fertig["api"]                # Komplextyp vollständig zurück


def test_nur_vorabberechtigung_fehlt_ein_schritt():
    app = json.loads(json.dumps(VOLLSTAENDIG))
    app["api"]["preAuthorizedApplications"] = []
    patches = _naa_patches(app, CID, "sig.example")
    assert len(patches) == 1
    assert patches[0]["api"]["preAuthorizedApplications"] == [{"appId": CID, "delegatedPermissionIds": ["S1"]}]


@pytest.mark.parametrize("aenderung", [
    lambda a: a["api"].update(requestedAccessTokenVersion=None),
    lambda a: a["api"]["oauth2PermissionScopes"][0].update(isEnabled=False),
    lambda a: a.update(spa={"redirectUris": []}),
    lambda a: a.update(identifierUris=[]),
])
def test_jede_einzelne_luecke_wird_erkannt(aenderung):
    app = json.loads(json.dumps(VOLLSTAENDIG))
    aenderung(app)
    patches = _naa_patches(app, CID, "sig.example")
    assert patches
    assert _naa_patches(_anwenden(app, patches), CID, "sig.example") == []


def test_ursprung_mit_port(monkeypatch):
    import aussenadresse
    monkeypatch.setattr(aussenadresse, "konfiguriert", lambda: "https://gw.example:9443")
    assert setup_wizard.naa_ursprung("egal") == "gw.example:9443"
    fertig = _anwenden({}, _naa_patches({}, CID, setup_wizard.naa_ursprung()))
    assert fertig["spa"]["redirectUris"] == ["brk-multihub://gw.example:9443"]


# ── Setzer im Einrichtungs-Login ──────────────────────────────────────────────

def test_einrichtungs_login_traegt_naa_ein(monkeypatch):
    import aussenadresse
    daten = {"BOOTSTRAP_CLIENT_ID": CID, "ADDIN_BASE_URL": "https://sig.example"}
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: daten.get(k, *a))
    monkeypatch.setattr(settings_store, "update", lambda p: daten.update(p))
    monkeypatch.setattr(aussenadresse, "konfiguriert", lambda: "https://sig.example")
    app = {"id": "OBJ", "displayName": "EXO Signature Gateway Login",
           "publicClient": {"redirectUris": ["https://sig.example/auth/callback"]},
           "web": {"redirectUris": ["https://sig.example/auth/id-callback"],
                   "implicitGrantSettings": {"enableIdTokenIssuance": True}}}
    gesendet = []

    async def gh(methode, url, token, json=None):
        if methode == "get":
            return {"value": [app]}
        gesendet.append(json)
        return {}
    monkeypatch.setattr(setup_wizard, "_gh", gh)
    asyncio.run(setup_wizard.patch_bootstrap_redirect_uri("TOKEN", "sig.example"))
    fertig = _anwenden(app, gesendet)
    assert _naa_patches(fertig, CID, "sig.example") == []
    assert fertig["publicClient"] == app["publicClient"]        # Anmeldung unberührt


# ── Prüfung ───────────────────────────────────────────────────────────────────

def _pruefen(monkeypatch, app):
    import aussenadresse
    import graph_client
    import httpx
    from webui.routen import setup as setup_routen
    daten = {"BOOTSTRAP_CLIENT_ID": CID, "ADMIN_USERS": [{"upn": "a@x"}],
             "BOOTSTRAP_REDIRECT_URIS": ["https://sig.example/auth/callback"]}
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: daten.get(k, *a))
    monkeypatch.setattr(settings_store, "update", lambda p: daten.update(p))
    monkeypatch.setattr(aussenadresse, "konfiguriert", lambda: "https://sig.example")
    monkeypatch.setattr(graph_client, "_acquire_token", lambda: "T")
    voll = {"id": "OBJ", "displayName": "Login",
            "publicClient": {"redirectUris": ["https://sig.example/auth/callback"]}, **app}

    class _Antwort:
        def raise_for_status(self): pass
        def json(self): return {"value": [voll]}

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            assert "spa" in url and "api" in url and "identifierUris" in url
            return _Antwort()
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return json.loads(asyncio.run(setup_routen.api_verify_sso()).body)


def test_pruefung_meldet_fehlende_naa(monkeypatch):
    d = _pruefen(monkeypatch, {})
    naa = next(s for s in d["schritte"] if "NAA" in s["name"])
    assert not naa["ok"] and not d["ok"]
    assert "brk-multihub://sig.example" in naa["hinweis"]
    assert d["nachholen"] == "/api/addin/update-redirect-uri"


def test_pruefung_gruen_bei_vollstaendiger_naa(monkeypatch):
    d = _pruefen(monkeypatch, VOLLSTAENDIG)
    naa = next(s for s in d["schritte"] if "NAA" in s["name"])
    assert naa["ok"] and d["ok"] and "nachholen" not in d


# ── Abnahme ───────────────────────────────────────────────────────────────────

def _abnahme(monkeypatch, app, an=True):
    import abnahme
    import aussenadresse
    import graph_client
    import httpx
    daten = {"BOOTSTRAP_CLIENT_ID": CID, "ADDIN_ENABLED": an, "SELF_SERVICE_ENABLED": False}
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: daten.get(k, *a))
    monkeypatch.setattr(aussenadresse, "konfiguriert", lambda: "https://sig.example")
    monkeypatch.setattr(graph_client, "_acquire_token", lambda: "T")

    class _A:
        def raise_for_status(self): pass
        def json(self): return {"value": [app]}
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _A())
    return abnahme._naa()


def test_abnahme_zeigt_fehlende_naa(monkeypatch):
    import abnahme
    p = _abnahme(monkeypatch, {})
    assert p["zustand"] == abnahme.OFFEN and "brk-multihub://sig.example" in p["befund"] and p["zu_tun"]


def test_abnahme_ok_bei_vollstaendiger_naa(monkeypatch):
    import abnahme
    assert _abnahme(monkeypatch, VOLLSTAENDIG)["zustand"] == abnahme.OK


def test_abnahme_entfaellt_ohne_addin_und_self_service(monkeypatch):
    import abnahme
    assert _abnahme(monkeypatch, {}, an=False)["zustand"] == abnahme.ENTFAELLT


def test_abnahme_fuehrt_den_punkt():
    import abnahme
    assert abnahme._naa in abnahme.PRUEFUNGEN
