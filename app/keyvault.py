"""
Azure Key Vault REST client for S/MIME private key storage.

Private keys are stored in Key Vault; the Sign API is called per-message
so the raw key material never leaves Azure.

Key naming: smime-{email with @→-at-, .→-, _→-}[:127]
Required role: "Key Vault Crypto Officer" on the vault (superset of Crypto User; needed for key import).
"""

import base64
import logging
import re
import time

import httpx

import settings_store
import stats

# Short-lived ARM tokens for logged-in users (delegated — lets them list/create their own resources)
# upn → (access_token, expires_at_monotonic)
_arm_user_tokens: dict[str, tuple[str, float]] = {}


def store_user_arm_token(upn: str, token: str, expires_in: int = 3600) -> None:
    _arm_user_tokens[upn.lower()] = (token, time.monotonic() + expires_in - 60)


def get_user_arm_token(upn: str) -> str | None:
    entry = _arm_user_tokens.get((upn or "").lower())
    if entry and time.monotonic() < entry[1]:
        return entry[0]
    _arm_user_tokens.pop((upn or "").lower(), None)
    return None

log = logging.getLogger(__name__)

_KV_API = "7.4"


def _email_to_key_name(email: str) -> str:
    """Convert email address to a Key Vault key name (max 127 chars)."""
    safe = email.lower().strip()
    safe = safe.replace("@", "-at-")
    safe = re.sub(r"[._]", "-", safe)
    # Key Vault key names: alphanumeric and hyphens only
    safe = re.sub(r"[^a-z0-9-]", "-", safe)
    # Collapse multiple hyphens
    safe = re.sub(r"-{2,}", "-", safe).strip("-")
    return ("smime-" + safe)[:127]


def is_configured() -> bool:
    """True if KEYVAULT_URL is set in settings."""
    return bool((settings_store.get("KEYVAULT_URL") or "").strip())


def vault_url() -> str:
    """Return the Key Vault URL (stripped of trailing slash)."""
    return (settings_store.get("KEYVAULT_URL") or "").strip().rstrip("/")


async def _get_kv_token(kv_url: str | None = None) -> str | None:
    """Acquire an access token for Key Vault using the MSAL app."""
    try:
        import graph_client
        app = graph_client._get_msal_app()
        if not app:
            log.warning("keyvault: MSAL app not configured — cannot acquire KV token")
            return None
        scope = ["https://vault.azure.net/.default"]
        result = app.acquire_token_silent(scope, account=None)
        if not result:
            result = app.acquire_token_for_client(scopes=scope)
        if "access_token" in result:
            return result["access_token"]
        log.error("keyvault: token acquisition failed: %s",
                  result.get("error_description", result.get("error")))
        return None
    except Exception as exc:
        log.error("keyvault: _get_kv_token error: %s", exc)
        return None


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


async def key_exists(email: str) -> bool:
    """Return True if an S/MIME key for this email exists in Key Vault."""
    if not is_configured():
        return False
    key_name = _email_to_key_name(email)
    token = await _get_kv_token()
    if not token:
        return False
    url = f"{vault_url()}/keys/{key_name}?api-version={_KV_API}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code == 200:
            return True
        if resp.status_code in (404, 403):
            return False
        log.warning("keyvault key_exists: unexpected status %s for %s", resp.status_code, email)
        return False
    except Exception as exc:
        log.error("keyvault key_exists error for %s: %s", email, exc)
        return False


def _kv_exportable() -> bool:
    """Return True when KV_KEY_MODE is 'fallback' (key is importable back from KV)."""
    import settings_store as _ss
    return _ss.get("KV_KEY_MODE") != "strict"


