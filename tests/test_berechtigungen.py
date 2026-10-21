"""Anwendungsberechtigungen: Soll (setup_wizard-Specs) gegen Ist (Rollen im Token).

Der Punkt: „beantragt" ist nicht „erteilt". Der Status muss aus dem TOKEN kommen,
und das Beheben darf NUR Rollen erteilen — kein neues Secret, kein neues Zertifikat
(der volle Setup-Lauf erneuert beides bei jedem Aufruf).
"""
from __future__ import annotations

import asyncio
import base64
import json

import berechtigungen
import settings_store
import setup_wizard


def _jwt(roles):
    teil = base64.urlsafe_b64encode(json.dumps({"roles": roles}).encode()).decode().rstrip("=")
    return f"x.{teil}.y"


def test_status_aus_dem_token(monkeypatch):
    monkeypatch.setattr(berechtigungen, "graph_rollen", lambda: {"Mail.Send", "User.Read.All"})
    monkeypatch.setattr(berechtigungen, "exo_rollen", lambda: {"Exchange.ManageAsApp"})
    st = {b["name"]: b["erteilt"] for b in berechtigungen.status()}
    assert st["Mail.Send"] is True
    assert st["MailboxSettings.ReadWrite"] is False
    assert st["Exchange.ManageAsApp"] is True and st["IMAP.AccessAsApp"] is False
    alle = {s["name"] for s in setup_wizard._GRAPH_PERMISSION_SPECS + setup_wizard._EXO_PERMISSION_SPECS}
    assert set(st) == alle, "Status muss genau die beantragten Rollen zeigen"


def test_ohne_token_ist_nichts_bekannt_statt_fehlt(monkeypatch):
    monkeypatch.setattr(berechtigungen, "graph_rollen", lambda: None)
    monkeypatch.setattr(berechtigungen, "exo_rollen", lambda: None)
    assert {b["erteilt"] for b in berechtigungen.status()} == {None}


def test_nur_filtert(monkeypatch):
    monkeypatch.setattr(berechtigungen, "graph_rollen", lambda: set())
    monkeypatch.setattr(berechtigungen, "exo_rollen", lambda: set())
    namen = [b["name"] for b in berechtigungen.status(["Calendars.Read", "MailboxSettings.ReadWrite"])]
    assert namen == ["MailboxSettings.ReadWrite", "Calendars.Read"]


def test_token_rollen_liest_jwt():
    assert berechtigungen.token_rollen(_jwt(["A", "B"])) == {"A", "B"}
    assert berechtigungen.token_rollen("kein-jwt") is None


def test_nachziehen_erteilt_nur_rollen(monkeypatch):
    aufrufe = []

    async def gh(methode, url, token, json=None):
        aufrufe.append((methode, url.split("?")[0].rsplit("/", 2)[-2:], json))
        if methode == "get" and "/applications" in url:
            return {"value": [{"id": "obj1"}]}
        if methode == "get" and "/servicePrincipals" in url and "appId eq" in url:
            return {"value": [{"id": "sp1"}]}
        if methode == "get":
            return {"value": [{"id": "res"}]}
        if methode == "post" and "appRoleAssignments" in url and json["appRoleId"] == setup_wizard._graph_permissions()[0]["id"]:
            raise RuntimeError("Permission being assigned already exists on the object")
        return {}
    monkeypatch.setattr(setup_wizard, "_gh", gh)
    monkeypatch.setattr(setup_wizard._gc, "reset_msal_app", lambda: None)
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: {"CLIENT_ID": "cid"}.get(k, d))
    res = asyncio.run(setup_wizard.berechtigungen_nachziehen("tok"))
    assert res == {"ok": True, "fehler": []}, "bereits erteilt ist kein Fehler"
    urls = " ".join("/".join(u) for _, u, _ in aufrufe)
    assert "addPassword" not in urls, "kein neues Secret"
    assert "keyCredentials" not in json.dumps([j for _, _, j in aufrufe if j]), "kein neues Zertifikat"
    erteilt = [j["appRoleId"] for m, _, j in aufrufe if m == "post"]
    soll = [p["id"] for p in setup_wizard._graph_permissions() + setup_wizard._EXO_PERMISSIONS]
    assert erteilt == soll


def test_nachziehen_meldet_echte_fehler(monkeypatch):
    async def gh(methode, url, token, json=None):
        if methode == "get":
            return {"value": [{"id": "x"}]}
        if methode == "post":
            raise RuntimeError("Authorization_RequestDenied")
        return {}
    monkeypatch.setattr(setup_wizard, "_gh", gh)
    monkeypatch.setattr(setup_wizard._gc, "reset_msal_app", lambda: None)
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: {"CLIENT_ID": "cid"}.get(k, d))
    res = asyncio.run(setup_wizard.berechtigungen_nachziehen("tok"))
    assert res["ok"] is False and res["fehler"]


