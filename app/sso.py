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

ROLE_ADMIN    = "admin"
ROLE_EDITOR   = "editor"
ROLE_CAMPAIGN = "kampagnen"      # Kampagnen-Manager: verwaltet Banner-Kampagnen
VALID_ROLES = {ROLE_ADMIN, ROLE_EDITOR, ROLE_CAMPAIGN}

# Scopes for SSO login (minimal, just identity)
# Nur Identität — bewusst KEIN `User.Read`/`offline_access`: Der Login benutzt das
# Access-Token nicht (Identität kommt aus dem id_token). Ein Graph-Resource-Scope
# löst die Token-Ausstellung im Back-Channel aus; verlangt eine Conditional-Access-
# Regel dort MFA, kann der Dialog NICHT erscheinen (kein interaktiver Kanal) und der
# Login scheitert nur mit einem Fehler. Ohne Resource-Scope wird MFA am interaktiven
# Sign-in ausgewertet, wo Entra den Dialog zeigen kann. NICHT wieder erweitern.
SSO_SCOPES = ["openid", "profile", "email"]


def _get_secret() -> str:
    """Return session signing secret, auto-generating and persisting if needed."""
    secret = settings_store.get("SSO_SESSION_SECRET") or ""
    if not secret:
        secret = secrets.token_hex(32)
        settings_store.update({"SSO_SESSION_SECRET": secret})
    return secret


def normalize_users() -> list[dict]:
    """Return ADMIN_USERS as list of {upn, role[, id]} dicts, migrating legacy string entries."""
    users = settings_store.get("ADMIN_USERS") or []
    result = []
    for entry in users:
        if isinstance(entry, str):
            result.append({"upn": entry.strip().lower(), "role": ROLE_ADMIN})
        elif isinstance(entry, dict):
            upn  = (entry.get("upn") or "").strip().lower()
            role = entry.get("role", ROLE_ADMIN)
            if upn and role in VALID_ROLES:
                user_entry: dict = {"upn": upn, "role": role}
                oid = (entry.get("id") or "").strip()
                if oid:
                    user_entry["id"] = oid
                result.append(user_entry)
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


_jwks_clients: dict = {}


def _jwks_client(jwks_uri: str):
    """Zwischengespeicherter PyJWKClient je JWKS-URI (holt Entras Signaturschlüssel)."""
    from jwt import PyJWKClient
    c = _jwks_clients.get(jwks_uri)
    if c is None:
        c = PyJWKClient(jwks_uri)
        _jwks_clients[jwks_uri] = c
    return c


def verify_id_token(id_token: str, nonce: str) -> dict | None:
    """Prüft ein FRONT-CHANNEL id_token (Implicit-Flow) VOLLSTÄNDIG und gibt die
    Claims zurück oder None.

    Anders als `decode_id_token` (Back-Channel, HTTPS-vertraut) kommt dieses Token
    ungeprüft durch den Browser — es MUSS verifiziert werden:
      - Signatur gegen Entras JWKS (RS256),
      - Audience == unsere Bootstrap-Client-ID,
      - Aussteller/`tid` == unser Tenant,
      - Ablauf (exp), und
      - `nonce` == der in der Sitzung hinterlegte Wert (Replay-/Injektionsschutz).
    """
    try:
        import jwt
        import config as _config
        client_id = (settings_store.get("BOOTSTRAP_CLIENT_ID") or "").strip()
        tenant = (settings_store.get("TENANT_ID")
                  or getattr(_config, "TENANT_ID", "") or "").strip()
        if not client_id:
            log.warning("verify_id_token: keine BOOTSTRAP_CLIENT_ID gesetzt")
            return None
        jwks_uri = (f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys"
                    if tenant else
                    "https://login.microsoftonline.com/organizations/discovery/v2.0/keys")
        signing_key = _jwks_client(jwks_uri).get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token, signing_key.key, algorithms=["RS256"],
            audience=client_id, leeway=60,
            options={"require": ["exp", "aud"], "verify_iss": False},
        )
        if tenant:
            tid = (claims.get("tid") or "").strip()
            iss = claims.get("iss") or ""
            if tid and tid != tenant:
                log.warning("verify_id_token: tid %r != Tenant %r", tid, tenant)
                return None
            if f"/{tenant}/" not in iss:
                log.warning("verify_id_token: Aussteller %r passt nicht zum Tenant", iss)
                return None
        if nonce and claims.get("nonce") != nonce:
            log.warning("verify_id_token: nonce stimmt nicht überein")
            return None
        return claims
    except Exception as exc:                                  # noqa: BLE001
        log.warning("verify_id_token fehlgeschlagen: %s", exc)
        return None


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