async def import_rsa_key(email: str, private_key_pem: bytes) -> str:
    """
    Import an RSA or EC private key from PEM into Key Vault.
    Returns the key ID (URL) on success.
    Raises on failure.
    """
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
    from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey, SECP256R1, SECP384R1

    # Decrypt key (try with current password, then without)
    import config as _config
    import settings_store as _ss
    key = None
    pw = _ss.get("SMIME_KEY_PASSWORD") or _config.SMIME_KEY_PASSWORD or ""
    for candidate in ([pw.encode()] if pw else []) + [None]:
        try:
            key = load_pem_private_key(private_key_pem, password=candidate)
            break
        except Exception:
            continue
    if key is None:
        raise ValueError("Privater Schlüssel konnte nicht geladen werden (falsches Passwort?)")

    if isinstance(key, RSAPrivateKey):
        kty = "RSA"
        priv = key.private_numbers()
        pub = priv.public_numbers

        def _bi(n: int) -> str:
            """Big integer to base64url-encoded bytes (big-endian, minimal length)."""
            length = (n.bit_length() + 7) // 8
            return _b64url_encode(n.to_bytes(length, "big"))

        jwk = {
            "kty": kty,
            "key_ops": ["sign", "verify", "decrypt", "unwrapKey"],
            "n": _bi(pub.n),
            "e": _bi(pub.e),
            "d": _bi(priv.d),
            "p": _bi(priv.p),
            "q": _bi(priv.q),
            "dp": _bi(priv.dmp1),
            "dq": _bi(priv.dmq1),
            "qi": _bi(priv.iqmp),
        }
        key_attrs = {"exportable": _kv_exportable()}

    elif isinstance(key, EllipticCurvePrivateKey):
        curve = key.curve
        if isinstance(curve, SECP256R1):
            crv = "P-256"
        elif isinstance(curve, SECP384R1):
            crv = "P-384"
        else:
            raise ValueError(f"Nicht unterstützte EC-Kurve: {curve.name}")
        priv_nums = key.private_numbers()
        pub_nums = priv_nums.public_numbers
        coord_size = (key.key_size + 7) // 8

        def _coord(n: int) -> str:
            return _b64url_encode(n.to_bytes(coord_size, "big"))

        jwk = {
            "kty": "EC",
            "crv": crv,
            "key_ops": ["sign", "verify", "decrypt", "unwrapKey"],
            "x": _coord(pub_nums.x),
            "y": _coord(pub_nums.y),
            "d": _coord(priv_nums.private_value),
        }
        key_attrs = {"exportable": _kv_exportable()}
    else:
        raise ValueError(f"Nicht unterstützter Schlüsseltyp: {type(key).__name__}")

    key_name = _email_to_key_name(email)
    token = await _get_kv_token()
    if not token:
        raise RuntimeError("Key Vault Token konnte nicht abgerufen werden")

    url = f"{vault_url()}/keys/{key_name}/import?api-version={_KV_API}"
    _full_ops = ["sign", "verify", "decrypt", "unwrapKey"]
    payload = {"key": jwk, "key_ops": _full_ops, "attributes": key_attrs}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.put(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )

    if resp.status_code not in (200, 201):
        body = resp.text[:500]
        # AKV.SKR.1003: exportable attribute of an existing key cannot be changed.
        # Retry without the exportable flag so the existing attribute is preserved.
        if resp.status_code == 400 and "AKV.SKR.1003" in body:
            log.warning(
                "keyvault: existing key has different exportable setting for %s — "
                "retrying without changing the attribute", email
            )
            payload_retry = {"key": jwk, "key_ops": ["sign", "verify"], "attributes": {}}
            async with httpx.AsyncClient(timeout=30) as client2:
                resp = await client2.put(
                    url,
                    json=payload_retry,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                )
            if resp.status_code not in (200, 201):
                body = resp.text[:500]
                log.error("keyvault import_rsa_key retry failed (HTTP %s): %s", resp.status_code, body)
                raise RuntimeError(f"Key Vault Import fehlgeschlagen (HTTP {resp.status_code}): {body}")
        else:
            log.error("keyvault import_rsa_key failed (HTTP %s): %s", resp.status_code, body)
            raise RuntimeError(f"Key Vault Import fehlgeschlagen (HTTP {resp.status_code}): {body}")

    key_id = resp.json().get("key", {}).get("kid", "")
    log.info("keyvault: key imported for %s → %s", email, key_id)
    return key_id


async def patch_key_ops(email: str) -> bool:
    """Add decrypt + unwrapKey to an existing KV key that was imported without those ops."""
    key_name = _email_to_key_name(email)
    token = await _get_kv_token()
    if not token:
        return False
    url = f"{vault_url()}/keys/{key_name}?api-version={_KV_API}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(
                url,
                json={"key_ops": ["sign", "verify", "decrypt", "unwrapKey"]},
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
        if resp.status_code in (200, 201):
            log.info("keyvault: key_ops patched for %s (added decrypt/unwrapKey)", email)
            return True
        log.error("keyvault: patch_key_ops failed HTTP %s for %s: %s",
                  resp.status_code, email, resp.text[:300])
        return False
    except Exception as exc:
        log.error("keyvault: patch_key_ops error for %s: %s", email, exc)
        return False


