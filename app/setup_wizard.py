"""
Setup wizard logic — Graph API calls executed after PKCE bootstrap auth.

All functions accept an access_token obtained via the PKCE flow with the
Microsoft Graph CLI App.  The token has delegated permissions that allow
creating app registrations and granting admin consent.
"""

import asyncio
import base64
import logging
import subprocess
import tempfile
from pathlib import Path

import httpx

import config
import settings_store
import graph_client as _gc

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"

# ── Well-known resource app IDs ───────────────────────────────────────────────
_GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"  # Microsoft Graph
_EXO_APP_ID = "00000002-0000-0ff1-ce00-000000000000"    # Exchange Online

# ── Permission IDs ────────────────────────────────────────────────────────────
_GRAPH_PERMISSIONS = [
    # User.Read.All
    {"id": "df021288-bdef-4463-88db-98f22de89214", "type": "Role"},
    # Mail.ReadWrite — needed for sent-item patching
    {"id": "e2a3a72e-5f79-4c64-b1b1-878b674786c9", "type": "Role"},
    # Mail.Send — needed for Graph API re-inject (Azure non-Enterprise mode)
    {"id": "b633e1c5-b582-4048-a93e-9f11b44c7e96", "type": "Role"},
]
_EXO_PERMISSIONS = [
    # Exchange.ManageAsApp
    {"id": "dc50a0fb-09a3-484d-be87-e023b12c6440", "type": "Role"},
    # IMAP.AccessAsApp — needed for IMAP APPEND (smtp587 / Azure mode)
    {"id": "5e5addcd-3e8d-4e90-baf5-964efab2b20a", "type": "Role"},
]

# Exchange Administrator built-in role ID (constant across all tenants)
_EXCHANGE_ADMIN_ROLE_ID = "29232cdf-9323-42fd-ade2-1d097af3e4de"

# Path where the auth certificate PFX (with private key) is stored
_AUTH_CERT_PATH = Path("/app/data/auth.pfx")


