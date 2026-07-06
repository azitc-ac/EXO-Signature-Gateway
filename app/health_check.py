"""
Mailbox health check system.

Runs per-configured-mailbox checks and stores results in settings_store under
MAILBOX_HEALTH (dict keyed by email). Gateway auto-fix actions are appended
to GATEWAY_AUDIT_LOG (rolling 200 entries).

Public API:
    async def run_all_checks(emails=None) -> dict
    async def run_checks_for_mailbox(email: str) -> dict
"""
import asyncio
import hashlib
import json
import logging
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import settings_store

log = logging.getLogger(__name__)

_AUTH_CERT_PATH = Path("/app/data/auth.pfx")
_AUDIT_MAX = 200


# ── Audit log ─────────────────────────────────────────────────────────────────

def _append_audit(action: str, mailbox: str, detail: str) -> None:
    """Append one entry to GATEWAY_AUDIT_LOG (rolling 200)."""
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "action": action,
        "mailbox": mailbox,
        "detail": detail,
    }
    log_list: list = list(settings_store.get("GATEWAY_AUDIT_LOG") or [])
    log_list.append(entry)
    if len(log_list) > _AUDIT_MAX:
        log_list = log_list[-_AUDIT_MAX:]
    settings_store.force_update({"GATEWAY_AUDIT_LOG": log_list})


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_result(status: str, detail: str) -> dict:
    return {"status": status, "checked_at": _now_str(), "detail": detail}


# ── Check 1: exo_mailbox ──────────────────────────────────────────────────────

def _check_exo_mailbox_sync(email: str) -> dict:
    """
    Authoritative existence check via EXO (Get-EXOMailbox, through exo_mailboxes'
    cached enumeration + alias resolution) — replaces the former Graph/license-
    based check.

    Why: Graph /users assignedPlans answers "does this account have an Exchange
    license", not "does this mailbox exist and work". That produced a false
    "Keine aktive Exchange-Lizenz" warning for Shared Mailboxes, which have no
    license but are perfectly valid, functioning EXO mailboxes. EXO's own
    Get-EXOMailbox is the direct, correct answer to "does this mailbox exist".
    """
    import exo_mailboxes
    guid = exo_mailboxes.resolve_guid(email)
    if guid:
        return _make_result("ok", "Postfach in EXO vorhanden")
    return _make_result("error", "Postfach nicht in EXO gefunden (auch nicht als Alias)")


# ── Check 2+3: dg_member + imap_permission (single PS run for all mailboxes) ─

