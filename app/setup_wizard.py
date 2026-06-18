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
    Create (or reuse) the 'EXO Signature Gateway' app registration.
    Returns {"app_id": ..., "app_object_id": ..., "sp_id": ..., "client_secret": ...}
    """
    # Check if app already exists
    existing = await _gh(
        "get",
        f"{GRAPH}/applications?$filter=displayName eq 'EXO Signature Gateway'&$select=id,appId",
        token,
    )
    existing_apps = existing.get("value", [])

    if existing_apps:
        app_object_id = existing_apps[0]["id"]
        app_id = existing_apps[0]["appId"]
        log.info("Reusing existing app: appId=%s objectId=%s", app_id, app_object_id)
    else:
        app_body = {
            "displayName": "EXO Signature Gateway",
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
                "displayName": "EXO Signature Gateway",
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
    connector_name: str = "EXO Signature Gateway - Outbound",
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
                "Bitte Schritt 3 (Azure-Anmeldung) erneut durchführen."
            ),
        }

    cmd = [
        "pwsh", "-NoProfile", "-NonInteractive", "-File", str(script),
        "-AppId", app_id,
        "-Organization", tenant_domain,
        "-CertPath", str(_AUTH_CERT_PATH),
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
                "Bitte Schritt 3 (Azure-Anmeldung) erneut durchführen."
            ),
        }

    cmd = [
        "pwsh", "-NoProfile", "-NonInteractive", "-File", str(script),
        "-AppId", app_id,
        "-Organization", tenant_domain,
        "-CertPath", str(_AUTH_CERT_PATH),
        "-SmtpProxyHostname", smtp_proxy_hostname,
    ]

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
                "Bitte Schritt 3 (Azure-Anmeldung) erneut durchführen."
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
        "foreach ($m in $mailboxes) {",
        "    Add-MailboxPermission -Identity $m.PrimarySmtpAddress"
        " -User $sp.ObjectId -AccessRights FullAccess"
        " -AutoMapping $false -ErrorAction SilentlyContinue | Out-Null",
        "    Write-Host \"+ $($m.PrimarySmtpAddress)\"",
        "    $count++",
        "}",
        "Write-Host \"Fertig: $count Postfächer konfiguriert.\"",
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
        return {"ok": False, "output": "Auth-Zertifikat nicht gefunden — bitte Schritt 4 (Entra-Login) erneut ausführen."}

    cmd = [
        "pwsh", "-NoProfile", "-NonInteractive", "-File", str(script),
        "-AppId", app_id,
        "-Organization", tenant_domain,
        "-CertPath", str(_AUTH_CERT_PATH),
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


def verify_connector() -> dict:
    """Check that both EXO connectors and the main transport rule exist."""
    return _run_verify_ps(
        '$out  = $null -ne (Get-OutboundConnector -Identity "EXO Signature Gateway - Outbound" -ErrorAction SilentlyContinue)\n'
        '$in   = $null -ne (Get-InboundConnector  -Identity "EXO Signature Gateway - Inbound"  -ErrorAction SilentlyContinue)\n'
        '$rule = $null -ne (Get-TransportRule      -Identity "Route via EXO Signature Gateway"  -ErrorAction SilentlyContinue)\n'
        'Write-Output (@{ok=$out -and $in -and $rule; outbound=$out; inbound=$in; rule=$rule} | ConvertTo-Json -Compress)\n'
    )


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
    return _run_verify_ps(
        '$signed = $null -ne (Get-TransportRule -Identity "EXO Signature Gateway - SMIME Signed Inbound"    -ErrorAction SilentlyContinue)\n'
        '$enc    = $null -ne (Get-TransportRule -Identity "EXO Signature Gateway - SMIME Encrypted Inbound" -ErrorAction SilentlyContinue)\n'
        'Write-Output (@{ok=$signed -and $enc; signed=$signed; encrypted=$enc} | ConvertTo-Json -Compress)\n'
    )