async def _gh(method: str, url: str, token: str, **kwargs) -> dict:
    """Helper: perform a Graph API call, raise on HTTP error, return JSON."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await getattr(client, method)(url, headers=headers, **kwargs)
    if not resp.is_success:
        raise RuntimeError(
            f"Graph {method.upper()} {url} → {resp.status_code}: {resp.text[:400]}"
        )
    if resp.content:
        return resp.json()
    return {}


# ── Step: discover tenant info ────────────────────────────────────────────────

async def discover_tenant(token: str) -> dict:
    """
    Fetch tenant ID and initial domain from /organization.
    Returns {"tenant_id": ..., "tenant_domain": ..., "smarthost": ...}
    """
    data = await _gh("get", f"{GRAPH}/organization?$select=id,verifiedDomains", token)
    orgs = data.get("value", [])
    if not orgs:
        raise RuntimeError("No organization found in token")
    org = orgs[0]
    tenant_id = org["id"]
    domains = org.get("verifiedDomains", [])
    initial = next((d["name"] for d in domains if d.get("isInitial")), None)
    if not initial:
        raise RuntimeError("Could not determine initial domain (.onmicrosoft.com)")
    smarthost = initial.replace(".onmicrosoft.com", ".mail.protection.outlook.com")
    log.info("Tenant: id=%s domain=%s smarthost=%s", tenant_id, initial, smarthost)
    return {"tenant_id": tenant_id, "tenant_domain": initial, "smarthost": smarthost}


# ── Step: generate auth certificate ──────────────────────────────────────────

def _generate_auth_cert() -> tuple[bytes, bytes]:
    """
    Generate a self-signed RSA-2048 certificate for Exchange Online app auth.
    Returns (cert_der_bytes, pfx_bytes).
    """
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        key_f = d / "auth.key"
        crt_f = d / "auth.crt"
        pfx_f = d / "auth.pfx"

        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048",
             "-keyout", str(key_f), "-out", str(crt_f),
             "-days", "3650", "-nodes",
             "-subj", "/CN=EXO-Signature-Service"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["openssl", "pkcs12", "-export",
             "-out", str(pfx_f),
             "-inkey", str(key_f), "-in", str(crt_f),
             "-passout", "pass:"],
            check=True, capture_output=True,
        )
        der = subprocess.run(
            ["openssl", "x509", "-in", str(crt_f), "-outform", "DER"],
            check=True, capture_output=True,
        ).stdout
        return der, pfx_f.read_bytes()


async def _upload_key_credential(token: str, app_object_id: str, cert_der: bytes) -> None:
    """Upload a certificate public key to an Azure AD app registration."""
    from datetime import datetime, timezone, timedelta
    cert_b64 = base64.b64encode(cert_der).decode()
    end_dt = (datetime.now(timezone.utc) + timedelta(days=3650)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await _gh("patch", f"{GRAPH}/applications/{app_object_id}", token, json={
        "keyCredentials": [{
            "type": "AsymmetricX509Cert",
            "usage": "Verify",
            "key": cert_b64,
            "endDateTime": end_dt,
        }]
    })
    log.info("Auth certificate uploaded to app %s", app_object_id)


# ── Step: create app registration (idempotent) ────────────────────────────────

async def create_app_registration(token: str, public_hostname: str) -> dict:
    """
    Create (or reuse) the '{GATEWAY_NAME}' app registration.
    Returns {"app_id": ..., "app_object_id": ..., "sp_id": ..., "client_secret": ...}
    """
    gateway_name = settings_store.get("GATEWAY_NAME") or "EXO Signature Gateway"
    gateway_name_odata = gateway_name.replace("'", "''")  # OData string literal escaping
    # Check if app already exists
    existing = await _gh(
        "get",
        f"{GRAPH}/applications?$filter=displayName eq '{gateway_name_odata}'&$select=id,appId",
        token,
    )
    existing_apps = existing.get("value", [])

    if existing_apps:
        app_object_id = existing_apps[0]["id"]
        app_id = existing_apps[0]["appId"]
        log.info("Reusing existing app: appId=%s objectId=%s", app_id, app_object_id)
    else:
        app_body = {
            "displayName": gateway_name,
            "signInAudience": "AzureADMyOrg",
            "requiredResourceAccess": [
                {
                    "resourceAppId": _GRAPH_APP_ID,
                    "resourceAccess": _GRAPH_PERMISSIONS,
                },
                {
                    "resourceAppId": _EXO_APP_ID,
                    "resourceAccess": _EXO_PERMISSIONS,
                },
            ],
        }
        app = await _gh("post", f"{GRAPH}/applications", token, json=app_body)
        app_object_id = app["id"]
        app_id = app["appId"]
        log.info("App registration created: appId=%s objectId=%s", app_id, app_object_id)

    # Find or create service principal
    sp_resp = await _gh(
        "get",
        f"{GRAPH}/servicePrincipals?$filter=appId eq '{app_id}'&$select=id",
        token,
    )
    sp_items = sp_resp.get("value", [])
    if sp_items:
        sp_id = sp_items[0]["id"]
        log.info("Reusing existing service principal: id=%s", sp_id)
    else:
        sp = await _gh("post", f"{GRAPH}/servicePrincipals", token, json={"appId": app_id})
        sp_id = sp["id"]
        log.info("Service principal created: id=%s", sp_id)

    # Grant admin consent and role (idempotent — errors are warnings)
    await _grant_admin_consent(token, sp_id)
    await _assign_exchange_admin_role(token, sp_id)

    # Always create a fresh client secret
    secret_resp = await _gh(
        "post",
        f"{GRAPH}/applications/{app_object_id}/addPassword",
        token,
        json={
            "passwordCredential": {
                "displayName": gateway_name,
                "endDateTime": "2099-12-31T00:00:00Z",
            }
        },
    )
    client_secret = secret_resp["secretText"]
    log.info("Client secret created for app %s", app_id)

    return {
        "app_id": app_id,
        "app_object_id": app_object_id,
        "sp_id": sp_id,
        "client_secret": client_secret,
    }


async def create_pool_app(token: str, index: int) -> dict:
    """
    Create/reuse '{GATEWAY_NAME} (Pool N)' with runtime-only permissions.
    No Exchange.ManageAsApp — pool apps are for mail processing only.
    Returns {"client_id": ..., "client_secret": ..., "label": ...}
    """
    gateway_name = settings_store.get("GATEWAY_NAME") or "EXO Signature Gateway"
    display_name = f"{gateway_name} (Pool {index})"
    display_name_odata = display_name.replace("'", "''")
    existing = await _gh(
        "get",
        f"{GRAPH}/applications?$filter=displayName eq '{display_name_odata}'&$select=id,appId",
        token,
    )
    existing_apps = existing.get("value", [])
    if existing_apps:
        app_object_id = existing_apps[0]["id"]
        app_id        = existing_apps[0]["appId"]
        log.info("Reusing existing pool app %d: appId=%s", index, app_id)
    else:
        app_body = {
            "displayName": display_name,
            "signInAudience": "AzureADMyOrg",
            "requiredResourceAccess": [
                {
                    "resourceAppId": _GRAPH_APP_ID,
                    "resourceAccess": _GRAPH_PERMISSIONS,   # same Graph scopes, no EXO
                },
            ],
        }
        app = await _gh("post", f"{GRAPH}/applications", token, json=app_body)
        app_object_id = app["id"]
        app_id        = app["appId"]
        log.info("Pool app %d created: appId=%s", index, app_id)

    # Service principal
    sp_resp = await _gh(
        "get",
        f"{GRAPH}/servicePrincipals?$filter=appId eq '{app_id}'&$select=id",
        token,
    )
    sp_items = sp_resp.get("value", [])
    if sp_items:
        sp_id = sp_items[0]["id"]
    else:
        sp = await _gh("post", f"{GRAPH}/servicePrincipals", token, json={"appId": app_id})
        sp_id = sp["id"]

    await _grant_admin_consent(token, sp_id)

    secret_resp = await _gh(
        "post",
        f"{GRAPH}/applications/{app_object_id}/addPassword",
        token,
        json={"passwordCredential": {
            "displayName": display_name,
            "endDateTime": "2099-12-31T00:00:00Z",
        }},
    )
    return {
        "client_id":     app_id,
        "client_secret": secret_resp["secretText"],
        "label":         display_name,
    }


async def _get_sp_id_for_resource(token: str, resource_app_id: str) -> str:
    """Look up the service principal object ID for a well-known app."""
    data = await _gh(
        "get",
        f"{GRAPH}/servicePrincipals?$filter=appId eq '{resource_app_id}'&$select=id",
        token,
    )
    items = data.get("value", [])
    if not items:
        raise RuntimeError(f"Service principal not found for appId {resource_app_id}")
    return items[0]["id"]


async def _get_app_roles(token: str, sp_id: str) -> dict:
    """Return {roleId: roleId} mapping for a service principal's appRoles."""
    data = await _gh("get", f"{GRAPH}/servicePrincipals/{sp_id}?$select=appRoles", token)
    return {r["id"]: r["id"] for r in data.get("appRoles", [])}


