"""
PKCE (Proof Key for Code Exchange) helpers for the Azure AD OAuth2 flow.

We use Microsoft Graph CLI App (14d82eec-204b-4c2f-b7e8-296a70dab67e) as the
bootstrap identity.  It is a Microsoft-owned public client that supports PKCE
without a client secret and is broadly allowed in tenant Conditional Access
policies.
"""

import base64
import hashlib
import logging
import secrets
import time
import urllib.parse

log = logging.getLogger(__name__)

# Fallback bootstrap client (Microsoft Graph CLI App) — may have redirect URI restrictions in some tenants.
# Prefer using a custom app registration via settings_store BOOTSTRAP_CLIENT_ID.
_FALLBACK_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"


def _get_client_id() -> str:
    import settings_store
    custom = (settings_store.get("BOOTSTRAP_CLIENT_ID") or "").strip()
    return custom if custom else _FALLBACK_CLIENT_ID

# Delegated scopes needed to create app registrations & grant admin consent
BOOTSTRAP_SCOPES = [
    "https://graph.microsoft.com/Application.ReadWrite.All",
    "https://graph.microsoft.com/AppRoleAssignment.ReadWrite.All",
    "https://graph.microsoft.com/Directory.ReadWrite.All",
    "https://graph.microsoft.com/RoleManagement.ReadWrite.Directory",
    "offline_access",
]

# Minimal scopes for SSO login (identity only) — KEIN Graph-Resource-Scope
# (User.Read/offline_access): der Login nutzt das Access-Token nicht, und ein
# Resource-Scope verschiebt einen MFA-Step-up in den Back-Channel, wo kein Dialog
# erscheinen kann. So wird MFA am interaktiven Sign-in ausgewertet. Siehe sso.py.
SSO_SCOPES = ["openid", "profile", "email"]

# Delegated ARM scope — lets the logged-in user access their Azure subscriptions
ARM_SCOPES = ["https://management.azure.com/user_impersonation", "offline_access"]

# In-memory session store: state → {verifier, created_at, redirect_uri, flow}
_sessions: dict[str, dict] = {}
_SESSION_TTL = 600  # 10 minutes


# ── PKCE helpers ──────────────────────────────────────────────────────────────

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge)."""
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


class InteractionRequired(RuntimeError):
    """Der Token-Endpunkt verlangt einen interaktiven Schritt — ein Conditional-
    Access-Step-up (typisch MFA, AADSTS50076). Der Fehler kommt im Back-Channel-
    Token-Tausch, wo kein Dialog möglich ist; er trägt den **Claims-Challenge**,
    mit dem die Autorisierung NEU gestartet werden muss, damit Entra den Dialog
    zeigt. Getrennt von RuntimeError, damit der Callback ihn gezielt behandelt."""

    def __init__(self, message: str, claims: str):
        super().__init__(message)
        self.claims = claims


def create_session(redirect_uri: str, scopes: list | None = None, flow: str = "setup",
                   next_url: str = "/", extra: dict | None = None,
                   claims: str | None = None, prompt: str = "select_account") -> tuple[str, str]:
    """
    Create a new PKCE session.
    Returns (state, authorization_url).
    flow: "setup" for wizard, "sso" for login
    extra: flow-spezifische Zusatzdaten (z.B. {"mi_object_id": …}), im Callback lesbar.
    """
    _prune_sessions()
    state = secrets.token_urlsafe(24)
    verifier, challenge = generate_pkce_pair()
    _sessions[state] = {
        "verifier": verifier,
        "redirect_uri": redirect_uri,
        "created_at": time.monotonic(),
        "flow": flow,
        "next_url": next_url if next_url.startswith("/") else "/",
        "extra": extra or {},
    }

    use_scopes = scopes if scopes is not None else BOOTSTRAP_SCOPES
    params = {
        "client_id": _get_client_id(),
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(use_scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": prompt,
    }
    if claims:
        # Conditional-Access-Step-up: mit dem Claims-Challenge fordert Entra genau
        # die fehlende Kontrolle (z. B. MFA) beim interaktiven Sign-in an.
        params["claims"] = claims
    auth_url = "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize?" + urllib.parse.urlencode(params)
    log.debug("PKCE session created state=%s flow=%s", state, flow)
    return state, auth_url


def pop_session(state: str) -> dict | None:
    """
    Retrieve and remove a session by state.
    Returns None if state is unknown or expired.
    """
    _prune_sessions()
    session = _sessions.pop(state, None)
    if session is None:
        log.warning("PKCE session not found for state=%s", state)
        return None
    return session


def _prune_sessions() -> None:
    now = time.monotonic()
    expired = [s for s, v in _sessions.items() if now - v["created_at"] > _SESSION_TTL]
    for s in expired:
        del _sessions[s]
        log.debug("PKCE session expired state=%s", s)


async def exchange_code(code: str, verifier: str, redirect_uri: str, scopes: list | None = None) -> dict:
    """
    Exchange an authorization code for tokens using PKCE.
    Returns the full token response dict.
    Raises RuntimeError on failure.
    """
    import httpx

    token_url = "https://login.microsoftonline.com/organizations/oauth2/v2.0/token"
    data = {
        "client_id": _get_client_id(),
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "scope": " ".join(scopes if scopes is not None else BOOTSTRAP_SCOPES),
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(token_url, data=data)

    body = resp.json()
    if "access_token" not in body:
        err = body.get("error_description") or body.get("error") or str(body)
        error = body.get("error", "")
        codes = body.get("error_codes") or []
        claims = body.get("claims") or ""
        # Struktur des Token-Fehlers sichtbar machen (ein Fehlerkörper enthält KEINE
        # Tokens) — so ist am Protokoll ablesbar, ob Entra einen Claims-Challenge
        # mitschickt oder nicht.
        log.warning("PKCE token error: error=%s codes=%s suberror=%s hat_claims=%s",
                    error, codes, body.get("suberror"), bool(claims))
        # Conditional-Access-Step-up (typisch MFA, AADSTS50076) ist interaktiv
        # nachziehbar. Erkennung NICHT nur am `claims`-Feld (das fehlt bei der
        # Auth-Code-Einlösung oft), sondern auch am Fehlercode 50076 /
        # error=interaction_required.
        if (error == "interaction_required" or claims or 50076 in codes
                or "AADSTS50076" in err):
            raise InteractionRequired(f"Token exchange failed: {err}", claims=claims)
        raise RuntimeError(f"Token exchange failed: {err}")

    log.info("PKCE token exchange succeeded")
    return body