def _check_exo_batch_sync(emails: list[str]) -> dict:
    """
    Run a single PowerShell session for all emails at once.
    Returns dict: {email: {"dg_member": bool, "dg_fixed": bool,
                            "imap_perm": bool, "imap_fixed": bool}}
    """
    import config as _config

    app_id = _config.CLIENT_ID or settings_store.get("CLIENT_ID") or ""
    tenant_domain = settings_store.get("TENANT_DOMAIN") or ""
    reinject_mode = settings_store.get("REINJECT_MODE") or "smtp"

    if not app_id or not tenant_domain:
        error_result = {"dg_member": False, "dg_fixed": False, "imap_perm": None, "imap_fixed": False,
                        "error": "CLIENT_ID oder TENANT_DOMAIN nicht konfiguriert"}
        return {e: dict(error_result) for e in emails}
    if not _AUTH_CERT_PATH.exists():
        error_result = {"dg_member": False, "dg_fixed": False, "imap_perm": None, "imap_fixed": False,
                        "error": "Auth-Zertifikat nicht gefunden"}
        return {e: dict(error_result) for e in emails}

    check_imap = (reinject_mode == "imap")
    gw = settings_store.get("GATEWAY_NAME") or "EXO Signature Gateway"
    dg_name = f"{gw} - Enabled Mailboxes"

    # Build email list as JSON for PS
    emails_json = json.dumps(emails)

    ps_check_imap = "1" if check_imap else "0"

    ps_script = f"""
$ErrorActionPreference = 'SilentlyContinue'
$cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
    '{_AUTH_CERT_PATH}', [string]$null,
    ([System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet -bor
     [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::Exportable))
try {{
    Connect-ExchangeOnline -AppId '{app_id}' -Certificate $cert -Organization '{tenant_domain}' -ShowBanner:$false -ShowProgress:$false -ErrorAction Stop
}} catch {{
    @{{error="EXO-Verbindung fehlgeschlagen: $($_.Exception.Message)"}} | ConvertTo-Json -Compress
    exit 1
}}

$dgName = '{dg_name}'
$checkImap = [int]'{ps_check_imap}'
$emailsJson = '{emails_json.replace("'", "''")}'
$emails = $emailsJson | ConvertFrom-Json

# Get SP for IMAP permission checks
$sp = $null
if ($checkImap -eq 1) {{
    $sp = Get-ServicePrincipal | Where-Object {{ $_.AppId -eq '{app_id}' }}
}}

# Get current DG members
$dgMembers = @()
$dgFetchError = $null
try {{
    $dg = Get-DistributionGroup -Identity $dgName -ErrorAction Stop
    $dgMemberObjs = Get-DistributionGroupMember -Identity $dgName -ResultSize Unlimited -ErrorAction Stop
    $dgMembers = @($dgMemberObjs | ForEach-Object {{ $_.PrimarySmtpAddress.ToLower() }})
}} catch {{
    $dgFetchError = $_.Exception.Message
}}

$results = @{{}}
foreach ($email in $emails) {{
    $emailLow = $email.ToLower()
    $dgFixed = $false
    $imapPerm = $null
    $imapFixed = $false

    if ($dgFetchError) {{
        # DG fetch failed — report error, skip auto-fix attempt
        $results[$emailLow] = @{{
            dg_member = $false
            dg_fixed = $false
            imap_perm = $imapPerm
            imap_fixed = $imapFixed
            error = "DG-Abruf fehlgeschlagen: $dgFetchError"
        }}
        continue
    }}

    $dgMember = $dgMembers -contains $emailLow

    # Auto-fix: add to DG if missing
    if (-not $dgMember) {{
        try {{
            Add-DistributionGroupMember -Identity $dgName -Member $email -ErrorAction Stop
            $dgMember = $true
            $dgFixed = $true
        }} catch {{
            if ($_.Exception.Message -like '*already*' -or $_.Exception.Message -like '*Duplicate*' -or $_.Exception.Message -like '*member*') {{
                $dgMember = $true
            }}
        }}
    }}

    # IMAP permission check
    if ($checkImap -eq 1 -and $sp) {{
        try {{
            $perm = Get-MailboxPermission -Identity $email -User $sp.Identity -ErrorAction Stop
            $imapPerm = ($null -ne $perm)
        }} catch {{
            $imapPerm = $false
        }}
        # Auto-fix: grant FullAccess if missing
        if ($imapPerm -eq $false) {{
            try {{
                Add-MailboxPermission -Identity $email -User $sp.Identity -AccessRights FullAccess -AutoMapping $false -ErrorAction Stop | Out-Null
                $imapPerm = $true
                $imapFixed = $true
            }} catch {{
                if ($_.Exception.Message -like '*already present*') {{
                    $imapPerm = $true
                }} else {{
                    $imapPerm = $false
                }}
            }}
        }}
    }}

    $results[$emailLow] = @{{
        dg_member = $dgMember
        dg_fixed = $dgFixed
        imap_perm = $imapPerm
        imap_fixed = $imapFixed
    }}
}}

$results | ConvertTo-Json -Depth 3 -Compress
Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
"""

    try:
        with tempfile.NamedTemporaryFile(suffix=".ps1", mode="w", delete=False,
                                         encoding="utf-8") as f:
            f.write(ps_script)
            ps_path = f.name

        proc = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-File", ps_path],
            capture_output=True, text=True, timeout=180,
        )
        Path(ps_path).unlink(missing_ok=True)

        output = proc.stdout.strip()
        # Find JSON line(s)
        for line in output.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                raw = json.loads(line)
                # Connection-error sentinel: {"error": "..."}
                if "error" in raw and len(raw) == 1:
                    err_msg = raw["error"]
                    log.warning("health_check: PS EXO connect error: %s", err_msg)
                    error_result = {"dg_member": False, "dg_fixed": False,
                                    "imap_perm": None, "imap_fixed": False,
                                    "error": err_msg}
                    return {e.lower(): dict(error_result) for e in emails}
                # Normal result: {email: {dg_member, ...}}
                result = {}
                for k, v in raw.items():
                    if not isinstance(v, dict):
                        continue
                    result[k.lower()] = {
                        "dg_member": bool(v.get("dg_member", False)),
                        "dg_fixed": bool(v.get("dg_fixed", False)),
                        "imap_perm": v.get("imap_perm"),
                        "imap_fixed": bool(v.get("imap_fixed", False)),
                        "error": v.get("error"),
                    }
                return result
            except Exception as exc:
                log.warning("health_check: PS JSON parse error: %s — %s", exc, line[:300])

        err_msg = (proc.stderr or proc.stdout or "Kein Output")[:300]
        log.warning("health_check: PS batch check failed rc=%d: %s", proc.returncode, err_msg)
        error_result = {"dg_member": False, "dg_fixed": False, "imap_perm": None, "imap_fixed": False,
                        "error": f"PS Fehler (rc={proc.returncode}): {err_msg}"}
        return {e.lower(): dict(error_result) for e in emails}

    except Exception as exc:
        log.error("health_check: _check_exo_batch_sync error: %s", exc)
        error_result = {"dg_member": False, "dg_fixed": False, "imap_perm": None, "imap_fixed": False,
                        "error": str(exc)}
        return {e.lower(): dict(error_result) for e in emails}


