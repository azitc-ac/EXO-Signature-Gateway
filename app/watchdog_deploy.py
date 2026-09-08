"""Bypass-Wächter: Azure-Function per ARM-REST anlegen (KV-Stil, ohne az/func).

Modelliert nach `keyvault.py`: reine HTTPS-Aufrufe an die Azure-REST-APIs mit einem
**delegierten ARM-Token** (aus dem Admin-Login, `flow=arm`) — kein `az`/`func` im
Container nötig. Legt Storage + Consumption-Function-App (System-MI, PowerShell)
an, lädt den Function-Code per Kudu-ZipDeploy hoch und setzt die App-Einstellungen.
Die drei EXO/Graph-Berechtigungen der MI kommen danach über den bestehenden
Wizard-Weg (grant_watchdog_graph_roles + grant_watchdog_role.ps1).

Subscription/Resource-Group-Auflistung + Token: wiederverwendet aus `keyvault`.
"""
from __future__ import annotations

import io
import json
import logging
import zipfile
from pathlib import Path

import httpx

log = logging.getLogger("watchdog_deploy")

_ARM = "https://management.azure.com"
# Quelle des Function-Codes im Container (per Dockerfile mitgeliefert).
_FUNC_SRC = Path("/app/watchdog_function")


async def _arm(method: str, url: str, token: str, body: dict | None = None,
               timeout: float = 60) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout) as c:
        return await c.request(
            method, url,
            json=body if body is not None else None,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )


def _err(resp: httpx.Response) -> str:
    try:
        return resp.json().get("error", {}).get("message", resp.text[:400])
    except Exception:
        return resp.text[:400]


async def ensure_resource_group(sub: str, rg: str, location: str, token: str) -> tuple[bool, str]:
    url = f"{_ARM}/subscriptions/{sub}/resourceGroups/{rg}?api-version=2022-12-01"
    resp = await _arm("put", url, token, {"location": location})
    if resp.status_code not in (200, 201):
        return False, f"RG (HTTP {resp.status_code}): {_err(resp)}"
    return True, "ok"


async def _poll_provisioning(url: str, token: str, tries: int = 40, delay: float = 5) -> tuple[bool, str]:
    """GET-Poll auf provisioningState == Succeeded (asyncio.sleep zwischen Versuchen)."""
    import asyncio
    for _ in range(tries):
        resp = await _arm("get", url, token, timeout=30)
        if resp.status_code == 200:
            st = (resp.json().get("properties", {}) or {}).get("provisioningState", "")
            if st == "Succeeded":
                return True, "Succeeded"
            if st in ("Failed", "Canceled"):
                return False, st
        await asyncio.sleep(delay)
    return False, "Timeout"


async def ensure_providers_registered(sub: str, token: str,
                                      namespaces: tuple[str, ...] = ("Microsoft.Storage", "Microsoft.Web"),
                                      tries: int = 40, delay: float = 5) -> tuple[bool, str]:
    """Ressourcenanbieter je Subscription registrieren, falls nötig.

    Frische Subscriptions haben Microsoft.Storage/Microsoft.Web nicht registriert
    (ARM 409 „not registered to use namespace"). Registrierung ist einmalig pro
    Abo und asynchron — GET-Poll auf registrationState == 'Registered'."""
    import asyncio
    for ns in namespaces:
        base = f"{_ARM}/subscriptions/{sub}/providers/{ns}?api-version=2021-04-01"
        resp = await _arm("get", base, token, timeout=30)
        state = (resp.json().get("registrationState", "") if resp.status_code == 200 else "")
        if state == "Registered":
            continue
        reg = await _arm("post", f"{_ARM}/subscriptions/{sub}/providers/{ns}/register"
                         "?api-version=2021-04-01", token, timeout=30)
        if reg.status_code not in (200, 202):
            return False, f"{ns} registrieren (HTTP {reg.status_code}): {_err(reg)}"
        for _ in range(tries):
            r = await _arm("get", base, token, timeout=30)
            if r.status_code == 200 and r.json().get("registrationState") == "Registered":
                break
            await asyncio.sleep(delay)
        else:
            return False, f"{ns}: Registrierung nicht rechtzeitig abgeschlossen"
    return True, "ok"