async def _grant_admin_consent(token: str, our_sp_id: str) -> None:
    """Grant admin consent for all required app roles."""
    graph_sp_id = await _get_sp_id_for_resource(token, _GRAPH_APP_ID)
    for perm in _GRAPH_PERMISSIONS:
        try:
            await _gh(
                "post",
                f"{GRAPH}/servicePrincipals/{our_sp_id}/appRoleAssignments",
                token,
                json={
                    "principalId": our_sp_id,
                    "resourceId": graph_sp_id,
                    "appRoleId": perm["id"],
                },
            )
            log.info("Granted Graph role %s", perm["id"])
        except Exception as exc:
            log.warning("Could not grant Graph role %s: %s", perm["id"], exc)

    try:
        exo_sp_id = await _get_sp_id_for_resource(token, _EXO_APP_ID)
        for perm in _EXO_PERMISSIONS:
            try:
                await _gh(
                    "post",
                    f"{GRAPH}/servicePrincipals/{our_sp_id}/appRoleAssignments",
                    token,
                    json={
                        "principalId": our_sp_id,
                        "resourceId": exo_sp_id,
                        "appRoleId": perm["id"],
                    },
                )
                log.info("Granted EXO role %s", perm["id"])
            except Exception as exc:
                log.warning("Could not grant EXO role %s: %s", perm["id"], exc)
    except Exception as exc:
        log.warning("Could not find EXO service principal: %s", exc)


async def _assign_exchange_admin_role(token: str, sp_id: str) -> None:
    """Assign the Exchange Administrator directory role to the service principal."""
    try:
        await _gh(
            "post",
            f"{GRAPH}/roleManagement/directory/roleAssignments",
            token,
            json={
                "principalId": sp_id,
                "roleDefinitionId": _EXCHANGE_ADMIN_ROLE_ID,
                "directoryScopeId": "/",
            },
        )
        log.info("Exchange Administrator role assigned to sp=%s", sp_id)
    except Exception as exc:
        log.warning("Could not assign Exchange Administrator role: %s", exc)


# ── Step: get current user UPN and patch Bootstrap app redirect URI ───────────

async def get_current_user_upn(token: str) -> str:
    """Get the UPN of the currently authenticated user via Graph /me."""
    try:
        me = await _gh("get", f"{GRAPH}/me?$select=userPrincipalName,mail", token)
        return me.get("userPrincipalName") or me.get("mail") or ""
    except Exception as exc:
        log.warning("Could not get current user UPN: %s", exc)
        return ""


_BOOTSTRAP_TARGET_NAME = "EXO Signature Gateway Login"
_BOOTSTRAP_OLD_NAMES = {"EXO Signature Gateway Setup", "EXO Signature Service Setup"}


async def patch_bootstrap_redirect_uri(token: str, hostname: str) -> None:
    """Add the public SSO redirect URI to the Bootstrap app and normalise its displayName."""
    if not hostname:
        return
    bootstrap_id = settings_store.get("BOOTSTRAP_CLIENT_ID") or ""
    if not bootstrap_id:
        return
    # Prefer ADDIN_BASE_URL (canonical external URL without port, works behind App Proxy)
    external_base = (settings_store.get("ADDIN_BASE_URL") or "").rstrip("/")
    if external_base:
        public_uri = f"{external_base}/auth/callback"
    else:
        port = config.WEBUI_PORT
        port_suffix = f":{port}" if port and port != 443 else ""
        public_uri = f"https://{hostname}{port_suffix}/auth/callback"
    try:
        resp = await _gh(
            "get",
            f"{GRAPH}/applications?$filter=appId eq '{bootstrap_id}'&$select=id,displayName,publicClient",
            token,
        )
        apps = resp.get("value", [])
        if not apps:
            log.warning("Bootstrap app %s not found in directory", bootstrap_id)
            return
        obj_id = apps[0]["id"]
        current_name = apps[0].get("displayName", "")
        existing_uris = apps[0].get("publicClient", {}).get("redirectUris", [])

        patch: dict = {}
        if public_uri not in existing_uris:
            existing_uris.append(public_uri)
            patch["publicClient"] = {"redirectUris": existing_uris}
            log.info("Added SSO redirect URI %s to Bootstrap app", public_uri)
        else:
            log.info("SSO redirect URI already present on Bootstrap app")

        if current_name in _BOOTSTRAP_OLD_NAMES:
            patch["displayName"] = _BOOTSTRAP_TARGET_NAME
            log.info("Renamed Bootstrap app from '%s' to '%s'", current_name, _BOOTSTRAP_TARGET_NAME)

        if patch:
            await _gh("patch", f"{GRAPH}/applications/{obj_id}", token, json=patch)
        settings_store.update({"BOOTSTRAP_REDIRECT_URIS": existing_uris})
    except Exception as exc:
        log.warning("Could not patch Bootstrap app: %s", exc)


# ── Step: run full setup after PKCE callback ──────────────────────────────────

