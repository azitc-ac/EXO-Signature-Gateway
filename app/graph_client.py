import logging
import urllib.parse
from dataclasses import dataclass

import httpx
import msal

import config
import settings_store

log = logging.getLogger(__name__)

_SCOPES = ["https://graph.microsoft.com/.default"]
_SELECT_FIELDS = ",".join([
    "displayName",
    "jobTitle",
    "department",
    "companyName",
    "mail",
    "mobilePhone",
    "businessPhones",
    "officeLocation",
    "businessHomePage",
    "onPremisesExtensionAttributes",
])

_msal_app: msal.ConfidentialClientApplication | None = None
_msal_credentials: tuple[str, str, str] | None = None  # (tenant, client, secret)


def _get_effective_credentials() -> tuple[str, str, str]:
    """Return (tenant_id, client_id, client_secret) — env vars override settings."""
    tenant = config.TENANT_ID or settings_store.get("TENANT_ID") or ""
    client = config.CLIENT_ID or settings_store.get("CLIENT_ID") or ""
    secret = config.CLIENT_SECRET or settings_store.get("CLIENT_SECRET") or ""
    return tenant, client, secret


def _get_msal_app() -> msal.ConfidentialClientApplication | None:
    global _msal_app, _msal_credentials
    tenant, client, secret = _get_effective_credentials()
    if not (tenant and client and secret):
        return None
    creds = (tenant, client, secret)
    if _msal_app is None or _msal_credentials != creds:
        authority = f"https://login.microsoftonline.com/{tenant}"
        _msal_app = msal.ConfidentialClientApplication(
            client,
            authority=authority,
            client_credential=secret,
        )
        _msal_credentials = creds
    return _msal_app


def reset_msal_app() -> None:
    """Force MSAL app re-creation on next call (call after credentials change)."""
    global _msal_app, _msal_credentials
    _msal_app = None
    _msal_credentials = None


def _acquire_token() -> str | None:
    app = _get_msal_app()
    if not app:
        log.warning("Graph credentials not configured — skipping token acquisition")
        return None
    result = app.acquire_token_silent(_SCOPES, account=None)
    if not result:
        result = app.acquire_token_for_client(scopes=_SCOPES)
    if "access_token" in result:
        return result["access_token"]
    log.error("Failed to acquire Graph token: %s", result.get("error_description"))
    return None


@dataclass
class UserData:
    displayName: str = ""
    jobTitle: str = ""
    department: str = ""
    companyName: str = ""
    mail: str = ""
    mobilePhone: str = ""
    phone: str = ""
    officeLocation: str = ""
    website: str = ""
    bookingsUrl: str = ""


async def update_sent_item(sender_email: str, message_id: str, html_body: str) -> bool:
    """Find a sent message by Message-ID and patch its HTML body."""
    token = _acquire_token()
    if not token:
        return False

    mid = message_id.strip()
    if not mid.startswith("<"):
        mid = f"<{mid}>"
    search_filter = urllib.parse.quote(f"internetMessageId eq '{mid}'")
    search_url = (
        f"https://graph.microsoft.com/v1.0/users/{sender_email}"
        f"/mailFolders/sentitems/messages?$filter={search_filter}&$select=id&$top=1"
    )
    auth = {"Authorization": f"Bearer {token}"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(search_url, headers=auth)
            resp.raise_for_status()
            items = resp.json().get("value", [])

        if not items:
            return False

        msg_graph_id = items[0]["id"]
        patch_url = f"https://graph.microsoft.com/v1.0/users/{sender_email}/messages/{msg_graph_id}"

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(
                patch_url,
                headers={**auth, "Content-Type": "application/json"},
                json={"body": {"contentType": "html", "content": html_body}},
            )
            resp.raise_for_status()

        log.info("Sent item updated for %s (message-id %s)", sender_email, message_id)
        return True

    except Exception as exc:
        log.error("update_sent_item failed for %s: %s", sender_email, exc)
        return False


async def get_user(email: str) -> UserData:
    token = _acquire_token()
    if not token:
        return UserData(mail=email)

    url = f"https://graph.microsoft.com/v1.0/users/{email}?$select={_SELECT_FIELDS}"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)

        if resp.status_code == 404:
            log.warning("Graph: user not found: %s", email)
            return UserData(mail=email)

        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.error("Graph API error for %s: %s", email, exc)
        return UserData(mail=email)

    ext = data.get("onPremisesExtensionAttributes") or {}
    phones = data.get("businessPhones") or []
    resolved_mail = data.get("mail") or email

    user_websites: dict = settings_store.get("USER_WEBSITES") or {}
    user_bookings: dict = settings_store.get("USER_BOOKINGS") or {}

    return UserData(
        displayName=data.get("displayName") or "",
        jobTitle=data.get("jobTitle") or "",
        department=data.get("department") or "",
        companyName=data.get("companyName") or "",
        mail=resolved_mail,
        mobilePhone=data.get("mobilePhone") or "",
        phone=phones[0] if phones else "",
        officeLocation=data.get("officeLocation") or "",
        website=user_websites.get(resolved_mail.lower()) or data.get("businessHomePage") or ext.get("extensionAttribute1") or "",
        bookingsUrl=user_bookings.get(resolved_mail.lower()) or ext.get("extensionAttribute2") or "",
    )