# ── Check 4: template ─────────────────────────────────────────────────────────

async def _check_template(email: str, cfg: dict) -> dict:
    """Try to render the signature template for this user."""
    try:
        import graph_client
        import signature_engine
        user = await graph_client.get_user(email)
        if not user.displayName:
            # Still try to render, but note incomplete data
            template_name = cfg.get("template", "default")
            try:
                signature_engine.render(user, template_name=template_name)
            except Exception as exc:
                return _make_result("error", f"Template-Fehler: {exc}")
            return _make_result("warn", "Benutzerdaten unvollständig (displayName leer)")
        template_name = cfg.get("template", "default")
        signature_engine.render(user, template_name=template_name)
        return _make_result("ok", "Vorlage gerendert")
    except Exception as exc:
        return _make_result("error", f"Template-Fehler: {exc}")


# ── Check 5: smime_cert ───────────────────────────────────────────────────────

def _check_smime_cert(email: str) -> dict:
    """Check S/MIME certificate validity and expiry."""
    import smime_store
    warn_days = int(settings_store.get("CERT_WARN_DAYS") or 14)
    try:
        certs = smime_store.list_user_certs(email)
    except Exception as exc:
        return _make_result("error", f"Fehler beim Laden: {exc}")

    if not certs:
        return _make_result("error", "Kein Zertifikat gefunden")

    # Use the default cert
    default = next((c for c in certs if c.get("is_default")), certs[0])
    if default.get("error"):
        return _make_result("error", f"Zertifikat ungültig: {default['error']}")

    days = default.get("days_left", 999)
    expiry = default.get("expiry", "?")
    if default.get("expired"):
        return _make_result("error", f"Abgelaufen seit {abs(days)} Tagen ({expiry})")
    if days <= warn_days:
        return _make_result("warn", f"Läuft in {days} Tagen ab ({expiry})")
    return _make_result("ok", f"Gültig noch {days} Tage (bis {expiry})")


# ── Check 6: smime_key ────────────────────────────────────────────────────────

def _has_local_key(email: str) -> bool:
    """True if this mailbox's signing key is stored locally (cert.pem/key.pem
    or a key.pem.bak backup) — i.e. it is NOT Key-Vault-backed. Key Vault being
    configured globally (KEYVAULT_URL set) does not mean every S/MIME-active
    mailbox's key actually lives there; some may never have been migrated."""
    import smime_store
    try:
        return bool(smime_store.get_signing_paths(email, allow_backup=True))
    except Exception:
        return False


def _check_smime_key(email: str) -> dict:
    """Check whether a signing key is available (local, backup, or Key Vault)."""
    import smime_store
    try:
        paths = smime_store.get_signing_paths(email, allow_backup=True)
    except Exception as exc:
        return _make_result("error", f"Fehler: {exc}")

    if paths:
        cert_path, key_path = paths
        if key_path.name == "key.pem.bak":
            return _make_result("ok", "Backup-Schlüssel vorhanden")
        return _make_result("ok", "Lokaler Schlüssel vorhanden")

    # No local key — check Key Vault
    kv_url = (settings_store.get("KEYVAULT_URL") or "").strip()
    if kv_url:
        return _make_result("ok", "Key Vault")

    return _make_result("error", "Kein Schlüssel gefunden (lokal + Key Vault)")