async def run_post_auth_setup(token: str) -> dict:
    """
    Called after successful PKCE login.  Runs tenant discovery, app creation,
    auth cert generation/upload, and stores results in settings_store.
    Returns a status dict with results.
    """
    result: dict = {}

    # 1. Discover tenant
    tenant_info = await discover_tenant(token)
    settings_store.update({
        "TENANT_ID": tenant_info["tenant_id"],
        "TENANT_DOMAIN": tenant_info["tenant_domain"],
        "EXO_SMARTHOST": tenant_info["smarthost"],
    })
    result["tenant"] = tenant_info
    log.info("Tenant discovery complete")

    # 2. Create (or reuse) app registration
    app_info = await create_app_registration(token, settings_store.get("PUBLIC_HOSTNAME") or "")
    settings_store.update({
        "CLIENT_ID": app_info["app_id"],
        "CLIENT_SECRET": app_info["client_secret"],
        "AZURE_APP_CREATED": True,
    })
    _gc.reset_msal_app()
    result["app"] = {
        "app_id": app_info["app_id"],
        "sp_id": app_info["sp_id"],
    }
    log.info("App registration complete")

    # 2b. Capture setup admin UPN and patch Bootstrap app redirect URI
    try:
        import sso as sso_mod
        upn = await get_current_user_upn(token)
        if upn:
            users = sso_mod.normalize_users()
            if not any(e["upn"] == upn.strip().lower() for e in users):
                users.append({"upn": upn.strip().lower(), "role": sso_mod.ROLE_ADMIN})
                settings_store.update({"ADMIN_USERS": users})
                log.info("Added setup admin to ADMIN_USERS: %s", upn)
        hostname = settings_store.get("PUBLIC_HOSTNAME") or ""
        await patch_bootstrap_redirect_uri(token, hostname)
    except Exception as exc:
        log.warning("Admin UPN / redirect URI setup failed: %s", exc)

    # 3. Generate auth certificate and upload to Azure
    try:
        cert_der, pfx_bytes = _generate_auth_cert()
        await _upload_key_credential(token, app_info["app_object_id"], cert_der)
        _AUTH_CERT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _AUTH_CERT_PATH.write_bytes(pfx_bytes)
        result["auth_cert"] = str(_AUTH_CERT_PATH)
        log.info("Auth certificate generated and uploaded")
    except Exception as exc:
        log.error("Auth certificate setup failed: %s", exc)
        result["auth_cert_error"] = str(exc)

    return result


# ── Step: run PowerShell S/MIME transport rules setup ────────────────────────