def test_doku_nennt_jede_beantragte_rolle():
    """Die Tabelle in der Doku (bis v1.10.2 im README, jetzt docs/installation.md) driftete: MailboxSettings.ReadWrite und Calendars.Read
    fehlten, und dort stand „Mail.ReadWrite.All“ — eine Rolle, die Graph nicht kennt."""
    import re
    from pathlib import Path
    readme = (Path(__file__).resolve().parent.parent / "docs" / "installation.md").read_text(encoding="utf-8")
    zeilen = set(re.findall(r"^\| `([A-Za-z.]+)` \|", readme, re.M))
    soll = {s["name"] for s in setup_wizard._GRAPH_PERMISSION_SPECS + setup_wizard._EXO_PERMISSION_SPECS}
    assert soll <= zeilen, f"in docs/installation.md fehlt: {sorted(soll - zeilen)}"
    assert "Mail.ReadWrite.All" not in zeilen


# ── „Erneut prüfen" muss eine eben erteilte Rolle sehen ──────────────────
# MSAL liefert Client-Credentials-Token bis zum Ablauf aus dem Cache und
# verweigert force_refresh. Die Prüfung holt deshalb ein frisches Token.

def test_graph_rollen_aus_frischem_token_nicht_aus_cache(monkeypatch):
    import graph_client
    monkeypatch.setattr(graph_client, "frisches_app_token", lambda *a: _jwt(["Mail.Send", "AppCatalog.Read.All"]))
    monkeypatch.setattr(graph_client, "_acquire_token", lambda: _jwt(["Mail.Send"]))
    monkeypatch.setattr(berechtigungen, "_rollen_im_cache", lambda: {"Mail.Send"})
    monkeypatch.setattr(graph_client, "reset_msal_app", lambda: None)
    assert "AppCatalog.Read.All" in berechtigungen.graph_rollen()


def test_neue_rolle_verwirft_betriebs_cache(monkeypatch):
    """Sieht die Prüfung mehr als das Betriebstoken, wird dessen Cache verworfen —
    sonst bekäme z. B. der Teams-Katalog bis zum Tokenablauf weiter 403."""
    import graph_client
    aufrufe = []
    monkeypatch.setattr(graph_client, "reset_msal_app", lambda: aufrufe.append(1))
    monkeypatch.setattr(graph_client, "frisches_app_token", lambda *a: _jwt(["A", "B"]))
    monkeypatch.setattr(berechtigungen, "_rollen_im_cache", lambda: {"A"})
    berechtigungen.graph_rollen()
    assert aufrufe == [1]
    aufrufe.clear()
    monkeypatch.setattr(berechtigungen, "_rollen_im_cache", lambda: {"A", "B"})
    berechtigungen.graph_rollen()
    assert aufrufe == []


def test_exo_rollen_aus_frischem_token(monkeypatch):
    import graph_client
    scopes = []
    monkeypatch.setattr(graph_client, "frisches_app_token",
                        lambda scope=None: scopes.append(scope) or _jwt(["Exchange.ManageAsApp"]))
    monkeypatch.setattr(graph_client, "_get_msal_app", lambda: None)
    assert berechtigungen.exo_rollen() == {"Exchange.ManageAsApp"}
    assert scopes == ["https://outlook.office365.com/.default"]


def test_frisches_app_token_hat_eigenen_leeren_cache(monkeypatch):
    import graph_client
    import msal
    erzeugt = []

    class FakeApp:
        def __init__(self, cid, **kw):
            erzeugt.append(kw)

        def acquire_token_for_client(self, scopes):
            return {"access_token": "t-" + scopes[0]}

    monkeypatch.setattr(graph_client, "_get_effective_credentials", lambda: ("tid", "cid", "geheim"))
    monkeypatch.setattr(msal, "ConfidentialClientApplication", FakeApp)
    assert graph_client.frisches_app_token() == "t-https://graph.microsoft.com/.default"
    graph_client.frisches_app_token()
    assert len(erzeugt) == 2
    caches = [kw.get("token_cache") for kw in erzeugt]
    assert all(isinstance(c, msal.TokenCache) for c in caches)
    assert caches[0] is not caches[1]


def test_frisches_app_token_ohne_zugangsdaten(monkeypatch):
    import graph_client
    monkeypatch.setattr(graph_client, "_get_effective_credentials", lambda: ("tid", "", ""))
    assert graph_client.frisches_app_token() is None


def test_erneut_pruefen_meldet_sich_am_ort():
    """Der Knopf zeichnete dieselbe Tabelle neu und wirkte wie tot. Jetzt: Knopf
    gesperrt (knopfAktion) + „Wird geprüft…", danach „Geprüft um …". Der Klick darf das Ereignis
    nicht als Hinweis durchreichen, und nach dem Erteilen darf die Prüfung die
    Erfolgsmeldung nicht sofort löschen."""
    from pathlib import Path
    t = (Path(__file__).parent.parent / "app/webui/templates/_berechtigungen.html").read_text()
    assert "melde('offen', 'Wird geprüft…')" in t
    assert "'Geprüft um ' + new Date()" in t
    assert "await knopfAktion(pruefKnopf," in t      # gesperrt + Pfeil, Freigabe auch bei Ausnahme
    assert "addEventListener('click', pruefen)" not in t
    assert "addEventListener('click', function () { pruefen(); })" in t
    assert "pruefen(['fertig'," in t
    assert "ev.data.type === 'anmeldung-abgelaufen' && wartet" in t