async def sign(email: str, digest_bytes: bytes, algorithm: str = "RS256") -> bytes:
    """
    Sign digest_bytes using Key Vault Sign API.
    Returns raw signature bytes.
    Raises on failure.
    """
    key_name = _email_to_key_name(email)
    token = await _get_kv_token()
    if not token:
        raise RuntimeError("Key Vault Token konnte nicht abgerufen werden")

    url = f"{vault_url()}/keys/{key_name}/sign?api-version={_KV_API}"
    payload = {
        "alg": algorithm,
        "value": _b64url_encode(digest_bytes),
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )

    if resp.status_code != 200:
        body = resp.text[:500]
        log.error("keyvault sign failed (HTTP %s) for %s: %s", resp.status_code, email, body)
        raise RuntimeError(f"Key Vault Sign fehlgeschlagen (HTTP {resp.status_code}): {body}")

    b64 = resp.json().get("value", "")
    # Key Vault returns base64url-encoded signature
    raw = base64.urlsafe_b64decode(b64 + "==")
    stats.increment("kv_sign_calls")
    return raw


async def _get_arm_token() -> str | None:
    """Acquire an ARM management token."""
    try:
        import graph_client
        app = graph_client._get_msal_app()
        if not app:
            return None
        scope = ["https://management.azure.com/.default"]
        result = app.acquire_token_silent(scope, account=None)
        if not result:
            result = app.acquire_token_for_client(scopes=scope)
        if "access_token" in result:
            return result["access_token"]
        log.error("arm token acquisition failed: %s", result.get("error_description", ""))
        return None
    except Exception as exc:
        log.error("_get_arm_token error: %s", exc)
        return None


async def _get_sp_object_id(client_id: str) -> str | None:
    """Look up the service principal object ID for the given app client ID via Graph."""
    try:
        import graph_client
        token = graph_client._acquire_token()
        if not token:
            return None
        url = (
            "https://graph.microsoft.com/v1.0/servicePrincipals"
            f"?$filter=appId eq '{client_id}'&$select=id"
        )
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code == 200:
            items = resp.json().get("value", [])
            if items:
                return items[0].get("id")
        log.error("_get_sp_object_id: HTTP %s for client_id=%s", resp.status_code, client_id)
        return None
    except Exception as exc:
        log.error("_get_sp_object_id error: %s", exc)
        return None


async def list_subscriptions(arm_token: str | None = None) -> tuple[bool, str, list[dict]]:
    """List Azure subscriptions. Uses delegated user token if provided, else app SP."""
    arm_token = arm_token or await _get_arm_token()
    if not arm_token:
        return False, "ARM-Token nicht verfügbar", []
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.get(
                "https://management.azure.com/subscriptions?api-version=2022-12-01",
                headers={"Authorization": f"Bearer {arm_token}"},
            )
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}", []
        items = resp.json().get("value", [])
        subs = [{"id": s["subscriptionId"], "name": s["displayName"]} for s in items]
        return True, "", subs
    except Exception as exc:
        log.error("list_subscriptions error: %s", exc)
        return False, str(exc), []


async def list_resource_groups(subscription_id: str, arm_token: str | None = None) -> tuple[bool, str, list[dict]]:
    """List resource groups. Uses delegated user token if provided, else app SP."""
    arm_token = arm_token or await _get_arm_token()
    if not arm_token:
        return False, "ARM-Token nicht verfügbar", []
    try:
        url = (
            f"https://management.azure.com/subscriptions/{subscription_id}"
            "/resourceGroups?api-version=2022-12-01&$orderby=name"
        )
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.get(url, headers={"Authorization": f"Bearer {arm_token}"})
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}", []
        items = resp.json().get("value", [])
        rgs = [{"name": rg["name"], "location": rg["location"]} for rg in items]
        return True, "", rgs
    except Exception as exc:
        log.error("list_resource_groups error: %s", exc)
        return False, str(exc), []