def run_smime_rules_setup(
    app_id: str,
    tenant_domain: str,
    connector_name: str | None = None,
) -> dict:
    """
    Run the PowerShell script to create the two S/MIME inbound transport rules.
    Returns {"ok": bool, "output": str}.
    """
    script = Path("/app/scripts/setup_smime_rules.ps1")
    if not script.exists():
        return {"ok": False, "output": "PowerShell script not found"}

    if not _AUTH_CERT_PATH.exists():
        return {
            "ok": False,
            "output": (
                "Auth-Zertifikat nicht gefunden (/app/data/auth.pfx). "
                "Bitte Schritt 5 (Entra App-Registrierung → „App-Registrierung neu einrichten“) ausführen."
            ),
        }

    gateway_name = settings_store.get("GATEWAY_NAME") or "EXO Signature Gateway"
    if connector_name is None:
        connector_name = f"{gateway_name} - Outbound"

    cmd = [
        "pwsh", "-NoProfile", "-NonInteractive", "-File", str(script),
        "-AppId", app_id,
        "-Organization", tenant_domain,
        "-CertPath", str(_AUTH_CERT_PATH),
        "-GatewayName", gateway_name,
        "-ConnectorName", connector_name,
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        output = (proc.stdout + "\n" + proc.stderr).strip()
        if proc.returncode == 0:
            settings_store.update({"SMIME_RULES_CREATED": True})
            log.info("S/MIME transport rules setup succeeded")
            return {"ok": True, "output": output}
        else:
            log.error("S/MIME rules setup failed rc=%d: %s", proc.returncode, output)
            return {"ok": False, "output": output}
    except Exception as exc:
        log.error("S/MIME rules setup error: %s", exc)
        return {"ok": False, "output": str(exc)}


# ── Step: run PowerShell EXO connector setup ──────────────────────────────────

def run_exo_connector_setup(
    app_id: str,
    tenant_domain: str,
    smtp_proxy_hostname: str,
    skip_inbound_connector: bool = False,
) -> dict:
    """
    Run the PowerShell script to create the EXO Outbound Connector and
    Transport Rule.  Returns {"ok": bool, "output": str}.
    """
    script = Path("/app/scripts/setup_exo_connector.ps1")
    if not script.exists():
        return {"ok": False, "output": "PowerShell script not found"}

    if not _AUTH_CERT_PATH.exists():
        return {
            "ok": False,
            "output": (
                "Auth-Zertifikat nicht gefunden (/app/data/auth.pfx). "
                "Bitte Schritt 5 (Entra App-Registrierung → „App-Registrierung neu einrichten“) ausführen."
            ),
        }

    gateway_name = settings_store.get("GATEWAY_NAME") or "EXO Signature Gateway"
    cmd = [
        "pwsh", "-NoProfile", "-NonInteractive", "-File", str(script),
        "-AppId", app_id,
        "-Organization", tenant_domain,
        "-CertPath", str(_AUTH_CERT_PATH),
        "-SmtpProxyHostname", smtp_proxy_hostname,
        "-GatewayName", gateway_name,
    ]
    if skip_inbound_connector:
        cmd.append("-SkipInboundConnector")

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        output = (proc.stdout + "\n" + proc.stderr).strip()
        if proc.returncode == 0:
            settings_store.update({"EXO_CONNECTOR_CREATED": True})
            log.info("EXO connector setup succeeded")
            return {"ok": True, "output": output}
        else:
            log.error("EXO connector setup failed rc=%d: %s", proc.returncode, output)
            return {"ok": False, "output": output}
    except Exception as exc:
        log.error("EXO connector setup error: %s", exc)
        return {"ok": False, "output": str(exc)}


# ── Step: IMAP access setup (smtp587 / Azure mode) ────────────────────────────

def run_imap_access_setup(app_id: str, tenant_domain: str) -> dict:
    """
    Register the app as EXO Service Principal and grant IMAP FullAccess to all
    user mailboxes.  Required for REINJECT_MODE=smtp587 (IMAP APPEND, Azure).

    Two things happen:
      1. New-ServicePrincipal — registers the app in EXO so IMAP XOAUTH2 works
      2. Add-MailboxPermission (FullAccess) — grants per-mailbox IMAP access
    """
    import requests as _req

    if not _AUTH_CERT_PATH.exists():
        return {
            "ok": False,
            "output": (
                "Auth-Zertifikat nicht gefunden (/app/data/auth.pfx). "
                "Bitte Schritt 5 (Entra App-Registrierung → „App-Registrierung neu einrichten“) ausführen."
            ),
        }

    # Get the Entra ID service principal Object ID for New-ServicePrincipal
    token = _gc._acquire_token()
    if not token:
        return {"ok": False, "output": "Graph-Token nicht verfügbar. CLIENT_ID / CLIENT_SECRET prüfen."}

    try:
        r = _req.get(
            f"https://graph.microsoft.com/v1.0/servicePrincipals"
            f"?$filter=appId eq '{app_id}'&$select=id",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        items = r.json().get("value", [])
        if not items:
            return {
                "ok": False,
                "output": (
                    f"Kein Entra-Service-Principal für App-ID {app_id} gefunden. "
                    "App-Registrierung in Azure prüfen."
                ),
            }
        sp_object_id = items[0]["id"]
        log.info("Entra SP ObjectId for app %s: %s", app_id, sp_object_id)
    except Exception as exc:
        return {"ok": False, "output": f"Graph-Abfrage fehlgeschlagen: {exc}"}

    ps_lines = [
        "$ErrorActionPreference = 'Stop'",
        "",
        "# Zertifikat laden (PFX ohne Passwort)",
        f"$cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(",
        f"    '{_AUTH_CERT_PATH}', [string]$null,",
        "    ([System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet -bor",
        "     [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::Exportable))",
        "",
        f"Connect-ExchangeOnline -AppId '{app_id}' -Certificate $cert"
        f" -Organization '{tenant_domain}' -ShowBanner:$false -ShowProgress:$false",
        "",
        "# 1. Service Principal in EXO registrieren",
        f"$sp = Get-ServicePrincipal | Where-Object {{ $_.AppId -eq '{app_id}' }}",
        "if (-not $sp) {",
        f"    $sp = New-ServicePrincipal -AppId '{app_id}'"
        f" -ObjectId '{sp_object_id}' -DisplayName 'EXO Signature Gateway'",
        "    Write-Host \"Service Principal registriert: $($sp.ObjectId)\"",
        "} else {",
        "    Write-Host \"Service Principal bereits vorhanden: $($sp.ObjectId)\"",
        "}",
        "",
        "# 2. FullAccess auf alle Postfächer setzen",
        "$mailboxes = Get-Mailbox -RecipientTypeDetails UserMailbox -ResultSize Unlimited",
        "$count = 0",
        "$failed = 0",
        "foreach ($m in $mailboxes) {",
        "    try {",
        "        Add-MailboxPermission -Identity $m.PrimarySmtpAddress"
        " -User $sp.ObjectId -AccessRights FullAccess"
        " -AutoMapping $false -ErrorAction Stop | Out-Null",
        "        Write-Host \"+ $($m.PrimarySmtpAddress)\"",
        "        $count++",
        "    } catch {",
        "        if ($_.Exception.Message -like '*already present*') {",
        "            Write-Host \"= $($m.PrimarySmtpAddress) (bereits vorhanden)\"",
        "            $count++",
        "        } else {",
        "            Write-Host \"FEHLER $($m.PrimarySmtpAddress): $($_.Exception.Message)\"",
        "            $failed++",
        "        }",
        "    }",
        "}",
        "Write-Host \"Fertig: $count Postfächer konfiguriert, $failed Fehler.\"",
        "if ($failed -gt 0) { exit 1 }",
        "Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue",
    ]

    with tempfile.NamedTemporaryFile(suffix=".ps1", mode="w", delete=False) as f:
        f.write("\n".join(ps_lines))
        ps_path = f.name

    try:
        proc = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-File", ps_path],
            capture_output=True, text=True, timeout=180,
        )
        output = (proc.stdout + "\n" + proc.stderr).strip()
        ok = proc.returncode == 0
        if ok:
            settings_store.update({"IMAP_ACCESS_CONFIGURED": True})
            log.info("IMAP access setup succeeded")
        else:
            log.error("IMAP access setup failed rc=%d: %s", proc.returncode, output)
        return {"ok": ok, "output": output}
    except Exception as exc:
        log.error("IMAP access setup error: %s", exc)
        return {"ok": False, "output": str(exc)}
    finally:
        Path(ps_path).unlink(missing_ok=True)


def run_mailbox_dg_update(app_id: str, tenant_domain: str, members: list[str]) -> dict:
    """
    Create/update 'EXO Signature Gateway - Enabled Mailboxes' DG and
    update the transport rule to route only DG members.
    Returns {"ok": bool, "output": str}.
    """
    script = Path("/app/scripts/update_mailbox_dg.ps1")
    if not script.exists():
        return {"ok": False, "output": "PowerShell script not found"}
    if not _AUTH_CERT_PATH.exists():
        return {"ok": False, "output": "Auth-Zertifikat nicht gefunden — bitte Schritt 5 (Entra App-Registrierung → „App-Registrierung neu einrichten“) ausführen."}

    gateway_name = settings_store.get("GATEWAY_NAME") or "EXO Signature Gateway"
    cmd = [
        "pwsh", "-NoProfile", "-NonInteractive", "-File", str(script),
        "-AppId", app_id,
        "-Organization", tenant_domain,
        "-CertPath", str(_AUTH_CERT_PATH),
        "-GatewayName", gateway_name,
    ]
    if members:
        cmd += ["-Members", ",".join(members)]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        output = (proc.stdout + "\n" + proc.stderr).strip()
        if proc.returncode == 0:
            log.info("Mailbox DG update succeeded (%d members)", len(members))
            return {"ok": True, "output": output}
        log.error("Mailbox DG update failed rc=%d: %s", proc.returncode, output)
        return {"ok": False, "output": output}
    except Exception as exc:
        log.error("Mailbox DG update error: %s", exc)
        return {"ok": False, "output": str(exc)}


def run_fetch_bookings_urls(app_id: str, tenant_domain: str, emails: list[str]) -> dict:
    """
    Fetch ExchangeGuid for each mailbox via PS and compute Bookings URLs.
    Returns {"ok": bool, "urls": {email: url}, "output": str}.
    """
    if not _AUTH_CERT_PATH.exists():
        return {"ok": False, "urls": {}, "output": "Auth-Zertifikat nicht gefunden."}
    if not emails:
        return {"ok": False, "urls": {}, "output": "Keine Postfächer konfiguriert."}

    emails_ps = ",".join(f'"{e}"' for e in emails)
    ps_cmd = f"""
Import-Module ExchangeOnlineManagement
$cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
    "/app/data/auth.pfx", [string]$null,
    ([System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet))
Connect-ExchangeOnline -AppId "{app_id}" -Certificate $cert -Organization "{tenant_domain}" -ShowBanner:$false
$emails = @({emails_ps})
$results = $emails | ForEach-Object {{
    try {{
        $m = Get-Mailbox -Identity $_ -ErrorAction Stop
        [PSCustomObject]@{{
            email = $m.PrimarySmtpAddress.ToString().ToLower()
            guid  = $m.ExchangeGuid.ToString("N")
        }}
    }} catch {{ $null }}
}} | Where-Object {{ $_ -ne $null }}
$results | ConvertTo-Json -Compress
Disconnect-ExchangeOnline -Confirm:$false
"""
    try:
        proc = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=120,
        )
        output = (proc.stdout + "\n" + proc.stderr).strip()
        if proc.returncode != 0:
            return {"ok": False, "urls": {}, "output": output}

        import json as _json, re as _re
        json_match = _re.search(r'(\[.*\]|\{.*\})', proc.stdout, _re.DOTALL)
        if not json_match:
            return {"ok": False, "urls": {}, "output": f"Keine JSON-Ausgabe.\n{output}"}
        raw = _json.loads(json_match.group(1))
        if isinstance(raw, dict):
            raw = [raw]
        urls = {}
        for item in raw:
            email = item.get("email", "")
            guid = item.get("guid", "")
            if email and guid:
                domain = email.split("@", 1)[1] if "@" in email else tenant_domain
                urls[email] = f"https://outlook.office.com/bookwithme/user/{guid}@{domain}?anonymous&ep=signature"
        return {"ok": True, "urls": urls, "output": output}
    except Exception as exc:
        return {"ok": False, "urls": {}, "output": str(exc)}


