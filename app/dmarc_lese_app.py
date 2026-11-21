"""Eigene Lese-App für DMARC-Berichte aus einer Microsoft-365-Gruppe.

Warum es sie gibt: Ist die Postfach-Eingrenzung der Gateway-App an
(APP_ACCESS_POLICY_ENABLED → ApplicationAccessPolicy RestrictAccess), sperrt
Exchange jedes Ziel außerhalb der Gruppe „… - App Scope“. Eine M365-Gruppe lässt
sich dort nicht aufnehmen (GroupMailbox ist kein gültiger PolicyScopeGroupId-
Empfänger, New-ApplicationAccessPolicy). Die Policy hängt an der App-ID — eine
zweite App, die NUR Group-Conversation.Read.All hat, ist von ihr nicht erfasst.
Die Haupt-App bleibt eingegrenzt.

Lebenszyklus — EIN Weg: `abgleichen()` läuft bei „Fehlende Berechtigungen
erteilen“ (setup_wizard.berechtigungen_nachziehen) und legt die App an, wenn sie
gebraucht wird, oder löscht sie, wenn nicht mehr. Nachweis ist das Zertifikat
aus auth.pfx (kein eigenes Geheimnis); beim Erneuern lädt es
setup_wizard._zert_an_alle_apps auch hierher hoch. Prüfung: Zeile
„DMARC-Lese-App“ in berechtigungen.status (Rolle im FRISCHEN Token dieser App).
"""
from __future__ import annotations

import logging

import msal

import config
import settings_store

log = logging.getLogger(__name__)

NAME = "DMARC-Lese-App"            # Zeile in berechtigungen.status
KEY = "_DMARC_LESE_APP"            # {client_id, objekt_id}
ROLLE = "Group-Conversation.Read.All"
ROLLE_ID = "4f0a8235-6f6f-4ec7-9500-34b452a4a0c3"
_GRAPH = "https://graph.microsoft.com/v1.0"
_SCOPES = ["https://graph.microsoft.com/.default"]

_msal_cache: dict = {}
_fehler: dict = {"text": ""}


def letzter_fehler() -> str:
    """Grund des letzten gescheiterten Token-Abrufs (AADSTS…), leer nach Erfolg.
    Ohne ihn hieß jede Ursache „Entra braucht einige Minuten“ — auch eine in
    Entra gelöschte App (AADSTS700016) oder ein fehlendes Zertifikat (AADSTS700027)."""
    return _fehler["text"]


def anzeigename() -> str:
    return f"{settings_store.get('GATEWAY_NAME') or 'EXO Signature Gateway'} - DMARC-Leser"


def gespeichert() -> dict:
    d = settings_store.get(KEY) or {}
    return d if isinstance(d, dict) and d.get("client_id") else {}


def noetig() -> bool:
    """Gruppe als DMARC-Ziel UND Postfach-Eingrenzung an."""
    z = settings_store.get("_DMARC_ZIEL") or {}
    return bool((settings_store.get("DMARC_POSTFACH") or "").strip()
                and isinstance(z, dict) and z.get("art") == "gruppe"
                and settings_store.get("APP_ACCESS_POLICY_ENABLED") is True)


def zu_verwenden() -> bool:
    """Liest der Gruppenabruf über die Lese-App (statt über die Haupt-App)?"""
    return noetig() and bool(gespeichert())


def _msal_app():
    import graph_client
    cid = gespeichert().get("client_id", "")
    tenant = config.TENANT_ID or settings_store.get("TENANT_ID") or ""
    zert = graph_client.zertifikat_nachweis()
    if not (cid and tenant and zert):
        return None
    schluessel = (cid, tenant, graph_client._pfx_stand())
    app = _msal_cache.get(schluessel)
    if app is None:
        _msal_cache.clear()
        app = graph_client._msal_app(cid, tenant, zert)
        _msal_cache[schluessel] = app
    return app


def token() -> str | None:
    """Anwendungstoken der Lese-App, None ohne App/Zertifikat oder bei Fehler
    (eine frisch angelegte App nimmt das Zertifikat erst nach Minuten an)."""
    app = _msal_app()
    if app is None:
        return None
    try:
        r = app.acquire_token_silent(_SCOPES, account=None) or app.acquire_token_for_client(scopes=_SCOPES)
    except Exception as exc:                                    # noqa: BLE001
        _fehler["text"] = str(exc)[:200]
        log.warning("DMARC-Lese-App: Token fehlgeschlagen: %s", exc)
        return None
    tok = r.get("access_token") if isinstance(r, dict) else None
    if tok:
        _fehler["text"] = ""
    else:
        _fehler["text"] = (str((r or {}).get("error_description") or (r or {}).get("error") or "")
                           .split("\n")[0][:200])
        log.warning("DMARC-Lese-App: kein Token: %s", _fehler["text"])
    return tok