async def list_vaults(subscription_id: str, arm_token: str | None = None) -> tuple[bool, str, list[dict]]:
    """List Key Vaults in a subscription. Uses delegated user token if provided, else app SP."""
    arm_token = arm_token or await _get_arm_token()
    if not arm_token:
        return False, "ARM-Token nicht verfügbar", []
    try:
        url = (
            f"https://management.azure.com/subscriptions/{subscription_id}"
            "/providers/Microsoft.KeyVault/vaults?api-version=2023-07-01"
        )
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.get(url, headers={"Authorization": f"Bearer {arm_token}"})
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}", []
        items = resp.json().get("value", [])
        vaults = [
            {
                "name": v["name"],
                "uri": v.get("properties", {}).get("vaultUri", f"https://{v['name']}.vault.azure.net").rstrip("/"),
                "resource_id": v.get("id", ""),
            }
            for v in items
        ]
        return True, "", vaults
    except Exception as exc:
        log.error("list_vaults error: %s", exc)
        return False, str(exc), []


# Key Vault Crypto Officer — superset of Crypto User; required for key import/create/delete
_KV_CRYPTO_OFFICER_ROLE = "14b46e9e-c2b0-4bf8-b336-e8a77c82bb72"


async def create_vault(
    subscription_id: str,
    resource_group: str,
    vault_name: str,
    location: str,
    tenant_id: str,
    client_id: str,
    create_rg: bool = False,
    arm_token: str | None = None,
) -> tuple[bool, str, str]:
    """
    Create an Azure Key Vault and assign Key Vault Crypto Officer role to the app SP.
    Crypto Officer is the superset of Crypto User and additionally allows key import/create/delete.
    Uses delegated user token if provided (no Contributor role needed on app SP),
    else falls back to app SP client credentials.
    Returns (ok, message, vault_url).
    """
    import uuid as _uuid

    arm_token = arm_token or await _get_arm_token()
    if not arm_token:
        return (
            False,
            "ARM-Token konnte nicht abgerufen werden — prüfe ob die App-Registrierung "
            "Contributor-Rechte auf die Subscription oder Resource Group hat.",
            "",
        )

    arm_base = "https://management.azure.com"
    vault_url_result = f"https://{vault_name}.vault.azure.net"

    # Optionally create the resource group first
    if create_rg:
        rg_put_url = (
            f"{arm_base}/subscriptions/{subscription_id}"
            f"/resourceGroups/{resource_group}?api-version=2022-12-01"
        )
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.put(
                rg_put_url,
                json={"location": location},
                headers={"Authorization": f"Bearer {arm_token}", "Content-Type": "application/json"},
            )
        if resp.status_code not in (200, 201):
            try:
                err = resp.json().get("error", {}).get("message", resp.text[:400])
            except Exception:
                err = resp.text[:400]
            log.error("create_vault: RG creation failed (HTTP %s): %s", resp.status_code, err)
            return False, f"Resource Group konnte nicht erstellt werden (HTTP {resp.status_code}): {err}", ""
        log.info("keyvault: resource group created/confirmed: %s", resource_group)

    # Create the vault
    vault_put_url = (
        f"{arm_base}/subscriptions/{subscription_id}"
        f"/resourceGroups/{resource_group}"
        f"/providers/Microsoft.KeyVault/vaults/{vault_name}"
        "?api-version=2023-07-01"
    )
    vault_body = {
        "location": location,
        "properties": {
            "sku": {"family": "A", "name": "standard"},
            "tenantId": tenant_id,
            "enableRbacAuthorization": True,
            "softDeleteRetentionInDays": 7,
        },
    }

    async with httpx.AsyncClient(timeout=60) as c:
        resp = await c.put(
            vault_put_url,
            json=vault_body,
            headers={"Authorization": f"Bearer {arm_token}", "Content-Type": "application/json"},
        )

    if resp.status_code not in (200, 201):
        try:
            err = resp.json().get("error", {}).get("message", resp.text[:400])
        except Exception:
            err = resp.text[:400]
        log.error("create_vault PUT failed (HTTP %s): %s", resp.status_code, err)
        return False, f"Key Vault konnte nicht erstellt werden (HTTP {resp.status_code}): {err}", ""

    log.info("keyvault: vault created: %s", vault_url_result)

    kv_resource_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.KeyVault/vaults/{vault_name}"
    )

    # Get SP object ID for role assignment
    sp_object_id = await _get_sp_object_id(client_id)
    if not sp_object_id:
        return (
            False,
            (
                f"Key Vault wurde erstellt, aber die Rollenzuweisung schlug fehl — "
                f"Service Principal für App {client_id} nicht gefunden. "
                f"Bitte im Azure Portal die Rolle 'Key Vault Crypto Officer' manuell zuweisen, "
                f"oder hier erneut versuchen."
            ),
            vault_url_result,
            kv_resource_id,
        )

    # Assign Key Vault Crypto Officer role (superset: allows sign + key import/create/delete)
    role_def_id = (
        f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization"
        f"/roleDefinitions/{_KV_CRYPTO_OFFICER_ROLE}"
    )
    assignment_id = str(_uuid.uuid4())
    role_url = (
        f"{arm_base}{kv_resource_id}/providers/Microsoft.Authorization"
        f"/roleAssignments/{assignment_id}?api-version=2022-04-01"
    )
    role_body = {
        "properties": {
            "roleDefinitionId": role_def_id,
            "principalId": sp_object_id,
            "principalType": "ServicePrincipal",
        }
    }

    async with httpx.AsyncClient(timeout=30) as c:
        resp = await c.put(
            role_url,
            json=role_body,
            headers={"Authorization": f"Bearer {arm_token}", "Content-Type": "application/json"},
        )

    if resp.status_code not in (200, 201):
        try:
            err = resp.json().get("error", {}).get("message", resp.text[:400])
        except Exception:
            err = resp.text[:400]
        log.error("create_vault role assignment failed (HTTP %s): %s", resp.status_code, err)
        perm_hint = (
            " Hinweis: Für die Rollenzuweisung selbst reicht die Rolle 'Contributor' NICHT — "
            "das angemeldete Azure-Konto braucht 'Owner' oder 'User Access Administrator' "
            "auf der Subscription/Resource Group, um andere Rollen zuweisen zu dürfen."
            if resp.status_code == 403 else ""
        )
        return (
            False,
            (
                f"Key Vault erstellt, aber Rollenzuweisung fehlgeschlagen "
                f"(HTTP {resp.status_code}): {err}. "
                f"Bitte im Azure Portal die Rolle 'Key Vault Crypto Officer' "
                f"für die App-Registrierung manuell zuweisen, oder hier erneut versuchen."
                f"{perm_hint}"
            ),
            vault_url_result,
            kv_resource_id,
        )

    log.info("keyvault: Crypto Officer role assigned (SP %s) on vault %s", sp_object_id, vault_name)
    return True, f"Key Vault '{vault_name}' erstellt und Rolle zugewiesen.", vault_url_result, kv_resource_id


