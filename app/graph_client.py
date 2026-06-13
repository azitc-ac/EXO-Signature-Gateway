import logging
from dataclasses import dataclass, field

import httpx
import msal

import config

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


def _get_msal_app() -> msal.ConfidentialClientApplication:
    global _msal_app
    if _msal_app is None:
        authority = f"https://login.microsoftonline.com/{config.TENANT_ID}"
        _msal_app = msal.ConfidentialClientApplication(
            config.CLIENT_ID,
            authority=authority,
            client_credential=config.CLIENT_SECRET,
        )
    return _msal_app


def _acquire_token() -> str | None:
    app = _get_msal_app()
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

    return UserData(
        displayName=data.get("displayName") or "",
        jobTitle=data.get("jobTitle") or "",
        department=data.get("department") or "",
        companyName=data.get("companyName") or "",
        mail=data.get("mail") or email,
        mobilePhone=data.get("mobilePhone") or "",
        phone=phones[0] if phones else "",
        officeLocation=data.get("officeLocation") or "",
        website=data.get("businessHomePage") or ext.get("extensionAttribute1") or "",
        bookingsUrl=ext.get("extensionAttribute2") or "",
    )
