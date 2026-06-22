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


async def update_sent_item(sender_email: str, message_id: str, html_body: str) -> bool | None:
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
            if 400 <= resp.status_code < 500:
                log.warning(
                    "Sent item PATCH %d for %s (not retrying): %s",
                    resp.status_code, sender_email, resp.text[:300],
                )
                return None  # permanent client error — caller must not retry
            resp.raise_for_status()

        log.info("Sent item updated for %s (message-id %s)", sender_email, message_id)
        return True

    except Exception as exc:
        log.error("update_sent_item failed for %s: %s", sender_email, exc)
        return False


async def cleanup_sent_items(
    sender_email: str, message_id: str, html_body: str
) -> bool | None:
    """Find all Sent Items with this Message-ID and clean up duplicates.

    Exchange creates a Sent Item when the user sends mail AND another when our
    gateway calls sendMail.  This function:

    - If multiple items found: deletes all but the newest (the one from our
      sendMail, which already has the signed MIME body).
    - If only one item found: patches it with html_body (covers SMTP reinject
      mode where sendMail never runs, and the early-timing edge case in Graph
      mode before the sendMail Sent Item has appeared — the caller retries with
      back-off so the next pass can still delete it once it shows up).

    Returns True on success, None on a permanent 4xx client error, False on
    transient failure / not-found (caller should retry).
    """
    token = _acquire_token()
    if not token:
        return False

    mid = message_id.strip()
    if not mid.startswith("<"):
        mid = f"<{mid}>"
    search_filter = urllib.parse.quote(f"internetMessageId eq '{mid}'")
    search_url = (
        f"https://graph.microsoft.com/v1.0/users/{sender_email}"
        f"/mailFolders/sentitems/messages"
        f"?$filter={search_filter}&$select=id,createdDateTime&$top=10"
    )
    auth = {"Authorization": f"Bearer {token}"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(search_url, headers=auth)
            resp.raise_for_status()
            items = resp.json().get("value", [])

        if not items:
            return False

        # Sort oldest-first; createdDateTime is ISO 8601 so lexicographic order works
        items.sort(key=lambda x: x.get("createdDateTime", ""))

        if len(items) == 1:
            # Single item: patch its body (SMTP reinject mode, or Graph mode where
            # the sendMail Sent Item hasn't appeared yet → caller retries)
            item_id = items[0]["id"]
            patch_url = (
                f"https://graph.microsoft.com/v1.0/users/{sender_email}/messages/{item_id}"
            )
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.patch(
                    patch_url,
                    headers={**auth, "Content-Type": "application/json"},
                    json={"body": {"contentType": "html", "content": html_body}},
                )
                if 400 <= resp.status_code < 500:
                    log.warning(
                        "Sent item PATCH %d for %s (not retrying): %s",
                        resp.status_code, sender_email, resp.text[:300],
                    )
                    return None
                resp.raise_for_status()
            log.info("Sent item patched for %s (message-id %s)", sender_email, message_id)
            return True

        # Multiple items: the oldest are the original(s) from the mail client;
        # the newest is from our sendMail and already has the correct signed MIME body.
        # Delete the older duplicates.
        to_delete = items[:-1]
        async with httpx.AsyncClient(timeout=30) as client:
            for item in to_delete:
                del_url = (
                    f"https://graph.microsoft.com/v1.0/users/{sender_email}"
                    f"/messages/{item['id']}"
                )
                d = await client.delete(del_url, headers=auth)
                if d.is_success or d.status_code == 404:
                    log.info(
                        "Deleted original Sent Item for %s (created %s)",
                        sender_email, item.get("createdDateTime", "?"),
                    )
                else:
                    log.warning(
                        "Failed to delete Sent Item for %s: HTTP %d %s",
                        sender_email, d.status_code, d.text[:200],
                    )
        log.info(
            "Sent items cleaned for %s: %d original(s) removed", sender_email, len(to_delete)
        )
        return True

    except Exception as exc:
        log.error("cleanup_sent_items failed for %s: %s", sender_email, exc)
        return False


GRAPH = "https://graph.microsoft.com/v1.0"


async def list_mailboxes() -> list[dict]:
    """
    List all EXO mailboxes via Graph API: licensed users, shared mailboxes,
    and Microsoft 365 group mailboxes.
    Returns list of {"email", "name", "type"} dicts sorted by email.
    """
    token = _acquire_token()
    if not token:
        return []
    headers = {"Authorization": f"Bearer {token}"}
    results: list[dict] = []

    async with httpx.AsyncClient(timeout=30) as client:
        # ── User mailboxes (licensed = regular, unlicensed with mail = shared) ──
        url = (f"{GRAPH}/users"
               "?$select=mail,displayName,assignedLicenses,userType"
               "&$top=999")
        while url:
            r = await client.get(url, headers=headers)
            if r.status_code != 200:
                log.warning("list_mailboxes: /users returned %s", r.status_code)
                break
            data = r.json()
            for u in data.get("value", []):
                mail = (u.get("mail") or "").lower().strip()
                if not mail or u.get("userType") == "Guest":
                    continue
                has_license = bool(u.get("assignedLicenses"))
                results.append({
                    "email": mail,
                    "name": u.get("displayName") or mail,
                    "type": "user" if has_license else "shared",
                })
            url = data.get("@odata.nextLink")

        # ── Microsoft 365 group mailboxes ──────────────────────────────────────
        grp_url = (f"{GRAPH}/groups"
                   "?$filter=groupTypes/any(c:c+eq+'Unified')"
                   "&$select=mail,displayName"
                   "&$top=999")
        while grp_url:
            r = await client.get(grp_url, headers=headers)
            if r.status_code != 200:
                log.debug("list_mailboxes: /groups returned %s (Group.Read.All may be missing)",
                          r.status_code)
                break
            data = r.json()
            for g in data.get("value", []):
                mail = (g.get("mail") or "").lower().strip()
                if not mail:
                    continue
                results.append({
                    "email": mail,
                    "name": g.get("displayName") or mail,
                    "type": "group",
                })
            grp_url = data.get("@odata.nextLink")

    return sorted(results, key=lambda x: x["email"])


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