def run_notification_dg_update(members: list[str]) -> dict:
    """
    Create/update 'EXO Signature Gateway - Notification recipients' DG and
    synchronise the given member list.
    Returns {"ok": bool, "email": str, "output": str}.
    """
    if not _AUTH_CERT_PATH.exists():
        return {"ok": False, "email": "", "output": "Auth-Zertifikat nicht gefunden"}
    app_id = config.CLIENT_ID or settings_store.get("CLIENT_ID") or ""
    org = settings_store.get("TENANT_DOMAIN") or ""
    if not app_id or not org:
        return {"ok": False, "email": "", "output": "CLIENT_ID oder TENANT_DOMAIN nicht konfiguriert"}

    cert = str(_AUTH_CERT_PATH)
    # Build comma-separated member list for PS (PS script splits on comma internally)
    members_csv = ",".join(members) if members else ""
    gateway_name = settings_store.get("GATEWAY_NAME") or "EXO Signature Gateway"
    dg_alias = "".join(ch for ch in gateway_name if ch.isalnum()) + "Notifications"

    ps_script = f"""
$ErrorActionPreference = 'Stop'
$cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
    '{cert}', [string]$null,
    ([System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet))
Connect-ExchangeOnline -AppId '{app_id}' -Certificate $cert -Organization '{org}' -ShowBanner:$false -ShowProgress:$false
$dgName = "{gateway_name} - Notification recipients"
$dgAlias = "{dg_alias}"
$dg = Get-DistributionGroup -Identity $dgName -ErrorAction SilentlyContinue
if (-not $dg) {{
    $dg = New-DistributionGroup -Name $dgName -Alias $dgAlias -Type Distribution -MemberJoinRestriction Closed -MemberDepartRestriction Closed -ErrorAction Stop
}}
$membersStr = '{members_csv}'
$desired = @()
if ($membersStr) {{
    $desired = $membersStr -split ',' | ForEach-Object {{ $_.Trim() }} | Where-Object {{ $_ -ne '' }}
}}
$current = @(Get-DistributionGroupMember -Identity $dg.Identity -ErrorAction SilentlyContinue | Select-Object -ExpandProperty PrimarySmtpAddress)
foreach ($m in $desired) {{
    if ($current -notcontains $m) {{
        Add-DistributionGroupMember -Identity $dg.Identity -Member $m -ErrorAction SilentlyContinue
    }}
}}
foreach ($m in $current) {{
    if ($desired -notcontains $m) {{
        Remove-DistributionGroupMember -Identity $dg.Identity -Member $m -Confirm:$false -ErrorAction SilentlyContinue
    }}
}}
$email = if ($dg.PrimarySmtpAddress) {{ $dg.PrimarySmtpAddress }} else {{ '' }}
Write-Output (@{{ok=$true; email=$email}} | ConvertTo-Json -Compress)
Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
"""
    try:
        proc = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, timeout=180,
        )
        output = (proc.stdout + "\n" + proc.stderr).strip()
        import json as _json
        for line in proc.stdout.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    result = _json.loads(line)
                    if result.get("ok"):
                        log.info("Notification DG updated with %d members, email=%s", len(members), result.get("email"))
                        return {"ok": True, "email": result.get("email", ""), "output": output}
                except Exception:
                    pass
        log.error("Notification DG update failed rc=%d: %s", proc.returncode, output)
        return {"ok": False, "email": "", "output": output}
    except Exception as exc:
        log.error("Notification DG update error: %s", exc)
        return {"ok": False, "email": "", "output": str(exc)}