# ── Check 7: kv_sign ─────────────────────────────────────────────────────────

async def _check_kv_sign(email: str) -> dict:
    """Test the Key Vault sign API with a health-check digest."""
    import keyvault
    import smime_store
    from cms_sign import _get_cert_key_type, _parse_cert_der
    try:
        digest = hashlib.sha256(b"health-check").digest()
        # Determine algorithm from cert key type (EC→ES256, RSA→RS256)
        algo = "RS256"
        try:
            cert_path = smime_store.get_signing_cert_path(email)
            if cert_path and cert_path.exists():
                cert_pem = cert_path.read_bytes()
                cert_der = _parse_cert_der(cert_pem)
                algo = "ES256" if _get_cert_key_type(cert_der) == "EC" else "RS256"
        except Exception:
            pass
        await keyvault.sign(email, digest, algorithm=algo)
        return _make_result("ok", "Sign API erreichbar")
    except Exception as exc:
        return _make_result("error", f"Sign API Fehler: {exc}")


# ── Overall status ────────────────────────────────────────────────────────────

def _compute_overall(checks: dict) -> str:
    statuses = [v.get("status", "skip") for v in checks.values()]
    if "error" in statuses:
        return "error"
    if "warn" in statuses or "fixed" in statuses:
        return "warn"
    return "ok"


# ── Single mailbox check ──────────────────────────────────────────────────────