async def ensure_crypto_officer_role(
    kv_resource_id: str,
    client_id: str,
    arm_token: str | None = None,
) -> tuple[bool, str]:
    """
    Idempotently assign Key Vault Crypto Officer role to the app SP on the given vault.
    kv_resource_id: full ARM resource path, e.g.
      /subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.KeyVault/vaults/{name}
    Uses a deterministic role-assignment UUID (scope+role+principal) so re-running is safe.
    Returns (ok, message).
    """
    import re as _re
    import uuid as _uuid

    arm_token = arm_token or await _get_arm_token()
    if not arm_token:
        return False, "ARM-Token nicht verfügbar — bitte oben 'Azure-Zugriff holen' klicken."

    # Extract subscription_id from resource path
    m = _re.match(r"/subscriptions/([^/]+)/", kv_resource_id)
    if not m:
        return False, f"Ungültige Vault Resource-ID: {kv_resource_id}"
    subscription_id = m.group(1)

    sp_object_id = await _get_sp_object_id(client_id)
    if not sp_object_id:
        return False, f"Service Principal für App {client_id} nicht gefunden."

    # Deterministic UUID: same scope+role+principal always yields the same assignment ID
    assignment_id = str(_uuid.uuid5(
        _uuid.NAMESPACE_URL,
        f"{kv_resource_id}:{_KV_CRYPTO_OFFICER_ROLE}:{sp_object_id}",
    ))
    role_url = (
        f"https://management.azure.com{kv_resource_id}/providers/Microsoft.Authorization"
        f"/roleAssignments/{assignment_id}?api-version=2022-04-01"
    )

    try:
        async with httpx.AsyncClient(timeout=30) as c:
            # Look up the role definition by name to get the canonical ARM resource ID.
            # Avoids issues with subscription-scoped vs. global paths and hardcoded GUIDs.
            import urllib.parse as _urlparse
            lookup_url = (
                f"https://management.azure.com/subscriptions/{subscription_id}"
                f"/providers/Microsoft.Authorization/roleDefinitions"
                f"?$filter=roleName+eq+%27Key+Vault+Crypto+Officer%27"
                f"&api-version=2022-04-01"
            )
            lr = await c.get(lookup_url, headers={"Authorization": f"Bearer {arm_token}"})
            if lr.status_code == 200 and lr.json().get("value"):
                role_def_id = lr.json()["value"][0]["id"]
                log.info("keyvault: role def found via lookup: %s", role_def_id)
            else:
                # Fall back to constructed path
                role_def_id = (
                    f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization"
                    f"/roleDefinitions/{_KV_CRYPTO_OFFICER_ROLE}"
                )
                log.warning(
                    "keyvault: role def lookup failed (HTTP %s), using hardcoded path: %s",
                    lr.status_code, role_def_id,
                )

            role_body = {
                "properties": {
                    "roleDefinitionId": role_def_id,
                    "principalId": sp_object_id,
                    "principalType": "ServicePrincipal",
                }
            }
            log.info(
                "keyvault: assigning role SP=%s vault=%s role_def=%s",
                sp_object_id, kv_resource_id, role_def_id,
            )
            resp = await c.put(
                role_url,
                json=role_body,
                headers={"Authorization": f"Bearer {arm_token}", "Content-Type": "application/json"},
            )
        if resp.status_code in (200, 201):
            log.info("keyvault: Crypto Officer role ensured (SP %s) on %s", sp_object_id, kv_resource_id)
            return True, "Rolle 'Key Vault Crypto Officer' erfolgreich zugewiesen."
        # 409 = assignment with this ID already exists with same data → idempotent success
        if resp.status_code == 409:
            return True, "Rolle 'Key Vault Crypto Officer' ist bereits zugewiesen."
        try:
            err = resp.json().get("error", {}).get("message", resp.text[:300])
        except Exception:
            err = resp.text[:300]
        log.error("keyvault: role assignment failed HTTP %s: %s", resp.status_code, resp.text[:400])
        perm_hint = (
            " Hinweis: Für die Rollenzuweisung selbst reicht die Rolle 'Contributor' NICHT — "
            "das angemeldete Azure-Konto braucht 'Owner' oder 'User Access Administrator' "
            "auf der Subscription/Resource Group, um andere Rollen zuweisen zu dürfen."
            if resp.status_code == 403 else ""
        )
        return False, f"Rollenzuweisung fehlgeschlagen (HTTP {resp.status_code}): {err}{perm_hint}"
    except Exception as exc:
        log.error("ensure_crypto_officer_role error: %s", exc)
        return False, str(exc)