def run_create_notification_mailbox() -> dict:
    """
    Create the shared mailbox "{GatewayName}-Notification" (alias derived from
    the configured GATEWAY_NAME, no hardcoded product name) if it doesn't
    already exist (idempotent — Get-Mailbox check before New-Mailbox below).
    If GATEWAY_NAME changes later, the alias changes too, so this naturally
    creates a fresh mailbox under the new name rather than silently reusing
    the old one.
    Returns {"ok": bool, "email": str, "output": str}.
    """
    if not _AUTH_CERT_PATH.exists():
        return {"ok": False, "email": "", "output": "Auth-Zertifikat nicht gefunden"}
    app_id = config.CLIENT_ID or settings_store.get("CLIENT_ID") or ""
    org = settings_store.get("TENANT_DOMAIN") or ""
    if not app_id or not org:
        return {"ok": False, "email": "", "output": "CLIENT_ID oder TENANT_DOMAIN nicht konfiguriert"}

    cert = str(_AUTH_CERT_PATH)
    gateway_name = settings_store.get("GATEWAY_NAME") or "EXO Signature Gateway"
    alias = "".join(ch for ch in gateway_name if ch.isalnum()) + "-Notification"
    display_name = f"{gateway_name} Notification"

    ps_script = f"""
$ErrorActionPreference = 'Stop'
$cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
    '{cert}', [string]$null,
    ([System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet))
Connect-ExchangeOnline -AppId '{app_id}' -Certificate $cert -Organization '{org}' -ShowBanner:$false -ShowProgress:$false
$mbx = Get-Mailbox -Identity '{alias}' -ErrorAction SilentlyContinue
if (-not $mbx) {{
    $mbx = New-Mailbox -Shared -Name '{display_name}' -DisplayName '{display_name}' -Alias '{alias}' -ErrorAction Stop
}}
$email = if ($mbx.PrimarySmtpAddress) {{ $mbx.PrimarySmtpAddress }} else {{ '' }}
Write-Output (@{{ok=$true; email=$email}} | ConvertTo-Json -Compress)
Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
"""
    try:
        proc = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, timeout=120,
        )
        output = (proc.stdout + "\n" + proc.stderr).strip()
        import json as _json
        for line in proc.stdout.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    result = _json.loads(line)
                    if result.get("ok"):
                        log.info("Notification-Shared-Mailbox angelegt/vorhanden: %s", result.get("email"))
                        return {"ok": True, "email": result.get("email", ""), "output": output}
                except Exception:
                    pass
        log.error("Notification-Shared-Mailbox-Anlage fehlgeschlagen rc=%d: %s", proc.returncode, output)
        return {"ok": False, "email": "", "output": output}
    except Exception as exc:
        log.error("Notification-Shared-Mailbox-Anlage Fehler: %s", exc)
        return {"ok": False, "email": "", "output": str(exc)}


# ── EXO state verification ────────────────────────────────────────────────────