async def run_checks_for_mailbox(email: str, exo_data: dict | None = None) -> dict:
    """
    Run all checks for a single mailbox.
    exo_data: pre-fetched result from _check_exo_batch_sync (avoids duplicate PS sessions).
    Updates settings_store under MAILBOX_HEALTH[email].
    Returns the check result dict for this mailbox.
    """
    import mailbox_match
    email = email.lower().strip()
    mailbox_config: dict = settings_store.get("MAILBOX_CONFIG") or {}
    cfg = mailbox_match.match_sender(mailbox_config, email)
    reinject_mode = settings_store.get("REINJECT_MODE") or "smtp"
    kv_url = (settings_store.get("KEYVAULT_URL") or "").strip()
    smime_active = cfg.get("smime_sign") or cfg.get("smime_encrypt") or cfg.get("smime")

    checks: dict = {}

    # 1. exo_mailbox (sync, wrapped in executor)
    loop = asyncio.get_event_loop()
    checks["exo_mailbox"] = await loop.run_in_executor(None, _check_exo_mailbox_sync, email)

    # 2+3. dg_member + imap_permission — use pre-fetched data if available
    if exo_data is not None:
        data = exo_data.get(email, {})
        err = data.get("error")
        if err:
            checks["dg_member"] = _make_result("error", f"PS-Fehler: {err}")
            checks["imap_permission"] = _make_result("skip", "PS-Fehler")
        else:
            dg_ok = data.get("dg_member", False)
            dg_fixed = data.get("dg_fixed", False)
            if dg_fixed:
                checks["dg_member"] = _make_result("fixed", "Fehlte — automatisch hinzugefügt")
                _append_audit("dg_member_added", email, "Automatisch zur DG hinzugefügt")
            elif dg_ok:
                checks["dg_member"] = _make_result("ok", "Mitglied der Distribution Group")
            else:
                checks["dg_member"] = _make_result("error", "Nicht in Distribution Group")

            if reinject_mode == "imap":
                imap_perm = data.get("imap_perm")
                imap_fixed = data.get("imap_fixed", False)
                if imap_fixed:
                    checks["imap_permission"] = _make_result(
                        "fixed", "FullAccess fehlte — automatisch gesetzt")
                    _append_audit("imap_permission_set", email, "FullAccess automatisch gesetzt")
                elif imap_perm is True:
                    checks["imap_permission"] = _make_result("ok", "FullAccess vorhanden")
                elif imap_perm is False:
                    checks["imap_permission"] = _make_result("error", "FullAccess fehlt")
                else:
                    checks["imap_permission"] = _make_result("skip", "SP nicht gefunden")
            else:
                checks["imap_permission"] = _make_result("skip", "REINJECT_MODE != imap")
    else:
        # Run PS for single mailbox (fallback when called directly)
        batch_result = await loop.run_in_executor(None, _check_exo_batch_sync, [email])
        data = batch_result.get(email, {})
        err = data.get("error")
        if err:
            checks["dg_member"] = _make_result("error", f"PS-Fehler: {err}")
            checks["imap_permission"] = _make_result("skip", "PS-Fehler")
        else:
            dg_ok = data.get("dg_member", False)
            dg_fixed = data.get("dg_fixed", False)
            if dg_fixed:
                checks["dg_member"] = _make_result("fixed", "Fehlte — automatisch hinzugefügt")
                _append_audit("dg_member_added", email, "Automatisch zur DG hinzugefügt")
            elif dg_ok:
                checks["dg_member"] = _make_result("ok", "Mitglied der Distribution Group")
            else:
                checks["dg_member"] = _make_result("error", "Nicht in Distribution Group")

            if reinject_mode == "imap":
                imap_perm = data.get("imap_perm")
                imap_fixed = data.get("imap_fixed", False)
                if imap_fixed:
                    checks["imap_permission"] = _make_result(
                        "fixed", "FullAccess fehlte — automatisch gesetzt")
                    _append_audit("imap_permission_set", email, "FullAccess automatisch gesetzt")
                elif imap_perm is True:
                    checks["imap_permission"] = _make_result("ok", "FullAccess vorhanden")
                elif imap_perm is False:
                    checks["imap_permission"] = _make_result("error", "FullAccess fehlt")
                else:
                    checks["imap_permission"] = _make_result("skip", "SP nicht gefunden")
            else:
                checks["imap_permission"] = _make_result("skip", "REINJECT_MODE != imap")

    # 4. template
    if cfg.get("sig"):
        checks["template"] = await _check_template(email, cfg)
    else:
        checks["template"] = _make_result("skip", "Signatur nicht aktiviert")

    # 5. smime_cert
    if smime_active:
        checks["smime_cert"] = _check_smime_cert(email)
    else:
        checks["smime_cert"] = _make_result("skip", "S/MIME nicht aktiviert")

    # 6. smime_key
    if smime_active:
        checks["smime_key"] = _check_smime_key(email)
    else:
        checks["smime_key"] = _make_result("skip", "S/MIME nicht aktiviert")

    # 7. kv_sign — kv_url configured GLOBALLY doesn't mean THIS mailbox's key
    # lives in Key Vault; some mailboxes may still have a purely local key
    # (never migrated). Only attempt the KV sign-test if there's no local key.
    if kv_url and smime_active and not _has_local_key(email):
        checks["kv_sign"] = await _check_kv_sign(email)
    elif kv_url and smime_active:
        checks["kv_sign"] = _make_result("skip", "Lokaler Schlüssel — nicht in Key Vault")
    else:
        checks["kv_sign"] = _make_result("skip", "Key Vault nicht konfiguriert oder S/MIME inaktiv")

    overall = _compute_overall(checks)
    result = {
        "last_checked": _now_str(),
        "overall": overall,
        "checks": checks,
    }

    # Persist to settings_store
    health: dict = dict(settings_store.get("MAILBOX_HEALTH") or {})
    health[email] = result
    settings_store.force_update({"MAILBOX_HEALTH": health})

    log.info("health_check: %s → overall=%s", email, overall)
    return result


# ── All mailboxes ─────────────────────────────────────────────────────────────

async def run_all_checks(emails: list[str] | None = None) -> dict:
    """
    Run checks for all configured mailboxes (or given list).
    Uses a single PS session for EXO batch checks (efficiency).
    Updates settings_store. Returns {email: result_dict}.
    """
    import mailbox_match
    mailbox_config: dict = settings_store.get("MAILBOX_CONFIG") or {}
    if emails is None:
        emails = mailbox_match.configured_addresses(mailbox_config)
    if not emails:
        log.info("health_check: no mailboxes configured — skipping")
        return {}

    log.info("health_check: running checks for %d mailbox(es): %s",
             len(emails), ", ".join(emails))

    # Run the EXO batch PS check once for all emails
    loop = asyncio.get_event_loop()
    exo_data = await loop.run_in_executor(None, _check_exo_batch_sync, emails)

    results = {}
    for email in emails:
        try:
            results[email] = await run_checks_for_mailbox(email, exo_data=exo_data)
        except Exception as exc:
            log.error("health_check: error for %s: %s", email, exc)
            results[email] = {
                "last_checked": _now_str(),
                "overall": "error",
                "checks": {"exo_mailbox": _make_result("error", str(exc))},
            }

    return results