def get_role(upn_or_oid: str) -> str | None:
    """Return role for UPN or OID ('admin' or 'editor'), or None if not configured."""
    if not upn_or_oid:
        return None
    val_lower = upn_or_oid.strip().lower()
    # Search by OID first, then by UPN
    for entry in normalize_users():
        oid = (entry.get("id") or "").lower()
        if oid and oid == val_lower:
            return entry["role"]
    for entry in normalize_users():
        if entry["upn"] == val_lower:
            return entry["role"]
    return None


def get_role_by_oid(oid: str) -> str | None:
    """Return role for Entra Object ID, or None if not found."""
    if not oid:
        return None
    oid_lower = oid.strip().lower()
    for entry in normalize_users():
        if (entry.get("id") or "").lower() == oid_lower:
            return entry["role"]
    return None


def resolve_upn_to_oid(upn: str) -> str | None:
    """
    Resolve a UPN to its Entra Object ID via Microsoft Graph.
    Returns the OID string or None on failure.
    Uses a synchronous httpx.Client call (safe for background/setup use).
    """
    try:
        import graph_client
        import httpx
        token = graph_client._acquire_token()
        if not token:
            log.warning("resolve_upn_to_oid: no Graph token for %s", upn)
            return None
        url = f"https://graph.microsoft.com/v1.0/users/{upn}?$select=id,userPrincipalName"
        headers = {"Authorization": f"Bearer {token}"}
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            oid = data.get("id") or ""
            if oid:
                log.info("Resolved UPN %s → OID %s", upn, oid)
                return oid
            log.warning("resolve_upn_to_oid: no id in response for %s", upn)
            return None
        log.warning("resolve_upn_to_oid: HTTP %s for %s", resp.status_code, upn)
        return None
    except Exception as exc:
        log.warning("resolve_upn_to_oid error for %s: %s", upn, exc)
        return None


def resolve_oid_to_upn(oid: str) -> str | None:
    """Resolve an Entra Object ID to its CURRENT UPN via Microsoft Graph.

    Gegenstück zu resolve_upn_to_oid — die oid ist der stabile Anker, der UPN kann
    sich ändern (Umbenennung). Rückgabe: UPN (klein) oder None. Sync httpx.
    """
    if not oid:
        return None
    try:
        import graph_client
        import httpx
        token = graph_client._acquire_token()
        if not token:
            return None
        url = f"https://graph.microsoft.com/v1.0/users/{oid}?$select=userPrincipalName"
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code == 200:
            return ((resp.json().get("userPrincipalName") or "").strip().lower()) or None
        log.warning("resolve_oid_to_upn: HTTP %s for %s", resp.status_code, oid)
        return None
    except Exception as exc:                                  # noqa: BLE001
        log.warning("resolve_oid_to_upn error for %s: %s", oid, exc)
        return None


def refresh_stored_upns() -> list[dict]:
    """Für jeden Eintrag mit oid den aktuellen UPN aus Graph holen und bei Änderung
    in ADMIN_USERS persistieren — damit Anzeige, Aktionen und Speicher konsistent
    bleiben, wenn ein Konto umbenannt wurde (die oid bleibt der Anker).

    Best-effort: schlägt eine Auflösung fehl, bleibt der gespeicherte UPN. Gibt die
    (aktuelle) Nutzerliste zurück. NUR aus dem Hauptprozess aufrufen (persistiert).
    """
    users = normalize_users()
    geaendert = False
    for entry in users:
        oid = (entry.get("id") or "").strip()
        if not oid:
            continue
        aktuell = resolve_oid_to_upn(oid)
        if aktuell and aktuell != entry["upn"]:
            log.info("ADMIN_USERS: UPN aktualisiert %s → %s (oid %s)",
                     entry["upn"], aktuell, oid)
            entry["upn"] = aktuell
            geaendert = True
    if geaendert:
        settings_store.update({"ADMIN_USERS": users})
    return users


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
