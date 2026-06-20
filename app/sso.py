"""
SSO session management for EXO Signature Gateway.
Signs session cookies with itsdangerous; decodes Entra ID tokens.
"""
import base64
import json
import logging
import secrets
import time

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

import settings_store

log = logging.getLogger(__name__)

SESSION_COOKIE = "exo_session"
SESSION_TTL = 8 * 3600  # 8 hours

ROLE_ADMIN  = "admin"
ROLE_EDITOR = "editor"
VALID_ROLES = {ROLE_ADMIN, ROLE_EDITOR}

# Scopes for SSO login (minimal, just identity)
SSO_SCOPES = ["openid", "profile", "email", "User.Read", "offline_access"]


def _get_secret() -> str:
    """Return session signing secret, auto-generating and persisting if needed."""
    secret = settings_store.get("SSO_SESSION_SECRET") or ""
    if not secret:
        secret = secrets.token_hex(32)
        settings_store.update({"SSO_SESSION_SECRET": secret})
    return secret


def normalize_users() -> list[dict]:
    """Return ADMIN_USERS as list of {upn, role} dicts, migrating legacy string entries."""
    users = settings_store.get("ADMIN_USERS") or []
    result = []
    for entry in users:
        if isinstance(entry, str):
            result.append({"upn": entry.strip().lower(), "role": ROLE_ADMIN})
        elif isinstance(entry, dict):
            upn  = (entry.get("upn") or "").strip().lower()
            role = entry.get("role", ROLE_ADMIN)
            if upn and role in VALID_ROLES:
                result.append({"upn": upn, "role": role})
    return result


def create_session_cookie(upn: str, local: bool = False, role: str = ROLE_ADMIN) -> str:
    """Return a signed cookie value for the given UPN."""
    s = URLSafeTimedSerializer(_get_secret())
    payload = {"u": upn, "t": "local" if local else "sso", "ts": int(time.time()), "r": role}
    return s.dumps(payload)


def verify_session_cookie(value: str) -> dict | None:
    """Verify and decode a session cookie. Returns payload dict or None."""
    try:
        s = URLSafeTimedSerializer(_get_secret())
        return s.loads(value, max_age=SESSION_TTL)
    except (BadSignature, SignatureExpired):
        return None
    except Exception as exc:
        log.debug("Session cookie error: %s", exc)
        return None


def decode_id_token(token: str) -> dict:
    """Decode JWT payload without signature verification (we trust Microsoft's HTTPS endpoint)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def get_upn_from_token_response(token_resp: dict) -> str:
    """Extract UPN from token response (id_token preferred_username claim)."""
    id_token = token_resp.get("id_token", "")
    if id_token:
        claims = decode_id_token(id_token)
        upn = (claims.get("preferred_username") or claims.get("upn") or
               claims.get("email") or "").strip()
        if upn:
            return upn
    access_token = token_resp.get("access_token", "")
    if access_token:
        claims = decode_id_token(access_token)
        upn = (claims.get("preferred_username") or claims.get("upn") or
               claims.get("email") or "").strip()
        if upn:
            return upn
    return ""


def get_role(upn: str) -> str | None:
    """Return role for UPN ('admin' or 'editor'), or None if not configured."""
    if not upn:
        return None
    upn_lower = upn.strip().lower()
    for entry in normalize_users():
        if entry["upn"] == upn_lower:
            return entry["role"]
    return None


def is_allowed(upn: str) -> bool:
    """Check if UPN has any configured role."""
    return get_role(upn) is not None


def is_admin(upn: str) -> bool:
    """Check if UPN has admin role."""
    return get_role(upn) == ROLE_ADMIN


def sso_configured() -> bool:
    """True if at least one user is configured AND Bootstrap app is set."""
    bootstrap = (settings_store.get("BOOTSTRAP_CLIENT_ID") or "").strip()
    return bool(normalize_users() and bootstrap)