async def create_storage_account(sub: str, rg: str, name: str, location: str,
                                 token: str) -> tuple[bool, str, str]:
    """Storage-Konto anlegen (Consumption-Function braucht eins) + Connection-String
    zurückgeben. name: 3–24 Kleinbuchstaben/Ziffern, global eindeutig."""
    base = (f"{_ARM}/subscriptions/{sub}/resourceGroups/{rg}"
            f"/providers/Microsoft.Storage/storageAccounts/{name}")
    put = base + "?api-version=2023-01-01"
    body = {
        "location": location,
        "sku": {"name": "Standard_LRS"},
        "kind": "StorageV2",
        "properties": {"minimumTlsVersion": "TLS1_2", "allowBlobPublicAccess": False,
                       "supportsHttpsTrafficOnly": True},
    }
    # Direkt nach der RG-Anlage kann der PUT kurz mit 400/404 scheitern
    # (Eventual Consistency) — ein paar Mal mit Pause wiederholen.
    import asyncio
    resp = None
    for versuch in range(4):
        resp = await _arm("put", put, token, body)
        if resp.status_code in (200, 202):
            break
        if resp.status_code in (400, 404, 409) and versuch < 3:
            await asyncio.sleep(6)
            continue
        break
    if resp is None or resp.status_code not in (200, 202):
        return False, f"Storage (HTTP {resp.status_code if resp else '?'}): {_err(resp) if resp else ''}", ""
    ok, st = await _poll_provisioning(base + "?api-version=2023-01-01", token)
    if not ok:
        return False, f"Storage-Provisioning: {st}", ""
    keys = await _arm("post", base + "/listKeys?api-version=2023-01-01", token)
    if keys.status_code != 200:
        return False, f"Storage-Keys (HTTP {keys.status_code}): {_err(keys)}", ""
    key = keys.json()["keys"][0]["value"]
    conn = (f"DefaultEndpointsProtocol=https;AccountName={name};AccountKey={key};"
            "EndpointSuffix=core.windows.net")
    return True, "ok", conn


async def create_function_app(sub: str, rg: str, app: str, location: str,
                             storage_conn: str, ps_version: str, token: str) -> tuple[bool, str, dict]:
    """Consumption-Serverfarm (Y1) + Function-App (Windows, PowerShell, System-MI).
    Gibt {principalId, defaultHostName} zurück (appId separat über Graph auflösen)."""
    # 1) Consumption-Plan (Y1 Dynamic)
    plan = f"{app}-plan"
    plan_url = (f"{_ARM}/subscriptions/{sub}/resourceGroups/{rg}"
                f"/providers/Microsoft.Web/serverfarms/{plan}?api-version=2023-12-01")
    resp = await _arm("put", plan_url, token, {
        "location": location,
        "sku": {"name": "Y1", "tier": "Dynamic"},
        "properties": {"computeMode": "Dynamic", "reserved": False},
    })
    if resp.status_code not in (200, 201, 202):
        roh = _err(resp)
        if "quota" in roh.lower() or "Y1 VMs" in roh:
            # Eingeschränkte Abos (Sponsorship/Free) haben oft 0 Y1-Kontingent.
            return False, ("Kein Y1-Kontingent (Consumption/serverlos) in dieser Region/diesem Abo. "
                           "Optionen: andere Region wählen · Y1-Kontingent anfordern "
                           "(Portal → Nutzung + Kontingente, bei Sponsorship per Support-Ticket) · "
                           "oder die cron-Variante nutzen (braucht kein Azure-Kontingent). "
                           f"[ARM: {roh[:200]}]"), {}
        return False, f"Plan (HTTP {resp.status_code}): {roh}", {}

    # 2) Function-App (site)
    share = app.lower()[:60]
    site_url = (f"{_ARM}/subscriptions/{sub}/resourceGroups/{rg}"
                f"/providers/Microsoft.Web/sites/{app}?api-version=2023-12-01")
    app_settings = [
        {"name": "AzureWebJobsStorage", "value": storage_conn},
        {"name": "WEBSITE_CONTENTAZUREFILECONNECTIONSTRING", "value": storage_conn},
        {"name": "WEBSITE_CONTENTSHARE", "value": share},
        {"name": "FUNCTIONS_EXTENSION_VERSION", "value": "~4"},
        {"name": "FUNCTIONS_WORKER_RUNTIME", "value": "powershell"},
        {"name": "WEBSITE_RUN_FROM_PACKAGE", "value": "1"},
    ]
    body = {
        "location": location,
        "kind": "functionapp",
        "identity": {"type": "SystemAssigned"},
        "properties": {
            "serverFarmId": (f"/subscriptions/{sub}/resourceGroups/{rg}"
                             f"/providers/Microsoft.Web/serverfarms/{plan}"),
            "httpsOnly": True,
            "siteConfig": {
                "powerShellVersion": ps_version,
                "minTlsVersion": "1.2",
                "appSettings": app_settings,
            },
        },
    }
    resp = await _arm("put", site_url, token, body, timeout=120)
    if resp.status_code not in (200, 201, 202):
        return False, f"Function-App (HTTP {resp.status_code}): {_err(resp)}", {}
    # Web-Sites melden KEIN provisioningState, sondern properties.state ("Running").
    # Auf Running + vorhandene MI-Objekt-ID warten.
    import asyncio
    state, principal_id, host = "", "", ""
    for _ in range(30):
        site = (await _arm("get", site_url, token)).json()
        state = (site.get("properties", {}) or {}).get("state", "")
        principal_id = (site.get("identity", {}) or {}).get("principalId", "")
        host = (site.get("properties", {}) or {}).get("defaultHostName", "")
        if state == "Running" and principal_id:
            return True, "ok", {"principalId": principal_id, "defaultHostName": host}
        await asyncio.sleep(5)
    return False, f"Function-App nicht bereit (state={state}, principalId={'ja' if principal_id else 'nein'})", {}


