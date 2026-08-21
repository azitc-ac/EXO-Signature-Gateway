"""Anmeldung, Sitzungen und Zugangsberechtigte.

Der Weg hinein: Anmeldung über den Microsoft-Dienst, örtliches Kennwort als
Notzugang, Abmeldung — und die Verwaltung derer, die überhaupt hereindürfen.

⚠️ Was hier steht, entscheidet über den Zugang zum ganzen Gateway. Zwei
Adressen sind bewusst ohne Wache erreichbar, weil vor der Anmeldung noch keine
Sitzung besteht (`/auth/local`, `/auth/callback`); `tests/test_wachen.py`
führt sie namentlich, damit das eine Entscheidung bleibt und keine Lücke wird.

Aus `app/webui/app.py` herausgelöst (21.08.2026). Reines Umsortieren — der
Inhalt der Funktionen ist unverändert; die Routen-Momentaufnahme in
`tests/test_routes.py` belegt, dass dieselbe Oberfläche herauskommt.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, StreamingResponse)

import asyncio
import secrets
import urllib.parse

import config
import graph_client
import pkce as pkce_mod
import settings_store
import sso as sso_mod

from webui.deps import (
    templates, log, _gateway_name, _check_auth, _require_admin,
    _check_password, _get_session_user, _get_session_role,
)
from webui.hilfen import _build_redirect_uri

router = APIRouter()


def _sso_external_host() -> str:
    """Return the hostname the SSO redirect_uri is registered for, or ''."""
    external = (settings_store.get("ADDIN_BASE_URL") or "").rstrip("/")
    if external:
        return urllib.parse.urlparse(external).hostname or ""
    hostname = settings_store.get("PUBLIC_HOSTNAME") or ""
    return hostname.split(":")[0]

def _sso_host_matches(request: Request) -> bool:
    """True if the browser's Host header matches the configured SSO hostname.

    When False, the SSO button would redirect the user to a different host
    (the Azure VM) after login — so we hide it and surface local login instead.
    Returns True when no external hostname is configured (no mismatch possible).
    """
    ext = _sso_external_host()
    if not ext:
        return True
    req_host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").lower()
    req_host = req_host.split(":")[0]
    return req_host == ext.lower()

def _setup_redirect_uri() -> str:
    """Redirect URI for the popup setup-login.

    Uses the public HTTPS callback — which lands back on this gateway, so the popup
    can self-close (postMessage + window.close) — but only once that HTTPS redirect is
    actually registered on the Bootstrap app (recorded in BOOTSTRAP_REDIRECT_URIS after
    the first login / by patch_bootstrap_redirect_uri). Otherwise falls back to the
    localhost callback (copy-paste flow), which works on the very first login before the
    HTTPS redirect has been added — avoids AADSTS50011 on a fresh Bootstrap app.
    """
    https_uri = _build_redirect_uri(sso=True)
    if https_uri.startswith("https://"):
        registered = settings_store.get("BOOTSTRAP_REDIRECT_URIS") or []
        if https_uri in registered:
            return https_uri
    return _build_redirect_uri()

def _setup_callback_page(ok: bool, msg: str = "") -> str:
    """Self-closing page for the popup setup-login (HTTPS-redirect variant).
    Signals the opener (wizard tab) to reload, then closes the popup."""
    if ok:
        icon, heading, body_text, color = "✓", "Entra-Login abgeschlossen", "App-Registrierung eingerichtet. Dieses Fenster schließt sich…", "#16a34a"
    else:
        icon, heading, body_text, color = "✗", "Setup fehlgeschlagen", msg or "Unbekannter Fehler", "#dc2626"
    post_msg = '{"type":"setup-auth-done"}' if ok else '{"type":"setup-auth-fail","msg":' + repr(msg) + '}'
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>{heading}</title></head>
<body style="font-family:sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f8fafc">
<div style="text-align:center;padding:40px;max-width:420px">
  <div style="font-size:52px;margin-bottom:16px">{icon}</div>
  <h2 style="color:{color};margin:0 0 10px">{heading}</h2>
  <p style="color:#64748b;margin:0">{body_text}</p>
</div>
<script>
  try {{ window.opener && window.opener.postMessage({post_msg}, window.opener.location.origin); }} catch(e) {{}}
  {'setTimeout(function(){window.close();},1200);' if ok else ''}
</script>
</body></html>"""