async def test_connection(kv_url: str | None = None) -> tuple[bool, str]:
    """
    Test Key Vault connectivity by listing keys (maxresults=1).
    Returns (ok: bool, message: str).
    """
    url_to_test = (kv_url or vault_url()).strip().rstrip("/")
    if not url_to_test:
        return False, "Keine Key Vault URL konfiguriert"

    token = await _get_kv_token()
    if not token:
        return False, "Azure-Token konnte nicht abgerufen werden — Entra App-Registrierung prüfen"

    test_url = f"{url_to_test}/keys?api-version={_KV_API}&maxresults=1"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                test_url,
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code == 200:
            count = len(resp.json().get("value", []))
            return True, f"Verbindung erfolgreich — {count} Schlüssel sichtbar"
        if resp.status_code == 403:
            return False, (
                "Zugriff verweigert (403). Die App-Registrierung braucht die Rolle "
                "'Key Vault Crypto Officer' auf dem Vault "
                "(Azure Portal → Key Vault → Zugriffssteuerung (IAM) → Rollenzuweisung hinzufügen)."
            )
        if resp.status_code == 404:
            return False, f"Key Vault nicht gefunden (404) — URL prüfen: {url_to_test}"
        return False, f"Unerwarteter Status {resp.status_code}: {resp.text[:200]}"
    except httpx.ConnectError:
        return False, f"Verbindung fehlgeschlagen — URL erreichbar? {url_to_test}"
    except Exception as exc:
        return False, f"Fehler: {exc}"