def build_function_zip() -> bytes:
    """Baut das Function-Paket aus dem mitgelieferten Quellcode (host.json,
    requirements.psd1, Watchdog/*). In-Memory, kein func/az nötig."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in ("host.json", "requirements.psd1",
                    "Watchdog/function.json", "Watchdog/run.ps1"):
            p = _FUNC_SRC / rel
            z.writestr(rel, p.read_bytes())
    return buf.getvalue()


async def deploy_code(sub: str, rg: str, app: str, token: str,
                     zip_bytes: bytes | None = None) -> tuple[bool, str]:
    """Function-Code per Kudu-ZipDeploy hochladen (kein func/az). Publishing-Creds
    per ARM holen, dann Zip an den SCM-Endpunkt posten (Basic-Auth)."""
    zip_bytes = zip_bytes if zip_bytes is not None else build_function_zip()
    creds_url = (f"{_ARM}/subscriptions/{sub}/resourceGroups/{rg}"
                 f"/providers/Microsoft.Web/sites/{app}/config/publishingcredentials/list"
                 "?api-version=2023-12-01")
    resp = await _arm("post", creds_url, token, timeout=30)
    if resp.status_code not in (200, 201):
        return False, f"Publishing-Creds (HTTP {resp.status_code}): {_err(resp)}"
    props = resp.json().get("properties", {})
    scm = props.get("scmUri", "")           # enthält user:pass@host
    user = props.get("publishingUserName", "")
    pw = props.get("publishingPassword", "")
    scm_host = f"{app}.scm.azurewebsites.net"
    try:
        async with httpx.AsyncClient(timeout=180) as c:
            r = await c.post(
                f"https://{scm_host}/api/zipdeploy",
                content=zip_bytes,
                auth=(user, pw),
                headers={"Content-Type": "application/zip"},
            )
        if r.status_code not in (200, 202):
            return False, f"ZipDeploy (HTTP {r.status_code}): {r.text[:300]}"
    except Exception as exc:                                # noqa: BLE001
        return False, f"ZipDeploy-Fehler: {exc}"
    return True, "ok"


async def delete_resources(sub: str, rg: str, app: str, storage: str, plan: str,
                           created_rg: bool, token: str) -> tuple[bool, str]:
    """Rückbau — löscht GENAU das, was der Installer angelegt hat.

    ⚠️ Hat der Installer die Ressourcengruppe SELBST angelegt (`created_rg`), wird
    sie ganz gelöscht (nimmt Storage/Plan/Site in einem Rutsch mit). Lag eine
    BESTEHENDE RG vor, werden NUR die drei erzeugten Ressourcen einzeln entfernt —
    niemals die fremde RG. Reihenfolge: Site vor Plan (der Plan lässt sich nicht
    löschen, solange die Site ihn nutzt), dann Storage. 404 gilt als Erfolg
    (schon weg). Löschungen laufen teils asynchron (202) — das genügt uns."""
    base = f"{_ARM}/subscriptions/{sub}/resourceGroups/{rg}"
    ok_codes = (200, 202, 204, 404)
    if created_rg:
        resp = await _arm("delete", f"{base}?api-version=2022-12-01", token, timeout=60)
        if resp.status_code not in ok_codes:
            return False, f"RG löschen (HTTP {resp.status_code}): {_err(resp)}"
        return True, "ok"
    fehler = []
    ziele = [
        ("Function", f"{base}/providers/Microsoft.Web/sites/{app}?api-version=2023-12-01"),
        ("Plan", f"{base}/providers/Microsoft.Web/serverfarms/{plan}?api-version=2023-12-01"),
        ("Storage", f"{base}/providers/Microsoft.Storage/storageAccounts/{storage}?api-version=2023-01-01"),
    ]
    for label, url in ziele:
        resp = await _arm("delete", url, token, timeout=60)
        if resp.status_code not in ok_codes:
            fehler.append(f"{label} HTTP {resp.status_code}")
    return (not fehler), ("ok" if not fehler else "; ".join(fehler))


async def set_app_settings(sub: str, rg: str, app: str, settings: dict, token: str) -> tuple[bool, str]:
    """App-Einstellungen mischen (GET bestehende, aktualisieren, PUT)."""
    base = (f"{_ARM}/subscriptions/{sub}/resourceGroups/{rg}"
            f"/providers/Microsoft.Web/sites/{app}/config/appsettings")
    cur = await _arm("post", base + "/list?api-version=2023-12-01", token)
    if cur.status_code != 200:
        return False, f"AppSettings lesen (HTTP {cur.status_code}): {_err(cur)}"
    props = cur.json().get("properties", {}) or {}
    props.update(settings)
    resp = await _arm("put", base + "?api-version=2023-12-01", token, {"properties": props})
    if resp.status_code not in (200, 201):
        return False, f"AppSettings schreiben (HTTP {resp.status_code}): {_err(resp)}"
    return True, "ok"