def _run_verify_ps(body: str) -> dict:
    """Connect to EXO, run a short PS body that emits one JSON line, return it."""
    import json as _json
    app_id = config.CLIENT_ID or settings_store.get("CLIENT_ID") or ""
    org = settings_store.get("TENANT_DOMAIN") or ""
    if not app_id or not org:
        return {"ok": False, "error": "CLIENT_ID oder TENANT_DOMAIN nicht konfiguriert"}
    if not _AUTH_CERT_PATH.exists():
        return {"ok": False, "error": "Auth-Zertifikat nicht gefunden"}
    cert = str(_AUTH_CERT_PATH)
    connect = (
        "$ErrorActionPreference = 'SilentlyContinue'\n"
        "$cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new("
        f"'{cert}', [string]$null, "
        "([System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet -bor "
        "[System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::Exportable))\n"
        f"Connect-ExchangeOnline -AppId '{app_id}' -Certificate $cert -Organization '{org}'"
        " -ShowBanner:$false -ShowProgress:$false\n"
    )
    full = connect + body + "\nDisconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue"
    try:
        proc = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", full],
            capture_output=True, text=True, timeout=60,
        )
        for line in proc.stdout.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                return _json.loads(line)
        return {"ok": False, "error": (proc.stderr or proc.stdout)[:300]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def verify_connector(smtp_mode: bool = False) -> dict:
    """Check EXO connectors and transport rule. Inbound Connector only required in SMTP mode."""
    gw = (settings_store.get("GATEWAY_NAME") or "EXO Signature Gateway").replace('"', '`"')
    if smtp_mode:
        ps = (
            f'$out  = $null -ne (Get-OutboundConnector -Identity "{gw} - Outbound" -ErrorAction SilentlyContinue)\n'
            f'$in   = $null -ne (Get-InboundConnector  -Identity "{gw} - Inbound"  -ErrorAction SilentlyContinue)\n'
            f'$rule = $null -ne (Get-TransportRule      -Identity "Route via {gw}"  -ErrorAction SilentlyContinue)\n'
            'Write-Output (@{ok=$out -and $in -and $rule; outbound=$out; inbound=$in; rule=$rule} | ConvertTo-Json -Compress)\n'
        )
    else:
        ps = (
            f'$out  = $null -ne (Get-OutboundConnector -Identity "{gw} - Outbound" -ErrorAction SilentlyContinue)\n'
            f'$in   = $null -ne (Get-InboundConnector  -Identity "{gw} - Inbound"  -ErrorAction SilentlyContinue)\n'
            f'$rule = $null -ne (Get-TransportRule      -Identity "Route via {gw}"  -ErrorAction SilentlyContinue)\n'
            'Write-Output (@{ok=$out -and $rule; outbound=$out; inbound=$in; rule=$rule} | ConvertTo-Json -Compress)\n'
        )
    return _run_verify_ps(ps)


def verify_imap() -> dict:
    """Check that the Gateway app is registered as an EXO ServicePrincipal."""
    app_id = config.CLIENT_ID or settings_store.get("CLIENT_ID") or ""
    body = (
        f'$sp   = Get-ServicePrincipal -AppId "{app_id}" -ErrorAction SilentlyContinue\n'
        '$ok   = $null -ne $sp\n'
        '$name = if ($sp) { $sp.DisplayName } else { "" }\n'
        'Write-Output (@{ok=$ok; displayName=$name} | ConvertTo-Json -Compress)\n'
    )
    return _run_verify_ps(body)


def verify_smime_rules() -> dict:
    """Check that both S/MIME inbound transport rules exist."""
    gw = (settings_store.get("GATEWAY_NAME") or "EXO Signature Gateway").replace('"', '`"')
    return _run_verify_ps(
        f'$signed = $null -ne (Get-TransportRule -Identity "{gw} - SMIME Signed Inbound"    -ErrorAction SilentlyContinue)\n'
        f'$enc    = $null -ne (Get-TransportRule -Identity "{gw} - SMIME Encrypted Inbound" -ErrorAction SilentlyContinue)\n'
        'Write-Output (@{ok=$signed -and $enc; signed=$signed; encrypted=$enc} | ConvertTo-Json -Compress)\n'
    )


# ── Remote Domain: castle.cloud ───────────────────────────────────────────────

def configure_remote_domain_castle() -> dict:
    """Create (if missing) and configure the Remote Domain for castle.cloud.

    Sets ByteEncoderTypeFor7BitCharsets=Use7Bit to prevent Exchange from
    changing Content-Transfer-Encoding 7bit → quoted-printable for ACME mails.
    """
    body = (
        'if (-not (Get-RemoteDomain -Identity "Castle ACME" -ErrorAction SilentlyContinue)) {\n'
        '    New-RemoteDomain -Name "Castle ACME" -DomainName "castle.cloud" | Out-Null\n'
        '    Write-Host "Created"\n'
        '} else { Write-Host "Exists" }\n'
        'Set-RemoteDomain -Identity "Castle ACME" '
        '-ContentType MimeText '
        '-CharacterSet us-ascii '
        '-NonMimeCharacterSet us-ascii '
        '-TNEFEnabled $false '
        '-LineWrapSize Unlimited '
        '-ByteEncoderTypeFor7BitCharsets Use7Bit\n'
        '$d = Get-RemoteDomain -Identity "Castle ACME"\n'
        'Write-Output (@{'
        'ok=$true; '
        'ContentType=[string]$d.ContentType; '
        'CharacterSet=$d.CharacterSet; '
        'TNEFEnabled=[bool]$d.TNEFEnabled; '
        'LineWrapSize=[string]$d.LineWrapSize; '
        'ByteEncoderTypeFor7BitCharsets=[string]$d.ByteEncoderTypeFor7BitCharsets'
        '} | ConvertTo-Json -Compress)\n'
    )
    return _run_verify_ps(body)


def get_remote_domain_castle() -> dict:
    """Read current Remote Domain settings for castle.cloud."""
    body = (
        '$d = Get-RemoteDomain -Identity "Castle ACME" -ErrorAction SilentlyContinue\n'
        'if ($null -eq $d) { Write-Output \'{"ok":false,"error":"not_found"}\'; exit }\n'
        'Write-Output (@{'
        'ok=$true; '
        'ContentType=[string]$d.ContentType; '
        'CharacterSet=$d.CharacterSet; '
        'TNEFEnabled=[bool]$d.TNEFEnabled; '
        'LineWrapSize=[string]$d.LineWrapSize; '
        'ByteEncoderTypeFor7BitCharsets=[string]$d.ByteEncoderTypeFor7BitCharsets'
        '} | ConvertTo-Json -Compress)\n'
    )
    return _run_verify_ps(body)


def remove_remote_domain_castle() -> dict:
    """Delete the castle.cloud Remote Domain entry."""
    body = (
        'if (Get-RemoteDomain -Identity "Castle ACME" -ErrorAction SilentlyContinue) {\n'
        '    Remove-RemoteDomain -Identity "Castle ACME" -Confirm:$false\n'
        '    Write-Output \'{"ok":true,"removed":true}\'\n'
        '} else { Write-Output \'{"ok":true,"removed":false}\' }\n'
    )
    return _run_verify_ps(body)