def _arm_callback_page(ok: bool, msg: str = "") -> str:
    if ok:
        icon, heading, body_text, color = "✓", "Azure-Verbindung hergestellt", "Dieses Fenster schließt sich…", "#16a34a"
    else:
        icon, heading, body_text, color = "✗", "Verbindung fehlgeschlagen", msg or "Unbekannter Fehler", "#dc2626"
    post_msg = '{"type":"arm-auth-done"}' if ok else '{"type":"arm-auth-fail","msg":' + repr(msg) + '}'
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>{heading}</title></head>
<body style="font-family:sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f8fafc">
<div style="text-align:center;padding:40px;max-width:420px">
  <div style="font-size:52px;margin-bottom:16px">{icon}</div>
  <h2 style="color:{color};margin:0 0 10px">{heading}</h2>
  <p style="color:#64748b;margin:0">{body_text}</p>
</div>
<script>
  try {{ window.opener && window.opener.postMessage({post_msg}, window.opener.location.origin); }} catch(e) {{}}
  {'setTimeout(function(){window.close();},1200);' if ok else ''}
</script>
</body></html>"""


@router.get("/auth/start")
async def auth_start(request: Request):
    """Return Azure AD auth URL as JSON (for fetch callers in the setup wizard).
    No auth required — generating a PKCE URL is harmless; privileged operations
    are protected by the Microsoft access token returned after login.
    """
    # ?localhost=1 erzwingt den Localhost/Copy-Paste-Redirect (Notausgang, falls die
    # HTTPS-Redirect-URI an der Bootstrap-App doch nicht registriert ist → AADSTS50011).
    force_localhost = request.query_params.get("localhost") in ("1", "true")
    redirect_uri = _build_redirect_uri() if force_localhost else _setup_redirect_uri()
    _state, auth_url = pkce_mod.create_session(redirect_uri, flow="setup")
    return JSONResponse({"auth_url": auth_url})

@router.get("/auth/start-redirect")
async def auth_start_redirect(request: Request):
    """Redirect browser directly to Azure AD for PKCE login (setup wizard)."""
    redirect_uri = _build_redirect_uri()
    _state, auth_url = pkce_mod.create_session(redirect_uri, flow="setup")
    return RedirectResponse(auth_url)

@router.get("/auth/callback", response_class=HTMLResponse)
async def auth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
):
    """Azure AD redirects here with the authorization code — handles both setup and SSO flows."""
    if error:
        session_obj = pkce_mod.pop_session(state) if state else None
        flow = (session_obj or {}).get("flow", "setup")
        if flow == "sso":
            return RedirectResponse(f"/auth/login?error={urllib.parse.quote(error)}", status_code=302)
        return templates.TemplateResponse(
            request=request, name="setup.html",
            context={"s": settings_store.public_view(), "e": {}, "active": "setup",
                     "auth_error": f"{error}: {error_description}",
                     "gateway_name": _gateway_name()},
        )

    session_obj = pkce_mod.pop_session(state)
    if not session_obj:
        return RedirectResponse("/auth/login?error=session_expired", status_code=302)

    flow = session_obj.get("flow", "setup")

    try:
        from sso import SSO_SCOPES
        if flow == "sso":
            use_scopes = SSO_SCOPES
        elif flow == "arm":
            use_scopes = pkce_mod.ARM_SCOPES
        else:
            use_scopes = None
        token_resp = await pkce_mod.exchange_code(
            code, session_obj["verifier"], session_obj["redirect_uri"], scopes=use_scopes
        )
    except Exception as exc:
        log.error("PKCE token exchange failed: %s", exc)
        if flow == "sso":
            return RedirectResponse(f"/auth/login?error={urllib.parse.quote(str(exc))}", status_code=302)
        if flow == "arm":
            return HTMLResponse(_arm_callback_page(ok=False, msg=str(exc)))
        return templates.TemplateResponse(
            request=request, name="setup.html",
            context={"s": settings_store.public_view(), "e": {}, "active": "setup",
                     "auth_error": str(exc), "gateway_name": _gateway_name()},
        )

    if flow == "sso":
        # SSO login: check UPN against configured users; also try OID for robustness
        upn = sso_mod.get_upn_from_token_response(token_resp)
        if not upn:
            return RedirectResponse("/auth/login?error=no_upn", status_code=302)
        # Extract OID from id_token for OID-based lookup
        id_token_claims = sso_mod.decode_id_token(token_resp.get("id_token", ""))
        oid = (id_token_claims.get("oid") or id_token_claims.get("sub") or "").strip()
        # Try OID first, then fall back to UPN
        role = (sso_mod.get_role_by_oid(oid) if oid else None) or sso_mod.get_role(upn)
        if not role:
            log.warning("SSO login denied for UPN: %s (oid: %s)", upn, oid or "n/a")
            return RedirectResponse(
                f"/auth/login?error=not_admin&upn={urllib.parse.quote(upn)}", status_code=302
            )
        # Auto-patch: if user entry lacks an OID but we have one now, save it
        if oid:
            users = sso_mod.normalize_users()
            patched = False
            for entry in users:
                if entry["upn"] == upn.lower() and not entry.get("id"):
                    entry["id"] = oid
                    patched = True
                    break
            if patched:
                settings_store.update({"ADMIN_USERS": users})
                log.info("Auto-patched OID for SSO user %s → %s", upn, oid)
        log.info("SSO login successful: %s (role: %s, oid: %s)", upn, role, oid or "n/a")
        cookie_val = sso_mod.create_session_cookie(upn, local=False, role=role)
        next_url = session_obj.get("next_url") or request.query_params.get("next", "/")
        response = RedirectResponse(next_url, status_code=302)
        response.set_cookie(
            sso_mod.SESSION_COOKIE, cookie_val,
            max_age=sso_mod.SESSION_TTL, httponly=True, samesite="lax", secure=True,
        )
        return response

    elif flow == "arm":
        # ARM delegated token: store it and close the popup
        import keyvault
        arm_token = token_resp.get("access_token", "")
        expires_in = int(token_resp.get("expires_in", 3600))
        upn = _get_session_user(request) or ""
        if arm_token and upn:
            keyvault.store_user_arm_token(upn, arm_token, expires_in)
            log.info("ARM delegated token stored via callback for %s (expires_in=%s)", upn, expires_in)
            return HTMLResponse(_arm_callback_page(ok=True))
        return HTMLResponse(_arm_callback_page(ok=False, msg="Kein Token erhalten oder Sitzung abgelaufen."))

    elif flow == "patch_redirect_uri":
        # Triggered from Settings → Add-in → "Redirect URI aktualisieren"
        access_token = token_resp.get("access_token", "")
        hostname = settings_store.get("PUBLIC_HOSTNAME") or ""
        try:
            import setup_wizard
            await setup_wizard.patch_bootstrap_redirect_uri(access_token, hostname)
            log.info("Add-in: Bootstrap redirect URI patched via settings flow")
        except Exception as exc:
            log.warning("Add-in redirect URI patch failed: %s", exc)
        return RedirectResponse("/setup?addin_uri_patched=1#step-addin", status_code=303)

    else:
        # Setup flow (popup, HTTPS redirect): run post-auth setup, then self-close
        # the popup and signal the opener (wizard tab) to reload. The localhost
        # copy-paste flow (/api/setup/auth-paste) remains as fallback.
        access_token = token_resp.get("access_token", "")
        try:
            import setup_wizard
            result = await setup_wizard.run_post_auth_setup(access_token)
            log.info("Post-auth setup complete: %s", result)
        except Exception as exc:
            log.error("Post-auth setup failed: %s", exc)
            return HTMLResponse(_setup_callback_page(ok=False, msg=str(exc)))
        return HTMLResponse(_setup_callback_page(ok=True))

@router.get("/auth/login", response_class=HTMLResponse)
async def auth_login(request: Request, error: str = "", next: str = "/"):
    """Login page — shown to unauthenticated users."""
    ext_host = _sso_external_host()
    return templates.TemplateResponse(
        request=request, name="login.html",
        context={
            "error": error,
            "next": next,
            "upn": request.query_params.get("upn", ""),
            "sso_configured": sso_mod.sso_configured(),
            "sso_available": bool((settings_store.get("BOOTSTRAP_CLIENT_ID") or "").strip()),
            "sso_host_matches": _sso_host_matches(request),
            "sso_external_host": ext_host,
            "gateway_name": _gateway_name(),
        },
    )

@router.get("/auth/login/microsoft")
async def auth_login_microsoft(request: Request, next: str = "/"):
    """Start SSO PKCE flow with minimal scopes."""
    redirect_uri = _build_redirect_uri(sso=True)
    _state, auth_url = pkce_mod.create_session(
        redirect_uri, scopes=sso_mod.SSO_SCOPES, flow="sso", next_url=next
    )
    return RedirectResponse(auth_url)

@router.get("/api/auth/sso-url")
async def api_sso_url(request: Request):
    """Return Microsoft SSO auth URL as JSON (for fetch callers — no auth needed)."""
    redirect_uri = _build_redirect_uri(sso=True)
    _state, auth_url = pkce_mod.create_session(
        redirect_uri, scopes=sso_mod.SSO_SCOPES, flow="sso"
    )
    return JSONResponse({"auth_url": auth_url})

@router.post("/api/auth/sso-paste")
async def api_sso_paste(request: Request):
    """Process a pasted callback URL from a failed SSO redirect."""
    body = await request.json()
    url = (body.get("url") or "").strip()
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    code  = (params.get("code")  or [""])[0]
    state = (params.get("state") or [""])[0]
    error = (params.get("error") or [""])[0]

    if error:
        raise HTTPException(400, error)
    if not code or not state:
        raise HTTPException(400, "URL enthält keinen Code oder State-Parameter")

    session_obj = pkce_mod.pop_session(state)
    if not session_obj:
        raise HTTPException(400, "Sitzung abgelaufen — bitte erneut mit Microsoft anmelden")

    try:
        token_resp = await pkce_mod.exchange_code(
            code, session_obj["verifier"], session_obj["redirect_uri"],
            scopes=sso_mod.SSO_SCOPES,
        )
    except Exception as exc:
        raise HTTPException(400, f"Token-Austausch fehlgeschlagen: {exc}")

    upn = sso_mod.get_upn_from_token_response(token_resp)
    if not upn:
        raise HTTPException(400, "Konto-Informationen konnten nicht gelesen werden")
    role = sso_mod.get_role(upn)
    if not role:
        raise HTTPException(403, f"{upn} ist nicht konfiguriert")

    log.info("SSO login (paste) successful: %s (role: %s)", upn, role)
    cookie_val = sso_mod.create_session_cookie(upn, local=False, role=role)
    resp = JSONResponse({"ok": True, "upn": upn})
    resp.set_cookie(
        sso_mod.SESSION_COOKIE, cookie_val,
        max_age=sso_mod.SESSION_TTL, httponly=True, samesite="lax", secure=True,
    )
    return resp

@router.post("/auth/local")
async def auth_local(request: Request):
    """Local admin login — creates session cookie."""
    data = await request.json()
    username_in = (data.get("username") or "").strip()
    password_in = (data.get("password") or "")
    username = settings_store.get("WEBUI_USERNAME") or "admin"
    if (secrets.compare_digest(username_in.encode(), username.encode())
            and _check_password(password_in)):
        cookie_val = sso_mod.create_session_cookie(username_in, local=True)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            sso_mod.SESSION_COOKIE, cookie_val,
            max_age=sso_mod.SESSION_TTL, httponly=True, samesite="lax", secure=True,
        )
        log.info("Local admin login: %s", username_in)
        # Send notification about local admin login (fire-and-forget)
        import notification as _notif
        ip = request.client.host if request.client else "unbekannt"
        ua = request.headers.get("user-agent", "unbekannt")
        asyncio.get_event_loop().run_in_executor(None, _notif.send_local_admin_login, ip, ua, username_in)
        return resp
    raise HTTPException(401, "Benutzername oder Passwort falsch")

@router.post("/auth/logout")
async def auth_logout(request: Request):
    """Clear session cookie."""
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(sso_mod.SESSION_COOKIE)
    return resp

@router.get("/auth/logout")
async def auth_logout_get(request: Request):
    """Clear session cookie and redirect to login."""
    resp = RedirectResponse("/auth/login", status_code=302)
    resp.delete_cookie(sso_mod.SESSION_COOKIE)
    return resp

@router.get("/api/whoami")
async def api_whoami(request: Request):
    """Returns current user info. Returns nulls when unauthenticated — no Basic-Auth challenge."""
    user = _get_session_user(request)
    if not user:
        return JSONResponse({"upn": None, "role": None})
    return JSONResponse({"upn": user.lower(), "role": _get_session_role(request)})

@router.get("/api/admin-users")
async def api_get_admin_users(_=Depends(_require_admin)):
    return JSONResponse({"users": sso_mod.normalize_users()})

@router.post("/api/admin-users")
async def api_add_admin_user(request: Request, user: str = Depends(_require_admin)):
    data = await request.json()
    upn  = (data.get("upn")  or "").strip().lower()
    role = (data.get("role") or sso_mod.ROLE_ADMIN)
    if not upn or "@" not in upn:
        raise HTTPException(400, "Ungültige UPN")
    if role not in sso_mod.VALID_ROLES:
        raise HTTPException(400, "Ungültige Rolle")
    users = sso_mod.normalize_users()
    if any(e["upn"] == upn for e in users):
        raise HTTPException(409, f"{upn} ist bereits konfiguriert")
    # Resolve Entra Object ID for robust identity tracking
    oid = await asyncio.get_event_loop().run_in_executor(None, sso_mod.resolve_upn_to_oid, upn)
    new_entry: dict = {"upn": upn, "role": role}
    if oid:
        new_entry["id"] = oid
    else:
        log.warning("Could not resolve OID for %s — saving without id", upn)
    users.append(new_entry)
    settings_store.update({"ADMIN_USERS": users})
    log.info("User added: %s (role: %s, oid: %s) by %s", upn, role, oid or "n/a", user)
    return JSONResponse({"ok": True, "users": users})

@router.patch("/api/admin-users/{upn}")
async def api_update_admin_user(upn: str, request: Request, user: str = Depends(_require_admin)):
    data = await request.json()
    upn  = urllib.parse.unquote(upn).strip().lower()
    role = (data.get("role") or "")
    if role not in sso_mod.VALID_ROLES:
        raise HTTPException(400, "Ungültige Rolle")
    if upn == user.strip().lower():
        raise HTTPException(400, "Eigene Rolle kann nicht geändert werden")
    users = sso_mod.normalize_users()
    for entry in users:
        if entry["upn"] == upn:
            entry["role"] = role
            settings_store.update({"ADMIN_USERS": users})
            log.info("Role of %s changed to %s by %s", upn, role, user)
            return JSONResponse({"ok": True, "users": users})
    raise HTTPException(404, "Benutzer nicht gefunden")

@router.delete("/api/admin-users/{upn}")
async def api_remove_admin_user(upn: str, request: Request, user: str = Depends(_require_admin)):
    upn = urllib.parse.unquote(upn).strip().lower()
    if upn == user.strip().lower():
        raise HTTPException(400, "Eigenes Konto kann nicht entfernt werden")
    users = sso_mod.normalize_users()
    new_users = [e for e in users if e["upn"] != upn]
    if not any(e["role"] == sso_mod.ROLE_ADMIN for e in new_users):
        raise HTTPException(400, "Mindestens ein Admin muss verbleiben")
    settings_store.update({"ADMIN_USERS": new_users})
    log.info("User removed: %s by %s", upn, user)
    return JSONResponse({"ok": True, "users": new_users})

@router.put("/api/admin-users")
async def api_replace_admin_users(request: Request, actor: str = Depends(_require_admin)):
    """Replace the entire admin users list (used by the settings page save button)."""
    data = await request.json()
    users = data.get("users", [])
    if not isinstance(users, list):
        raise HTTPException(400, "Ungültiges Format")
    for entry in users:
        if not entry.get("upn") or "@" not in entry["upn"]:
            raise HTTPException(400, f"Ungültige UPN: {entry.get('upn')}")
        if entry.get("role") not in sso_mod.VALID_ROLES:
            raise HTTPException(400, f"Ungültige Rolle: {entry.get('role')}")
    if not any(e.get("role") == sso_mod.ROLE_ADMIN for e in users):
        raise HTTPException(400, "Mindestens ein Admin muss vorhanden sein")
    # Resolve OIDs for entries that don't have one yet
    for entry in users:
        if not entry.get("id"):
            oid = await asyncio.get_event_loop().run_in_executor(
                None, sso_mod.resolve_upn_to_oid, entry["upn"]
            )
            if oid:
                entry["id"] = oid
    settings_store.update({"ADMIN_USERS": users})
    log.info("Admin users saved by %s: %s", actor, [u["upn"] for u in users])
    return JSONResponse({"ok": True, "users": users})

@router.get("/api/entra/users")
async def api_entra_users_search(q: str = "", _=Depends(_require_admin)):
    """Search Entra tenant users via Graph API for the admin user combobox."""
    token = graph_client._acquire_token()
    if not token:
        raise HTTPException(503, "Graph-Zugangsdaten nicht konfiguriert")
    headers = {"Authorization": f"Bearer {token}"}
    params: dict = {"$select": "id,userPrincipalName,displayName", "$top": "25"}
    if q:
        # $search supports UPN + displayName without needing $filter on displayName
        # Requires ConsistencyLevel: eventual
        q_esc = q.replace('"', '\\"')
        params["$search"] = f'"userPrincipalName:{q_esc}" OR "displayName:{q_esc}"'
        params["$count"] = "true"
        headers["ConsistencyLevel"] = "eventual"
    else:
        params["$orderby"] = "userPrincipalName"
    try:
        async with __import__("httpx").AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://graph.microsoft.com/v1.0/users",
                headers=headers,
                params=params,
            )
        resp.raise_for_status()
        users = resp.json().get("value", [])
        return JSONResponse({
            "users": [
                {"id": u["id"], "upn": u["userPrincipalName"], "name": u.get("displayName", "")}
                for u in users
            ]
        })
    except Exception as exc:
        raise HTTPException(500, str(exc))