def erteilt() -> bool | None:
    """Steht die Rolle im FRISCHEN Token der Lese-App? False ohne App, None,
    wenn sich kein Token beschaffen ließ (Verteilung, Zertifikat)."""
    if not gespeichert():
        return False
    import berechtigungen
    import graph_client
    cid = gespeichert()["client_id"]
    tenant = config.TENANT_ID or settings_store.get("TENANT_ID") or ""
    zert = graph_client.zertifikat_nachweis()
    if not (tenant and zert):
        return None
    try:
        r = graph_client._msal_app(cid, tenant, zert, msal.TokenCache()) \
            .acquire_token_for_client(scopes=_SCOPES)
    except Exception:                                           # noqa: BLE001
        return None
    tok = r.get("access_token") if isinstance(r, dict) else None
    rollen = berechtigungen.token_rollen(tok) if tok else None
    return None if rollen is None else ROLLE in rollen


def status_zeile() -> dict | None:
    """Zeile für berechtigungen.status — nur, wenn die Lese-App gebraucht wird
    oder noch existiert (dann zum Aufräumen: „nicht benötigt“)."""
    if not (noetig() or gespeichert()):
        return None
    return {"name": NAME, "api": "Graph",
            "zweck": (f"Eigene App nur mit {ROLLE}: DMARC-Berichte aus der Microsoft-365-Gruppe "
                      "lesen — die Postfach-Eingrenzung lässt Gruppen nicht zu"),
            "erteilt": erteilt(), "benoetigt": noetig()}


async def abgleichen(admin_token: str) -> list[str]:
    """Soll herstellen: anlegen (App + Zertifikat + Dienstprinzipal + Rolle), wenn
    nötig; löschen, wenn nicht mehr nötig. Idempotent. Liefert Fehlertexte."""
    import setup_wizard as sw
    gh = sw._gh
    fehler: list[str] = []
    d = gespeichert()
    if not noetig():
        if d:
            try:
                await gh("delete", f"{_GRAPH}/applications/{d['objekt_id']}", admin_token)
                log.info("DMARC-Lese-App %s gelöscht (nicht mehr benötigt)", d["client_id"][:8])
            except Exception as exc:                            # noqa: BLE001
                if "404" not in str(exc):
                    return [f"DMARC-Lese-App nicht gelöscht: {exc}"]
            settings_store.force_update({KEY: {}})
            _msal_cache.clear()
        return fehler
    cert_der = sw._auth_zert_der()
    if not cert_der:
        return ["DMARC-Lese-App: kein Auth-Zertifikat (auth.pfx) vorhanden."]
    try:
        obj, cid = d.get("objekt_id", ""), d.get("client_id", "")
        if obj:
            vorhanden = await gh("get", f"{_GRAPH}/applications?$filter=appId eq '{cid}'&$select=id", admin_token)
            if not vorhanden.get("value"):
                obj = cid = ""
        if not obj:
            name = anzeigename().replace("'", "''")
            treffer = (await gh("get", f"{_GRAPH}/applications?$filter=displayName eq '{name}'"
                                       "&$select=id,appId", admin_token)).get("value") or []
            if treffer:
                obj, cid = treffer[0]["id"], treffer[0]["appId"]
            else:
                app = await gh("post", f"{_GRAPH}/applications", admin_token, json={
                    "displayName": anzeigename(), "signInAudience": "AzureADMyOrg",
                    "requiredResourceAccess": [{"resourceAppId": sw._GRAPH_APP_ID,
                                                "resourceAccess": [{"id": ROLLE_ID, "type": "Role"}]}],
                })
                obj, cid = app["id"], app["appId"]
                log.info("DMARC-Lese-App %s angelegt", cid[:8])
        await sw._upload_key_credential(admin_token, obj, cert_der)
        sps = (await gh("get", f"{_GRAPH}/servicePrincipals?$filter=appId eq '{cid}'&$select=id",
                        admin_token)).get("value") or []
        sp = sps[0]["id"] if sps else (await gh("post", f"{_GRAPH}/servicePrincipals", admin_token,
                                                json={"appId": cid}))["id"]
        graph_sp = await sw._get_sp_id_for_resource(admin_token, sw._GRAPH_APP_ID)
        try:
            await gh("post", f"{_GRAPH}/servicePrincipals/{sp}/appRoleAssignments", admin_token,
                     json={"principalId": sp, "resourceId": graph_sp, "appRoleId": ROLLE_ID})
        except Exception as exc:                                # noqa: BLE001
            if not sw._schon_erteilt(exc):
                fehler.append(f"DMARC-Lese-App: Rolle nicht erteilt: {exc}")
        settings_store.force_update({KEY: {"client_id": cid, "objekt_id": obj}})
        _msal_cache.clear()
    except Exception as exc:                                    # noqa: BLE001
        fehler.append(f"DMARC-Lese-App nicht eingerichtet: {exc}")
    for f in fehler:
        log.warning("%s", f)
    return fehler
