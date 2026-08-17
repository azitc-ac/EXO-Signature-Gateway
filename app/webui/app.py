import asyncio
import collections
import hashlib
import hmac
import io
import json as _json_mod
import os
import queue as _queue_mod
import re as _re
import secrets
import shutil
import smtplib
import ssl
import subprocess
import sys
import threading
import logging
import urllib.parse
import uuid as _uuid
import xml.etree.ElementTree as _ET
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

from fastapi import FastAPI, Request, Form, Depends, HTTPException, status, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

import secure_io as _secure_io
from fastapi.templating import Jinja2Templates

import config
import graph_client
import held_mails as _held_mails_mod
import mail_processor
import pkce as pkce_mod
import settings_store
import signature_engine
import sso as sso_mod


from contextlib import asynccontextmanager

@asynccontextmanager
async def _lifespan(application):
    # Die Testumgebung war bis v1.7.207 ein Ankreuzfeld neben der Auswahl des
    # Bezugswegs; sie ist jetzt ein eigener Eintrag in derselben Liste.
    # Bestehende Konfigurationen mit gesetztem Flag werden hier überführt —
    # idempotent, ändert nur, was noch nicht überführt ist.
    try:
        from ca_backends import registry as _ca_registry
        _umgestellt = _ca_registry.migriere_staging_flag()
        if _umgestellt:
            logging.getLogger(__name__).info(
                "CASTLE-Testumgebung: %d Postfach/Postfächer auf den eigenen "
                "Bezugsweg umgestellt", _umgestellt)
    except Exception as exc:
        logging.getLogger(__name__).error(
            "Umstellung der CASTLE-Testumgebung fehlgeschlagen: %s", exc)

    import acme_state
    acme_state.resume_pending_polls()
    yield

app = FastAPI(title="EXO Signature Gateway", lifespan=_lifespan)

# Gemeinsames Fundament — dieselben Objekte, die auch die Routenmodule nutzen.
# Weitergereicht statt neu definiert: Die Tests haengen sich ueber
# `app.dependency_overrides[_check_auth]` ein, und der Schluessel ist das
# Funktionsobjekt. Zwei Kopien, und die Umgehung passte zu keinem Depends mehr.
from webui.deps import (                                    # noqa: E402
    log, templates, _STATIC_DIR, _TEMPLATE_DIR, _gateway_name,
    _NotAuthenticated, _check_password,
    _get_session_user, _get_session_role, _check_auth, _require_admin,
)

# Der Mount haengt an der ANWENDUNG und bleibt deshalb hier, waehrend das
# Verzeichnis aus dem Fundament kommt. Beim Verschieben fiel er zunaechst mit
# heraus — die Routen-Momentaufnahme meldete prompt „/static VERLOREN".
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# Geteilte Helfer. `_addin_base_url` kam frueher aus `routen/addin.py` zurueck —
# das lief, war aber die falsche Richtung: `app.py` bindet Routenmodule ein und
# holt nichts aus ihnen. Mit `_cert_expiry` stand derselbe Fall ein zweites Mal
# an, deshalb jetzt ein eigener Ort. (`_addin_base_url` braucht `app.py` selbst
# inzwischen nicht mehr — es teilen sich Add-in- und Einrichtungsmodul.)
#
# `_build_redirect_uri` und `_password_change_required` kamen mit dem
# Einrichtungsmodul dazu: Beide werden dort UND hier gebraucht (Anmelderouten
# bzw. Uebersichtsseite) — waeren sie mitgewandert, holte `app.py` sie aus einem
# Routenmodul zurueck, also genau die verkehrte Richtung.
from webui.hilfen import (                                   # noqa: E402
    _cert_expiry, _build_redirect_uri, _password_change_required,
)

# ── Routenmodule ─────────────────────────────────────────────────────────────
from webui.routen import addin as _routen_addin              # noqa: E402
from webui.routen import backup as _routen_backup            # noqa: E402
from webui.routen import hub as _routen_hub                  # noqa: E402
from webui.routen import mailboxes as _routen_mailboxes      # noqa: E402
from webui.routen import portal as _routen_portal            # noqa: E402
from webui.routen import settings as _routen_settings        # noqa: E402
from webui.routen import setup as _routen_setup              # noqa: E402
from webui.routen import smime as _routen_smime              # noqa: E402

# EINE Quelle: hieraus werden die Router eingebunden, und `tests/test_routes.py`
# zaehlt daraus die Routen ab.
#
# ⚠️ Notwendig, weil `include_router()` ab FastAPI 0.139 die Routen NICHT mehr
# nach `app.routes` kopiert, sondern einen Stellvertreter (`_IncludedRouter`)
# einhaengt. Zur Laufzeit stimmt alles — von aussen sind die Adressen aber
# nicht mehr aufzaehlbar. Ohne diese Liste verloere die Routen-Momentaufnahme
# mit jedem weiteren Modul stillschweigend an Abdeckung, also genau das Netz,
# das diesen Umbau ueberhaupt verantwortbar macht.
ROUTENMODULE = [_routen_addin, _routen_backup, _routen_hub, _routen_mailboxes,
                _routen_portal, _routen_settings, _routen_setup, _routen_smime]

for _modul in ROUTENMODULE:
    app.include_router(_modul.router)


@app.exception_handler(_NotAuthenticated)
async def _not_authenticated_handler(request: Request, exc: _NotAuthenticated):
    if exc.is_api:
        # No WWW-Authenticate header — that header triggers the browser's native Basic-Auth
        # dialog for any fetch() call, even when the user has a valid SSO session but the
        # session cookie was just invalidated (e.g. after container restart).
        # JS callers receive the 401 JSON and handle it gracefully without a browser popup.
        return JSONResponse({"detail": "Nicht angemeldet"}, status_code=401)
    next_url = urllib.parse.quote(str(request.url.path), safe="")
    return RedirectResponse(f"/auth/login?next={next_url}", status_code=302)






# ── In-memory stats (reset on restart) ────────────────────────────────────────
import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent.parent))
import stats as _stats_mod


def get_stats() -> dict:
    return _stats_mod.get()


def increment_stat(key: str) -> None:
    _stats_mod.increment(key)


# ── Live log streaming ─────────────────────────────────────────────────────────
_LOG_BUFFER: collections.deque = collections.deque(maxlen=500)
_LOG_SUBSCRIBERS: list[_queue_mod.Queue] = []
_LOG_SUBSCRIBERS_LOCK = threading.Lock()


class _MemoryLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        line = self.format(record)
        _LOG_BUFFER.append(line)
        with _LOG_SUBSCRIBERS_LOCK:
            for q in _LOG_SUBSCRIBERS:
                try:
                    q.put_nowait(line)
                except _queue_mod.Full:
                    pass


_mem_handler = _MemoryLogHandler()
_mem_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
))
logging.getLogger().addHandler(_mem_handler)

# Short-lived tokens for /log/stream (EventSource cannot send HTTP Basic Auth)
import time as _time
_LOG_TOKENS: dict[str, float] = {}


def _make_log_token() -> str:
    token = secrets.token_urlsafe(32)
    _LOG_TOKENS[token] = _time.time() + 3600
    # Purge expired tokens
    expired = [k for k, exp in _LOG_TOKENS.items() if _time.time() > exp]
    for k in expired:
        _LOG_TOKENS.pop(k, None)
    return token


def _check_log_token(token: str) -> bool:
    exp = _LOG_TOKENS.get(token)
    return exp is not None and _time.time() < exp










# ── Role middleware — sets request.state.user_role for templates ───────────────
@app.middleware("http")
async def _attach_user_role(request: Request, call_next):
    cookie = request.cookies.get(sso_mod.SESSION_COOKIE)
    role = sso_mod.ROLE_ADMIN  # default: Basic auth or unauthenticated
    if cookie:
        payload = sso_mod.verify_session_cookie(cookie)
        if payload:
            role = payload.get("r", sso_mod.ROLE_ADMIN)
    request.state.user_role = role
    response = await call_next(request)
    # Never cache dynamic HTML — avoids stale UI (e.g. old JS) after an update.
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-store"
    return response


# ── Helpers ────────────────────────────────────────────────────────────────────
def _webui_scheme() -> str:
    """https if TLS cert is present, http otherwise."""
    return "https" if Path(config.SMTP_TLS_CERT).exists() else "http"


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


# ── Routes: public (no auth) ───────────────────────────────────────────────────

@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "service": "exo-signature-service"})




# ── Routes: PKCE auth flow ─────────────────────────────────────────────────────

@app.get("/auth/start")
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


@app.get("/auth/start-redirect")
async def auth_start_redirect(request: Request):
    """Redirect browser directly to Azure AD for PKCE login (setup wizard)."""
    redirect_uri = _build_redirect_uri()
    _state, auth_url = pkce_mod.create_session(redirect_uri, flow="setup")
    return RedirectResponse(auth_url)




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


@app.get("/auth/callback", response_class=HTMLResponse)
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


# ── Routes: SSO login / logout ────────────────────────────────────────────────

@app.get("/auth/login", response_class=HTMLResponse)
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


@app.get("/auth/login/microsoft")
async def auth_login_microsoft(request: Request, next: str = "/"):
    """Start SSO PKCE flow with minimal scopes."""
    redirect_uri = _build_redirect_uri(sso=True)
    _state, auth_url = pkce_mod.create_session(
        redirect_uri, scopes=sso_mod.SSO_SCOPES, flow="sso", next_url=next
    )
    return RedirectResponse(auth_url)


@app.get("/api/auth/sso-url")
async def api_sso_url(request: Request):
    """Return Microsoft SSO auth URL as JSON (for fetch callers — no auth needed)."""
    redirect_uri = _build_redirect_uri(sso=True)
    _state, auth_url = pkce_mod.create_session(
        redirect_uri, scopes=sso_mod.SSO_SCOPES, flow="sso"
    )
    return JSONResponse({"auth_url": auth_url})


@app.post("/api/auth/sso-paste")
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


@app.post("/auth/local")
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


@app.post("/auth/logout")
async def auth_logout(request: Request):
    """Clear session cookie."""
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(sso_mod.SESSION_COOKIE)
    return resp


@app.get("/auth/logout")
async def auth_logout_get(request: Request):
    """Clear session cookie and redirect to login."""
    resp = RedirectResponse("/auth/login", status_code=302)
    resp.delete_cookie(sso_mod.SESSION_COOKIE)
    return resp









@app.get("/api/whoami")
async def api_whoami(request: Request):
    """Returns current user info. Returns nulls when unauthenticated — no Basic-Auth challenge."""
    user = _get_session_user(request)
    if not user:
        return JSONResponse({"upn": None, "role": None})
    return JSONResponse({"upn": user.lower(), "role": _get_session_role(request)})


@app.get("/api/admin-users")
async def api_get_admin_users(_=Depends(_require_admin)):
    return JSONResponse({"users": sso_mod.normalize_users()})


@app.post("/api/admin-users")
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


@app.patch("/api/admin-users/{upn}")
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


@app.delete("/api/admin-users/{upn}")
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


@app.put("/api/admin-users")
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


@app.get("/api/entra/users")
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






















# ── Routes: mailbox config ─────────────────────────────────────────────────────

@app.get("/api/templates")
async def api_get_templates(_=Depends(_check_auth)):
    """List available signature template names."""
    import signature_engine
    return {"templates": signature_engine.list_templates()}


@app.delete("/api/templates/{name}")
async def api_delete_template(name: str, _=Depends(_check_auth)):
    """Delete a named template (not 'default')."""
    if not name or name == "default":
        raise HTTPException(400, "Das 'default'-Template kann nicht gelöscht werden")
    html_path = Path(config.TEMPLATE_DIR) / f"{name}.html"
    txt_path = Path(config.TEMPLATE_DIR) / f"{name}.txt"
    meta_path = Path(config.TEMPLATE_DIR) / f"{name}.meta.json"
    deleted = []
    for p in (html_path, txt_path, meta_path):
        if p.exists():
            p.unlink()
            deleted.append(p.name)
    if not deleted:
        raise HTTPException(404, f"Template '{name}' nicht gefunden")
    import signature_engine
    signature_engine._reload_env()
    log.info("Template '%s' deleted", name)
    return {"ok": True, "deleted": deleted}


def _vorlagenverweise_umbenennen(alt: str, neu: str) -> dict:
    """Jeden gespeicherten Verweis auf eine Vorlage umschreiben.

    EINE Stelle, die alle Orte kennt — sonst vergisst die nächste Änderung
    einen davon, und ein Postfach zeigt still auf eine Vorlage, die es nicht
    mehr gibt. Der Signaturdienst fällt dann wortlos auf „default" zurück; der
    Betreiber merkt es erst an einer falschen Signatur.

    Orte (Stand 2026-08-03):
      MAILBOX_CONFIG[*]  -> "template", "min_template", "addin_templates" (Liste)
      TEMPLATE_POLICIES  -> "sig", "min", "addin"
      CUSTOM_POLICIES[*] -> "template"
    """
    geaendert: dict[str, int] = {}
    aenderungen: dict[str, object] = {}

    mc = settings_store.get("MAILBOX_CONFIG") or {}
    n_mc = 0
    for cfg in mc.values():
        if not isinstance(cfg, dict):
            continue
        for feld in ("template", "min_template"):
            if cfg.get(feld) == alt:
                cfg[feld] = neu
                n_mc += 1
        liste = cfg.get("addin_templates")
        if isinstance(liste, list) and alt in liste:
            cfg["addin_templates"] = [neu if x == alt else x for x in liste]
            n_mc += 1
    if n_mc:
        aenderungen["MAILBOX_CONFIG"] = mc
        geaendert["Postfächer"] = n_mc

    tp = settings_store.get("TEMPLATE_POLICIES") or {}
    n_tp = 0
    if isinstance(tp, dict):
        for feld in ("sig", "min", "addin"):
            if tp.get(feld) == alt:
                tp[feld] = neu
                n_tp += 1
    if n_tp:
        aenderungen["TEMPLATE_POLICIES"] = tp
        geaendert["Richtlinien"] = n_tp

    cp = settings_store.get("CUSTOM_POLICIES") or []
    n_cp = 0
    if isinstance(cp, list):
        for pol in cp:
            if isinstance(pol, dict) and pol.get("template") == alt:
                pol["template"] = neu
                n_cp += 1
    if n_cp:
        aenderungen["CUSTOM_POLICIES"] = cp
        geaendert["Eigene Richtlinien"] = n_cp

    if aenderungen:
        settings_store.update(aenderungen)
    return geaendert


@app.post("/api/templates/{name}/rename")
async def api_rename_template(name: str, request: Request, _=Depends(_check_auth)):
    """Vorlage umbenennen — samt aller Verweise darauf.

    Ohne das Nachziehen der Verweise wäre Umbenennen gefährlicher als Löschen:
    Beim Löschen fällt der Fehler sofort auf, beim Umbenennen zeigt ein
    Postfach stillschweigend ins Leere.
    """
    import shutil
    daten = await request.json()
    ziel = _re.sub(r"[^a-zA-Z0-9_\-]", "", (daten.get("ziel") or "").strip()).strip("-_")
    if not ziel:
        raise HTTPException(400, "Ungültiger Name (Buchstaben, Ziffern, - und _).")
    if ziel == "default":
        raise HTTPException(400, "Der Name 'default' ist vergeben.")

    quelle_safe = _re.sub(r"[^a-zA-Z0-9_\-]", "", name).strip("-_") or "default"
    if quelle_safe == "default":
        raise HTTPException(400, "Die Standardvorlage lässt sich nicht umbenennen.")
    if quelle_safe == ziel:
        raise HTTPException(400, "Alter und neuer Name sind gleich.")

    verz = Path(config.TEMPLATE_DIR)
    if not (verz / f"{quelle_safe}.html").exists():
        raise HTTPException(404, f"Vorlage '{name}' nicht gefunden.")
    if (verz / f"{ziel}.html").exists():
        raise HTTPException(409, f"Es gibt bereits eine Vorlage '{ziel}'.")

    verschoben = []
    for endung in ("html", "txt", "meta.json"):
        q = verz / f"{quelle_safe}.{endung}"
        if q.exists():
            shutil.move(str(q), str(verz / f"{ziel}.{endung}"))
            verschoben.append(endung)

    verweise = _vorlagenverweise_umbenennen(quelle_safe, ziel)
    import signature_engine
    signature_engine._reload_env()
    log.info("Template '%s' -> '%s' umbenannt (Verweise: %s)", quelle_safe, ziel, verweise or "keine")

    teile = [f"{n} {was}" for was, n in verweise.items()]
    return JSONResponse({
        "ok": True, "name": ziel, "moved": verschoben, "verweise": verweise,
        "message": (f"Umbenannt in '{ziel}'."
                    + (f" Nachgezogen: {', '.join(teile)}." if teile else "")),
    })


@app.post("/api/templates/{name}/create")
async def api_create_template(name: str, _=Depends(_check_auth)):
    """Leere Vorlage anlegen, damit sie sofort in der Auswahl steht.

    Bisher fuehrte „+ Neue Vorlage" nur auf die Bearbeitungsseite; auf der
    Platte entstand nichts. Die Vorlage tauchte deshalb erst nach dem ersten
    Speichern in der Liste auf — wer zwischendurch wegnavigierte, fand seine
    Arbeit nicht wieder und legte sie ein zweites Mal an.
    """
    safe = _re.sub(r"[^a-zA-Z0-9_\-]", "", name).strip("-_")
    if not safe:
        raise HTTPException(400, "Ungültiger Name (Buchstaben, Ziffern, - und _).")
    if safe == "default":
        raise HTTPException(400, "Der Name 'default' ist vergeben.")
    verz = Path(config.TEMPLATE_DIR)
    if (verz / f"{safe}.html").exists():
        raise HTTPException(409, f"Es gibt bereits eine Vorlage '{safe}'.")

    (verz / f"{safe}.html").write_text("", encoding="utf-8")
    (verz / f"{safe}.txt").write_text("", encoding="utf-8")
    import signature_engine
    signature_engine._reload_env()
    log.info("Template '%s' angelegt (leer) von %s", safe, _)
    return JSONResponse({"ok": True, "name": safe})


@app.post("/api/templates/{name}/duplicate")
async def api_duplicate_template(name: str, request: Request, _=Depends(_check_auth)):
    """Vorlage kopieren — samt Blockliste, damit die Kopie im Baukasten bleibt.

    WARUM ES DAS BRAUCHT
    Den HTML-Quelltext einer Baukasten-Vorlage in eine neue zu kopieren ergibt
    eine Vorlage OHNE `.meta.json`. Sie funktioniert, lässt sich aber nur noch
    als Quelltext bearbeiten: das HTML ist das Erzeugnis, die Blockliste ist die
    Quelle. Eine Rückübersetzung gibt es nicht und wäre auch nicht verlässlich —
    aus fertigem HTML ließe sich nicht ablesen, welche Blöcke es einmal waren.

    Deshalb kopiert dieser Weg alle drei Dateien. Fehlt der Quelle die
    `.meta.json` (von Hand geschriebene Vorlage), wird das ausdrücklich
    gemeldet, statt stillschweigend eine nicht mehr baukastenfähige Kopie zu
    hinterlassen.
    """
    import shutil
    daten = await request.json()
    ziel_roh = (daten.get("ziel") or "").strip()
    ziel = _re.sub(r"[^a-zA-Z0-9_\-]", "", ziel_roh).strip("-_")
    if not ziel:
        raise HTTPException(400, "Bitte einen Namen für die Kopie angeben "
                                 "(Buchstaben, Ziffern, - und _).")
    if ziel == "default":
        raise HTTPException(400, "Der Name 'default' ist vergeben.")

    quelle_safe = _re.sub(r"[^a-zA-Z0-9_\-]", "", name).strip("-_") or "default"
    quelle = "signature" if quelle_safe == "default" else quelle_safe
    verz = Path(config.TEMPLATE_DIR)
    if not (verz / f"{quelle}.html").exists():
        raise HTTPException(404, f"Vorlage '{name}' nicht gefunden.")
    if (verz / f"{ziel}.html").exists():
        raise HTTPException(409, f"Es gibt bereits eine Vorlage '{ziel}'.")

    kopiert = []
    for endung in ("html", "txt", "meta.json"):
        q = verz / f"{quelle}.{endung}"
        if q.exists():
            shutil.copyfile(q, verz / f"{ziel}.{endung}")
            kopiert.append(endung)

    import signature_engine
    signature_engine._reload_env()
    baukasten = "meta.json" in kopiert
    log.info("Template '%s' dupliziert nach '%s' (Baukasten: %s)",
             quelle, ziel, baukasten)
    return JSONResponse({
        "ok": True, "name": ziel, "builder": baukasten, "copied": kopiert,
        "message": (f"Kopie '{ziel}' angelegt — im Baukasten bearbeitbar."
                    if baukasten else
                    f"Kopie '{ziel}' angelegt. Die Vorlage hat keine Baukasten-Daten "
                    f"und lässt sich nur als Quelltext bearbeiten."),
    })


@app.post("/api/templates/{name}/parse")
async def api_parse_template(name: str, request: Request, _=Depends(_check_auth)):
    """HTML in eine Blockliste zurücklesen — als VORSCHLAG, nichts wird gespeichert.

    Der Editor ruft das beim Wechsel auf den Baukasten, wenn eine Vorlage nur
    als Quelltext vorliegt. Erst ein anschliessendes Speichern macht die
    Umwandlung verbindlich; bis dahin bleibt die Vorlage unangetastet. So kann
    der Nutzer das Ergebnis in der Vorschau vergleichen und ablehnen.
    """
    import template_parser as _tp
    try:
        daten = await request.json()
    except Exception:
        daten = {}
    html_roh = daten.get("html")
    if html_roh is None:
        safe = _re.sub(r"[^a-zA-Z0-9_\-]", "", name).strip("-_") or "default"
        fname = "signature" if safe == "default" else safe
        pfad = Path(config.TEMPLATE_DIR) / f"{fname}.html"
        if not pfad.exists():
            raise HTTPException(404, f"Vorlage '{name}' nicht gefunden.")
        html_roh = pfad.read_text(encoding="utf-8")

    meta = _tp.parse_html(html_roh)
    hinweise = meta.pop("_hinweise", [])
    import template_builder as _tb
    # Die Vorschau kommt aus dem Vorschlag selbst, nicht aus der Quelle: nur so
    # sieht der Nutzer, was NACH dem Speichern herauskaeme.
    return JSONResponse({"ok": True, "meta": meta, "hinweise": hinweise,
                         "html": _tb.render_html(meta),
                         "blocks": len(meta.get("blocks") or [])})


@app.get("/api/templates/{name}/meta")
async def api_get_template_meta(name: str, _=Depends(_check_auth)):
    """Return the builder meta JSON for a template, or 404 if none exists."""
    safe = _re.sub(r"[^a-zA-Z0-9_\-]", "", name).strip("-_") or "default"
    fname = "signature" if safe == "default" else safe
    meta_path = Path(config.TEMPLATE_DIR) / f"{fname}.meta.json"
    if not meta_path.exists():
        # ⚠️ Für Nachrichten an Postfachinhaber ist „keine Datei" kein Fehler,
        # sondern der Normalfall: Solange niemand sie bearbeitet hat, gilt die
        # mitgelieferte Fassung. Ohne diesen Zweig zeigte der Editor eine LEERE
        # Vorlage — und Speichern hätte den Text gelöscht, den der Empfänger
        # braucht, um die CA-Mail von Phishing zu unterscheiden.
        schluessel = _usermail_key(fname)
        if schluessel:
            import usermail
            return JSONResponse(usermail.standard_meta(schluessel))
        raise HTTPException(404, "Kein Builder-Meta für diese Vorlage")
    import json as _json
    return JSONResponse(_json.loads(meta_path.read_text()))


@app.post("/api/templates/{name}/meta")
async def api_save_template_meta(name: str, request: Request, _=Depends(_check_auth)):
    """Save builder meta JSON, regenerate .html and .txt from it."""
    import json as _json
    import template_builder as _tb
    import signature_engine
    safe = _re.sub(r"[^a-zA-Z0-9_\-]", "", name).strip("-_") or "default"
    fname = "signature" if safe == "default" else safe
    try:
        meta = await request.json()
    except Exception:
        raise HTTPException(400, "Ungültiges JSON")
    if not isinstance(meta, dict) or "blocks" not in meta:
        raise HTTPException(400, "Meta-JSON muss 'blocks' enthalten")
    meta.setdefault("version", 1)
    if not (meta.get("blocks") or []):
        # Eine leere Bausteinliste ergibt eine leere Vorlage — und jede damit
        # versandte Mail traegt gar keine Signatur mehr. Am 02.08.2026 ist
        # genau das passiert: Die Ruecklesung lieferte nichts, gespeichert
        # wurde trotzdem, und die Vorlage war weg.
        raise HTTPException(400, "Die Vorlage enthält keine Bausteine. "
                                 "Speichern würde die Signatur löschen.")

    # Ein Fehler beim Erzeugen darf NIE als nackter Serverfehler herauskommen:
    # Der Editor bekommt dann HTML statt JSON und meldet „Speichern
    # fehlgeschlagen: … is not valid JSON" — eine Meldung, aus der niemand auf
    # sein Eingabefeld schliessen kann. Genau so gemeldet am 06.08.2026, nachdem
    # in ein px-Feld „12pt" getippt worden war.
    try:
        html_content = _tb.render_html(meta)
        txt_content = _tb.render_txt(meta)
    except Exception as exc:
        log.error("Template '%s' liess sich nicht erzeugen: %s", safe, exc, exc_info=True)
        raise HTTPException(400, f"Die Vorlage liess sich nicht erzeugen: {exc}. "
                                 f"Bitte die Eingaben prüfen — nicht gespeichert.")

    # Erzeugt der Baukasten ein UNGUELTIGES Template, waere die Signatur beim
    # Versand leer — sichtbar wird das erst beim Empfaenger. Deshalb hier
    # pruefen und die Datei gar nicht erst schreiben.
    try:
        import jinja2
        jinja2.Environment().parse(html_content)
        jinja2.Environment().parse(txt_content)
    except Exception as exc:
        log.error("Template '%s' waere unbrauchbar geworden: %s", safe, exc)
        raise HTTPException(400, f"Die erzeugte Vorlage ist kein gültiges "
                                 f"Template ({exc}). Nicht gespeichert — die "
                                 f"bisherige Fassung bleibt erhalten.")

    # Sicherung der bisherigen Fassung, bevor sie ueberschrieben wird.
    alt = Path(config.TEMPLATE_DIR) / f"{fname}.html"
    if alt.exists():
        try:
            (Path(config.TEMPLATE_DIR) / f"{fname}.html.bak").write_text(
                alt.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception as exc:
            log.warning("Sicherung von %s fehlgeschlagen: %s", fname, exc)
    # Entweder alle drei Dateien oder keine — und ueber tmp+replace().
    #
    # Vorher liefen hier drei nackte write_text() hintereinander. Zwei Fehler
    # steckten darin, beide am 07.08.2026 auf der Azure-VM aufgeschlagen:
    #
    # 1. Ein Fehlschlag beim zweiten Aufruf hinterliess die neue meta.json neben
    #    dem alten HTML. Baukasten und ausgelieferte Signatur waren dann
    #    verschieden, ohne dass es irgendwo auffiel.
    # 2. `write_text()` oeffnet die ZIELDATEI. Gehoert die einem anderen Nutzer
    #    (auf der VM schrieb der als root laufende Deploy die Vorlagen), scheitert
    #    das mit EACCES — obwohl das Verzeichnis dem Dienst gehoert. `replace()`
    #    braucht nur Schreibrecht am VERZEICHNIS und kommt damit durch.
    #
    # Der Fehler kam als nackter 500 heraus; der Editor bekam HTML statt JSON und
    # meldete „Unexpected token 'I', "Internal S"... is not valid JSON". Aus so
    # einer Meldung ist die Ursache nicht zu erraten.
    ziele = [
        (Path(config.TEMPLATE_DIR) / f"{fname}.meta.json",
         _json.dumps(meta, ensure_ascii=False, indent=2)),
        (Path(config.TEMPLATE_DIR) / f"{fname}.html", html_content),
        (Path(config.TEMPLATE_DIR) / f"{fname}.txt", txt_content),
    ]
    fertig: list[tuple[Path, Path]] = []
    try:
        for ziel, inhalt in ziele:
            tmp = ziel.parent / f"{ziel.name}.tmp"
            tmp.write_text(inhalt, encoding="utf-8")
            # Rechte auf der TEMP-Datei setzen, nicht auf dem Ziel: replace()
            # uebernimmt die der Quelldatei (dieselbe Falle wie in
            # settings_store._save(), dort mit 600 statt 644).
            tmp.chmod(0o644)
            fertig.append((tmp, ziel))
        for tmp, ziel in fertig:
            tmp.replace(ziel)
    except OSError as exc:
        for tmp, _ziel in fertig:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        log.error("Vorlage '%s' liess sich nicht schreiben: %s", safe, exc, exc_info=True)
        raise HTTPException(500, f"Die Vorlage liess sich nicht schreiben: {exc}. "
                                 f"Nicht gespeichert — die bisherige Fassung bleibt "
                                 f"erhalten. Meist sind es die Zugriffsrechte auf "
                                 f"dem Vorlagenverzeichnis.")
    signature_engine._reload_env()
    log.info("Template '%s' saved via builder by %s", safe, _)
    return {"ok": True, "html": html_content, "txt": txt_content}


@app.get("/api/health/mailboxes")
async def api_health_mailboxes(_=Depends(_require_admin)):
    """Return current cached MAILBOX_HEALTH data."""
    return settings_store.get("MAILBOX_HEALTH") or {}


@app.post("/api/health/mailboxes")
async def api_health_run(_=Depends(_require_admin)):
    """Run health checks for all configured mailboxes and return results."""
    import health_check
    results = await health_check.run_all_checks()
    return {"ok": True, "results": results}


@app.get("/api/health/audit-log")
async def api_health_audit_log(_=Depends(_require_admin)):
    """Return GATEWAY_AUDIT_LOG entries."""
    return settings_store.get("GATEWAY_AUDIT_LOG") or []


# ── Routes: authenticated pages ────────────────────────────────────────────────

_DE_MONTHS = ["Januar","Februar","März","April","Mai","Juni",
              "Juli","August","September","Oktober","November","Dezember"]


def _prev_month(year: int, month: int, delta: int = 1) -> tuple[int, int]:
    """Return (year, month) shifted back by delta months."""
    m = month - delta
    while m < 1:
        m += 12
        year -= 1
    return year, m


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: str = Depends(_check_auth)):
    # Solange das Setup nicht abgeschlossen ist, immer im Wizard landen (statt Dashboard) —
    # gilt für alle Login-Wege (lokal/SSO) und nach Session-Ablauf.
    if not settings_store.get("SETUP_COMPLETE"):
        return RedirectResponse("/setup", status_code=302)
    # Die Übersicht zeigt Betriebsdaten — Postverkehr, Protokollauszug, Lizenz,
    # Graph-Kontingent. Der Signatur-Editor pflegt Vorlagen und Inhalte und hat
    # damit nichts zu tun; er startet direkt im Editor.
    #
    # Bewusst eine Weiterleitung statt `Depends(_require_admin)`: Diese Adresse
    # ist die Startseite. Ein 403 wäre für einen Editor, der das Lesezeichen
    # öffnet, eine Sackgasse — die Weiterleitung bringt ihn dorthin, wo er
    # arbeiten kann. Die Daten selbst schützen die Endpunkte darunter, die
    # allesamt die Verwaltungsrolle verlangen.
    if _get_session_role(request) != sso_mod.ROLE_ADMIN:
        return RedirectResponse("/template", status_code=302)
    from datetime import datetime as _dt
    import smime_store as _smime_store
    import stats as _stats_mod2
    pw_change = _password_change_required()
    total = get_stats()
    daily = _stats_mod2.get_today()
    now = _dt.now()
    monthly = _stats_mod2.get_period(now.year, now.month)
    yearly  = _stats_mod2.get_period(now.year)
    prev_year = now.year - 1
    m1y, m1m = _prev_month(now.year, now.month, 1)
    m2y, m2m = _prev_month(now.year, now.month, 2)
    signing_certs = _smime_store.list_certs()
    recipient_certs = _smime_store.list_recipient_certs()
    warn_days = int(settings_store.get("CERT_WARN_DAYS") or 14)
    expiring_certs = [c for c in signing_certs + recipient_certs
                      if not c.get("error") and c.get("days_left", 999) <= warn_days]
    return templates.TemplateResponse(
        request=request, name="dashboard.html",
        context={
            "stats": total,
            "stats_daily": daily,
            "stats_3d": _stats_mod2.get_last_n_days(3),
            "date_3d_from": (now - __import__("datetime").timedelta(days=2)).strftime("%Y-%m-%d"),
            "stats_monthly": monthly,
            "stats_monthly_m1": _stats_mod2.get_period(m1y, m1m),
            "stats_monthly_m2": _stats_mod2.get_period(m2y, m2m),
            "stats_yearly": yearly,
            "stats_prev_yearly": _stats_mod2.get_period(prev_year),
            "stats_month_label": f"{_DE_MONTHS[now.month - 1]} {now.year}",
            "stats_month_m1_label": f"{_DE_MONTHS[m1m - 1]} {m1y}",
            "stats_month_m2_label": f"{_DE_MONTHS[m2m - 1]} {m2y}",
            "stats_year_label": str(now.year),
            "stats_prev_year_label": str(prev_year),
            "today": now.strftime("%Y-%m-%d"),
            "today_month": now.strftime("%Y-%m"),
            "today_year": str(now.year),
            "prev_month_1": f"{m1y:04d}-{m1m:02d}",
            "prev_month_2": f"{m2y:04d}-{m2m:02d}",
            "prev_year_str": str(prev_year),
            "cert_expiry": _cert_expiry(),
            "signing_certs": signing_certs,
            "expiring_certs": expiring_certs,
            "active": "dashboard",
            "password_change_needed": pw_change,
            "gateway_name": _gateway_name(),
            "show_welcome_banner": not settings_store.get("WELCOME_DISMISSED"),
        },
    )


def _usermail_liste() -> list[dict]:
    """Die bekannten Nachrichten an Postfachinhaber für die Auswahl im Editor."""
    import usermail
    return [{"key": k,
             "name": usermail.dateiname(k),
             "anzeige": v["anzeige"],
             "zweck": v["zweck"],
             "ist_standard": usermail.ist_standard(k)}
            for k, v in usermail.VORLAGEN.items()]


def _usermail_key(fname: str) -> str:
    """Schlüssel, falls die gerade geöffnete Vorlage eine Nutzer-Mail ist."""
    import usermail
    for k in usermail.VORLAGEN:
        if usermail.dateiname(k) == fname:
            return k
    return ""


@app.post("/api/usermails/{schluessel}/standard")
async def api_usermail_standard(schluessel: str, _=Depends(_check_auth)):
    """Die mitgelieferte Fassung wiederherstellen.

    Sie wird geschrieben wie eine bearbeitete Vorlage — dieselbe Datenstruktur,
    derselbe Weg. Deshalb ist die wiederhergestellte Fassung anschliessend ganz
    normal weiter bearbeitbar und nicht etwa schreibgeschützt.
    """
    import usermail
    import template_builder as _tb
    if not usermail.ist_bekannt(schluessel):
        raise HTTPException(404, "Unbekannte Nachricht")
    meta = usermail.standard_meta(schluessel)
    fname = usermail.dateiname(schluessel)
    verz = Path(config.TEMPLATE_DIR)
    (verz / f"{fname}.meta.json").write_text(
        _json_mod.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (verz / f"{fname}.html").write_text(_tb.render_html(meta), encoding="utf-8")
    (verz / f"{fname}.txt").write_text(_tb.render_txt(meta), encoding="utf-8")
    log.info("Nutzer-Mail %s auf die mitgelieferte Fassung zurückgesetzt", schluessel)
    return JSONResponse({"ok": True})


@app.get("/api/usermails")
async def api_usermails(_=Depends(_check_auth)):
    return JSONResponse({"usermails": _usermail_liste()})


@app.get("/template", response_class=HTMLResponse)
async def template_editor(request: Request, user: str = Depends(_check_auth)):
    import signature_engine as _sig_engine
    name = request.query_params.get("name") or "default"
    fname = "signature" if name == "default" else name
    html_path = Path(config.TEMPLATE_DIR) / f"{fname}.html"
    txt_path = Path(config.TEMPLATE_DIR) / f"{fname}.txt"
    meta_path = Path(config.TEMPLATE_DIR) / f"{fname}.meta.json"
    template_list = _sig_engine.list_templates()
    custom_vars = [cv["name"] for cv in (settings_store.get("CUSTOM_TEMPLATE_VARS") or []) if cv.get("name")]
    return templates.TemplateResponse(
        request=request, name="template_editor.html",
        context={
            "html_content": html_path.read_text() if html_path.exists() else "",
            "txt_content": txt_path.read_text() if txt_path.exists() else "",
            # Für Nachrichten an Postfachinhaber IMMER wahr: Ohne eigene
            # Datei liefert der Meta-Endpunkt die mitgelieferte Fassung, und
            # der Editor soll sie laden statt leer zu bleiben.
            "has_meta": meta_path.exists() or bool(_usermail_key(fname)),
            # Wurde der Quelltext NACH dem letzten Baukasten-Speichern
            # geaendert? Dann sind die Bausteine veraltet, und der Editor bietet
            # an, sie aus dem Quelltext neu zu lesen.
            #
            # Genau dafuer wurde die Ruecklesung gebaut: Handaenderungen am
            # HTML sollen in den Baukasten UEBERNOMMEN werden. Ohne diese
            # Pruefung griff sie nur bei Vorlagen ohne Bausteine — wer eine
            # Baukasten-Vorlage von Hand nachbesserte, sah beim naechsten
            # Oeffnen die alten Bausteine und verlor seine Arbeit beim
            # Speichern.
            "quelltext_neuer": (
                meta_path.exists() and html_path.exists()
                and html_path.stat().st_mtime > meta_path.stat().st_mtime + 1
            ),
            "active": "template",
            "saved": request.query_params.get("saved"),
            "current_template": name,
            "template_list": template_list,
            "custom_vars": custom_vars,
            "gateway_name": _gateway_name(),
            # Nachrichten an Postfachinhaber. Sie liegen im selben Verzeichnis
            # und werden im selben Baukasten bearbeitet, stehen aber in einer
            # EIGENEN Auswahl — in der Signaturliste hätten sie nichts zu
            # suchen, dort wäre eine Zuweisung ein Klick.
            "usermails": _usermail_liste(),
            "usermail_key": _usermail_key(fname),
        },
    )


@app.post("/template", response_class=HTMLResponse)
async def template_save(
    request: Request,
    html_content: str = Form(""),
    txt_content: str = Form(""),
    template_name: str = Form("default"),
    user: str = Depends(_check_auth),
):
    # Sanitise template_name: only allow alphanumeric, dash, underscore
    import re as _re2
    safe_name = _re2.sub(r"[^a-zA-Z0-9_\-]", "", template_name).strip("-_") or "default"
    if safe_name == "default":
        html_path = Path(config.TEMPLATE_DIR, "signature.html")
        txt_path = Path(config.TEMPLATE_DIR, "signature.txt")
    else:
        html_path = Path(config.TEMPLATE_DIR, f"{safe_name}.html")
        txt_path = Path(config.TEMPLATE_DIR, f"{safe_name}.txt")
    html_path.write_text(html_content)
    txt_path.write_text(txt_content)
    signature_engine._reload_env()
    log.info("Template '%s' saved by user %s", safe_name, user)
    return RedirectResponse(url=f"/template?name={safe_name}&saved=1", status_code=303)


@app.get("/preview", response_class=HTMLResponse)
async def preview(request: Request, email: str = "", user: str = Depends(_check_auth)):
    return templates.TemplateResponse(
        request=request, name="preview.html",
        context={"email": email, "active": "preview", "gateway_name": _gateway_name()},
    )


@app.get("/api/preview-data")
async def api_preview_data(
    email: str = "",
    template: str = "default",
    banner: str = "",
    disclaimer: str = "",
    explizit: bool = False,
    user: str = Depends(_check_auth),
):
    """Render a signature template for a given email address (Graph lookup).
    Also renders the configured banner and disclaimer (or explicit params) and
    returns them as `banner_html` / `disclaimer_html`.

    `explizit=1` bedeutet: Die drei Vorlagennamen sind VERBINDLICH, ein leerer
    Wert heisst „keine" und nicht „nimm die aus der Postfach-Konfiguration".

    Ohne dieses Kennzeichen liesse sich „ausdruecklich keiner" gar nicht
    ausdruecken — ein leerer Banner faellt sonst auf die Konfiguration zurueck.
    Genau das braucht aber die Vorschau-Seite, auf der Signatur, Banner und
    Disclaimer frei zusammengestellt werden. Die Live-Vorschau im Baukasten
    schickt das Kennzeichen NICHT: dort soll stehen, was das Postfach
    tatsaechlich bekaeme."""
    import graph_client as _gc
    import mailbox_match

    # ⚠️ Nachrichten an Postfachinhaber gehen einen ANDEREN Weg als Signaturen:
    # Sie kennen weder `user` noch `custom`, sondern `empfaenger` und `ca`.
    # Ohne diesen Zweig rendert die Vorschau sie mit dem Signatur-Kontext —
    # die Platzhalter sind dort unbekannt und werden LEER eingesetzt. Im Editor
    # stand dann „Für Ihre Adresse  wird ein Zertifikat…", und es sah aus, als
    # sei die Vorlage kaputt.
    schluessel = _usermail_key(template or "")
    if schluessel:
        import usermail
        ergebnis = usermail.rendern(
            schluessel,
            email or "vorname.nachname@example.org",
            (settings_store.get("CA_ANZEIGENAME") or "").strip() or "Ihrer Zertifizierungsstelle")
        betreff, rumpf = ergebnis if ergebnis else ("", "")
        return JSONResponse({"html": rumpf, "txt": "", "betreff": betreff,
                             "banner_html": "", "disclaimer_html": "", "error": None})

    user_data = _gc.UserData()
    error = None
    if email:
        try:
            user_data = await _gc.get_user(email)
        except Exception as exc:
            error = str(exc)
    if explizit and not template:
        sig_html, sig_txt = "", ""
    else:
        sig_html, sig_txt = signature_engine.render(user_data, template_name=template)
    # Resolve banner and disclaimer: explicit param > mailbox config
    if not explizit and email and (not banner or not disclaimer):
        _mc = settings_store.get("MAILBOX_CONFIG") or {}
        _cfg = mailbox_match.match_sender(_mc, email)
        if not banner:
            banner = _cfg.get("banner_template", "")
        if not disclaimer:
            disclaimer = _cfg.get("disclaimer_template", "")
    banner_html = ""
    if banner:
        banner_html, _ = signature_engine.render(user_data, template_name=banner)
    disclaimer_html = ""
    if disclaimer:
        disclaimer_html, _ = signature_engine.render(user_data, template_name=disclaimer)
    return JSONResponse({"html": sig_html, "txt": sig_txt, "error": error,
                         "banner_html": banner_html, "banner_template": banner,
                         "disclaimer_html": disclaimer_html, "disclaimer_template": disclaimer})


@app.get("/api/cert/catalog")
async def api_cert_catalog(_=Depends(_require_admin)):
    """Anbieter-Katalog des Hubs für die Anzeige (Anbindung-Seite).
    Erzwingt immer einen frischen Hub-Fetch (Admin-Endpunkt, selten aufgerufen)."""
    import hub_catalog as _hub_cat
    import hub_client
    try:
        await _hub_cat.refresh(force=True)
    except Exception:
        pass
    return JSONResponse({
        "registered": hub_client.cert_is_registered(),
        "providers": _hub_cat.cached(),
        "disabled": settings_store.get("CATALOG_PROVIDERS_DISABLED") or [],
        "currency": _hub_cat.currency(),
        "vat_percent": _hub_cat.vat_percent(),
    })


@app.post("/api/cert/catalog/toggle")
async def api_cert_catalog_toggle(body: dict, user: str = Depends(_require_admin)):
    """Anbieter lokal an-/abwählen — abgewählte erscheinen nicht in der
    Backend-Auswahl pro Postfach (bereits bestehende Zuordnungen bleiben)."""
    pid = (body.get("provider_id") or "").strip()
    enabled = bool(body.get("enabled"))
    if not pid:
        raise HTTPException(400, "provider_id fehlt")
    disabled = list(settings_store.get("CATALOG_PROVIDERS_DISABLED") or [])
    if enabled:
        disabled = [d for d in disabled if d != pid]
    elif pid not in disabled:
        disabled.append(pid)
    settings_store.update({"CATALOG_PROVIDERS_DISABLED": disabled})
    log.info("Katalog-Anbieter %s %s von %s", pid,
             "aktiviert" if enabled else "abgewählt", user)
    return JSONResponse({"ok": True, "disabled": disabled})


# ── Fair-Use-Lizenz ──────────────────────────────────────────────────────────

@app.get("/api/license/status")
async def api_license_status(_=Depends(_require_admin)):
    import license as _lic
    return JSONResponse(_lic.fair_use_state())


@app.post("/api/license")
async def api_license_set(body: dict, user: str = Depends(_require_admin)):
    """Lizenzschlüssel einspielen — Offline-Prüfung (Signatur + Tenant)."""
    import license as _lic
    key = (body.get("key") or "").strip()
    if not key:
        raise HTTPException(400, "Lizenzschlüssel fehlt")
    payload, err = _lic.verify(key)
    if err:
        raise HTTPException(400, err)
    terr = _lic.tenant_error(payload)
    if terr:
        raise HTTPException(400, terr)
    settings_store.update({"LICENSE_KEY": key})
    log.info("Lizenz eingespielt von %s: lic_id=%s customer=%s mailboxes=%s",
             user, payload.get("lic_id"), payload.get("customer"), payload.get("mailboxes"))
    return JSONResponse(_lic.fair_use_state())


@app.post("/api/license/fetch-hub")
async def api_license_fetch_hub(user: str = Depends(_require_admin)):
    """Lizenz automatisch über die Hub-Anbindung abrufen und einspielen."""
    import hub_client
    import license as _lic
    res = await hub_client.get_license()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error") or "Abruf fehlgeschlagen")
    key = res.get("license") or ""
    payload, err = _lic.verify(key)
    if err:
        raise HTTPException(400, f"Hub lieferte ungültige Lizenz: {err}")
    terr = _lic.tenant_error(payload)
    if terr:
        raise HTTPException(400, terr)
    settings_store.update({"LICENSE_KEY": key})
    log.info("Lizenz via Hub abgerufen von %s: lic_id=%s", user, payload.get("lic_id"))
    return JSONResponse(_lic.fair_use_state())


@app.get("/api/license/renewal")
async def api_license_renewal(user: str = Depends(_require_admin)):
    """Verlängerungszustand der Lizenz beim Hub abfragen.

    Getrennt von /api/license/status, weil dieser Weg das Netz braucht: der
    Fair-Use-Zustand kommt offline aus dem hinterlegten Schlüssel und darf
    nicht davon abhängen, ob der Hub gerade erreichbar ist.
    """
    import hub_client
    if not hub_client.is_registered():
        return JSONResponse({"ok": False, "reason": "not_connected"})
    res = await hub_client.get_license()
    if not res.get("ok"):
        return JSONResponse({"ok": False, "reason": "unavailable",
                             "error": res.get("error") or ""})
    # Durchreichen statt aufzaehlen — siehe hub_client.get_license(). Der
    # `license`-Schluessel wird entfernt: die Oberflaeche braucht ihn nicht,
    # und er gehoert nicht ohne Anlass ins Browserfenster.
    return JSONResponse({k: v for k, v in res.items() if k != "license"})


# Ein Durchreicher für ALLE Abo-Aktionen statt einer je Aktion.
#
# Vorher stand hier je Handlung ein eigener Endpunkt, der die Felder von Hand
# abschrieb. Genau daran ging am 27.07.2026 dreimal an einem Tag dieselbe
# Zahlungsweise verloren: beim Kauf, in der Ansicht, beim Umschalten. Eine
# Schicht, die aufzählt, vergisst irgendwann etwas — eine, die durchreicht,
# kann es nicht.
# Aktion → benötigter Zustimmungskontext, oder None.
#
# ⚠️ NICHT als Menge, sondern als Zuordnung. Als der alte Kaufendpunkt hier
# aufging, blieb sein `context_consented("license_purchase")` zurück — und der
# Kauf lief ohne Zustimmung durch. Eine Menge lädt dazu ein, die Wache zu
# vergessen; eine Zuordnung zwingt dazu, für JEDE Aktion zu entscheiden.
#
# `cancel` und `portal` sind bewusst frei: wer geänderten Bedingungen NICHT
# zustimmt, muss beenden können. Eine Kündigung an die Zustimmung zu den
# Bedingungen zu binden, die man gerade ablehnt, wäre eine Falle.
_HUB_AKTIONEN = {
    "checkout": "license_purchase",
    "quantity": "license_purchase",
    "zahlungsweise": "license_purchase",
    "cancel": None,
    "portal": None,
}


@app.post("/api/license/hub/{aktion}")
async def api_license_hub(aktion: str, body: dict | None = None,
                          user: str = Depends(_require_admin)):
    """Abo-Aktion an den Hub weiterreichen. Antwort unverändert zurück."""
    if aktion not in _HUB_AKTIONEN:
        raise HTTPException(404, f"Unbekannte Aktion: {aktion}")
    import legal_consent          # lokal, wie überall sonst in dieser Datei
    kontext = _HUB_AKTIONEN[aktion]
    if kontext and not legal_consent.context_consented(kontext):
        raise HTTPException(403, "Den aktuellen Fassungen der Rechtsdokumente wurde "
                                 "noch nicht zugestimmt. Sie stehen im Abschnitt "
                                 "'Rechtliche Dokumente' auf dieser Seite.")
    import hub_client
    # Die Tenant-ID kennt NUR das Gateway — sie ist der Kopierschutz-Anker der
    # Lizenz. Deshalb wird sie hier ergänzt und nicht von der Oberfläche
    # geschickt, wo sie manipulierbar wäre.
    nutzlast = dict(body or {})
    nutzlast["tenant_id"] = (settings_store.get("TENANT_ID") or "").strip()
    # Welche Fassungen hier gerade gelten. Der Hub hat die Dokumente nicht und
    # könnte die Aktualität eines Belegs sonst gar nicht beurteilen — er liess
    # einen Beleg über eine ÜBERHOLTE Fassung durchgehen.
    nutzlast["doc_versions"] = legal_consent.current_versions()
    res = await hub_client._license_json(aktion, nutzlast)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error") or "Der Hub hat abgelehnt.")
    log.info("Lizenz-Abo: %s durch %s", aktion, user)
    return JSONResponse(res)



@app.delete("/api/license")
async def api_license_delete(user: str = Depends(_require_admin)):
    settings_store.update({"LICENSE_KEY": ""})
    log.info("Lizenz entfernt von %s", user)
    return JSONResponse({"ok": True})




# ── Wartungsmodus / Held Mails ────────────────────────────────────────────────

@app.get("/api/maintenance/mails")
async def api_held_mails_list(_: str = Depends(_require_admin)):
    return JSONResponse({
        "maintenance_mode": bool(settings_store.get("MAINTENANCE_MODE")),
        "mails": _held_mails_mod.list_all(),
    })


@app.get("/api/maintenance/mails/{mail_id}/preview", response_class=HTMLResponse)
async def api_held_mail_preview(mail_id: str, _: str = Depends(_require_admin)):
    html = _held_mails_mod.get_preview_html(mail_id)
    if html is None:
        raise HTTPException(404, "Mail not found")
    return HTMLResponse(html or "<em>(kein HTML-Inhalt)</em>")


@app.delete("/api/maintenance/mails/{mail_id}")
async def api_held_mail_delete(mail_id: str, _: str = Depends(_require_admin)):
    if not _held_mails_mod.delete(mail_id):
        raise HTTPException(404, "Mail not found")
    return JSONResponse({"ok": True})


def _sent_items_nach_freigabe_aufraeumen(sender: str, empfaenger: list[str],
                                         roh: bytes) -> None:
    """Nach dem Freigeben dieselbe Aufräumung wie im normalen Weg anstossen.

    Der normale Weg (handler.py, nach „Mail processed OK") plant
    `_cleanup_sent_item` ein: Exchange legt ein gesendetes Element an, wenn der
    Nutzer sendet, und ein weiteres, wenn das Gateway die Mail neu einspeist —
    der Aufräumer behält davon genau eines.

    Die Freigabe aus dem Wartungsmodus rief nur `reinject.send()` und ging
    ohne diesen Schritt zu Ende. Das fiel doppelt ins Gewicht, weil Exchange
    eine Mail an mehrere Empfänger in getrennte Vorgänge aufteilt: Jeder
    Vorgang wird einzeln zurückgehalten, einzeln freigegeben und legt sein
    eigenes gesendetes Element an. Am 07.08.2026 nachgewiesen — eine Rechnung
    an zwei Empfänger stand dreifach in „Gesendete Elemente": das Original des
    Absenders (`X-Mailer: lxo`) und zwei Fassungen des Gateways.

    `_is_first_for_mid()` sorgt dafür, dass die Aufräumung je Nachricht nur
    einmal läuft, auch wenn mehrere Vorgänge derselben Mail freigegeben werden.
    """
    import email as _em
    import handler as _handler
    import mail_processor as _mp

    if not settings_store.get("SENT_ITEMS_UPDATE"):
        return
    try:
        msg = _em.message_from_bytes(roh)
    except Exception as exc:                      # pragma: no cover
        log.warning("Freigabe: Mail nicht lesbar, keine Aufräumung: %s", exc)
        return

    mid = (msg.get("Message-ID") or "").strip()
    if not mid or not _handler._is_first_for_mid(sender, mid):
        return

    # Verschlüsselte Mail: NICHT anfassen.
    #
    # Der Aufräumer behält bei `replace_all=False` die JÜNGSTE Fassung — das
    # wäre hier die Chiffre, und der Absender könnte seine eigene gesendete
    # Mail nicht mehr lesen. Der normale Weg löst das mit dem Klartext, den er
    # vor dem Verschlüsseln noch hat; hier liegt nur die verschlüsselte
    # Fassung vor. Lieber ein Duplikat zu viel als ein unlesbares Postfach.
    #
    # Über ALLE Teile, nicht nur die oberste Ebene: Eine verschlüsselte Mail
    # kann in einer Hülle stecken, deren erster Teil ein lesbarer Hinweis ist
    # („Diese Nachricht ist verschlüsselt…"). Auf oberster Ebene stünde dann
    # `multipart/mixed`, eine Prüfung nur dort liefe ins Leere — und
    # `extract_html()` fände ausgerechnet diesen Hinweistext, mit dem dann die
    # einzige verbleibende Fassung überschrieben würde. Die reine Form fällt
    # ohnehin schon durch die `html`-Prüfung darunter; geprüft wird hier also
    # genau der Fall, den sie NICHT abfängt.
    if any((t.get_content_type() or "").lower() == "application/pkcs7-mime"
           and "enveloped" in (t.get_param("smime-type") or "").lower()
           for t in msg.walk()):
        log.info("Freigabe: verschlüsselte Mail — gesendete Elemente bleiben unberührt")
        return

    html = _mp.extract_html(msg)
    if not html:
        return
    asyncio.create_task(_handler._cleanup_sent_item(
        sender, mid, html,
        subject=msg.get("Subject", "") or "",
        to_recipients=list(empfaenger or []),
        replace_all=False,
    ))


@app.post("/api/maintenance/mails/{mail_id}/release")
async def api_held_mail_release(mail_id: str, _: str = Depends(_require_admin)):
    import reinject as _reinject
    result = _held_mails_mod.get_raw(mail_id)
    if result is None:
        raise HTTPException(404, "Mail not found")
    from_addr, to_addrs, raw_bytes = result
    try:
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: _reinject.send(from_addr, to_addrs, raw_bytes)
        )
    except Exception as exc:
        raise HTTPException(500, f"Zustellung fehlgeschlagen: {exc}")
    _held_mails_mod.delete(mail_id)
    _sent_items_nach_freigabe_aufraeumen(from_addr, to_addrs, raw_bytes)
    return JSONResponse({"ok": True})


@app.post("/api/maintenance/mode")
async def api_set_maintenance_mode(request: Request, _: str = Depends(_require_admin)):
    body = await request.json()
    enabled = bool(body.get("enabled", False))
    settings_store.update({"MAINTENANCE_MODE": enabled})
    return JSONResponse({"ok": True, "maintenance_mode": enabled})


@app.get("/api/smtp-acl/status")
async def api_smtp_acl_status(_: str = Depends(_require_admin)):
    """State of the SMTP source-IP allowlist for the Erweitert-Tab panel."""
    import smtp_acl
    from datetime import datetime, timezone
    ts = smtp_acl.last_refresh_ts()
    rejects = [
        {"ts": datetime.fromtimestamp(t, timezone.utc).isoformat(), "ip": ip}
        for t, ip in smtp_acl.recent_rejects()[:25]
    ]
    return JSONResponse({
        "enabled": settings_store.get("SMTP_SOURCE_ACL_ENABLED") is not False,
        "range_count": smtp_acl.range_count(),
        "last_refresh": (datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None),
        "extra_cidrs": settings_store.get("SMTP_ACL_EXTRA_CIDRS") or [],
        "recent_rejects": rejects,
    })


@app.post("/api/smtp-acl/refresh")
async def api_smtp_acl_refresh(_: str = Depends(_require_admin)):
    """Fetch the current Exchange Online IP ranges on demand."""
    import asyncio
    import smtp_acl
    n = await asyncio.to_thread(smtp_acl.refresh)
    return JSONResponse({"ok": True, "range_count": n})


@app.get("/outlook-addin")
async def outlook_addin_page_redirect(user: str = Depends(_require_admin)):
    # Outlook Add-in ist jetzt Teil von Einrichtung (eigener wizard-step)
    return RedirectResponse("/setup#step-addin", status_code=308)


@app.post("/api/test-mail")
async def api_test_mail(request: Request, user: str = Depends(_check_auth)):
    data = await request.json()
    from_email = (data.get("from_email") or "").strip()
    to_email = (data.get("to_email") or "").strip()
    mail_type = (data.get("mail_type") or "plain").strip()
    if not from_email or not to_email:
        raise HTTPException(400, "from_email und to_email sind erforderlich")

    if mail_type == "html":
        msg = MIMEText(
            "<html><body><p>Dies ist eine HTML Test-Mail vom EXO Signature Gateway.</p>"
            "<p>Die Signatur wird durch den Service eingefügt.</p></body></html>",
            "html", "utf-8",
        )
    else:
        msg = MIMEText(
            "Dies ist eine Nur-Text Test-Mail vom EXO Signature Gateway.\n"
            "Die Signatur wird durch den Service eingefügt.",
            "plain", "utf-8",
        )
    msg["Subject"] = f"Test-Mail ({mail_type}) – Signaturprüfung"
    msg["From"] = from_email
    msg["To"] = to_email

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        with smtplib.SMTP("127.0.0.1", config.SMTP_PORT, timeout=10) as smtp:
            smtp.ehlo()
            try:
                smtp.starttls(context=ctx)
                smtp.ehlo()
            except smtplib.SMTPException:
                pass
            smtp.sendmail(from_email, [to_email], msg.as_bytes())
        log.info("Test mail sent from=%s to=%s by %s", from_email, to_email, user)
        return JSONResponse({"ok": True})
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/letsencrypt")
async def api_letsencrypt(request: Request, user: str = Depends(_require_admin)):
    data = await request.json()
    domain = (data.get("domain") or "").strip()
    email = (data.get("email") or "").strip()
    if not domain or not email:
        raise HTTPException(400, "domain und email sind erforderlich")

    data_dir = Path("/app/data")
    webroot = data_dir / "acme-webroot"
    le_cfg = data_dir / "le-config"
    le_work = data_dir / "le-work"
    le_logs = data_dir / "le-logs"
    for d in [webroot, le_cfg, le_work, le_logs]:
        d.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        ["certbot", "certonly", "--webroot",
         "-w", str(webroot), "-d", domain,
         "--email", email, "--agree-tos", "--non-interactive",
         "--config-dir", str(le_cfg),
         "--work-dir", str(le_work),
         "--logs-dir", str(le_logs)],
        capture_output=True, text=True, timeout=120,
    )

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "certbot error").strip()
        log.error("certbot failed: %s", detail)
        raise HTTPException(500, detail)

    cert_dir = le_cfg / "live" / domain
    try:
        shutil.copy2(cert_dir / "fullchain.pem", config.SMTP_TLS_CERT)
        shutil.copy2(cert_dir / "privkey.pem", config.SMTP_TLS_KEY)
    except OSError as exc:
        raise HTTPException(500, f"Zertifikat kopieren fehlgeschlagen: {exc}")
    log.info("Let's Encrypt cert renewed for %s by %s", domain, user)
    return JSONResponse({"ok": True, "detail": "Zertifikat erneuert. Neustart erforderlich."})


@app.post("/api/notification/test")
async def api_notification_test(user: str = Depends(_require_admin)):
    import notification as _notif
    import config as _config
    to = _notif._get_notify_to()
    if not to:
        raise HTTPException(400, "Kein Benachrichtigungs-Empfänger konfiguriert")
    ok = _notif._graph_send(to, "EXO Gateway – Test-Benachrichtigung",
                            _notif._html_wrap("Test-Benachrichtigung", "#27ae60",
                                              "<p>Die Benachrichtigungsfunktion ist korrekt konfiguriert.</p>"))
    if not ok:
        raise HTTPException(500, "Senden fehlgeschlagen – Einstellungen prüfen")
    return JSONResponse({"ok": True})




@app.post("/api/restart")
async def api_restart(user: str = Depends(_require_admin)):
    log.info("Service restart requested by %s", user)

    def _do_restart():
        import time
        time.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=_do_restart, daemon=True).start()
    return JSONResponse({"ok": True})


@app.get("/config-view", response_class=HTMLResponse)
async def config_view(request: Request, user: str = Depends(_require_admin)):
    tenant = config.TENANT_ID or settings_store.get("TENANT_ID") or ""
    client = config.CLIENT_ID or settings_store.get("CLIENT_ID") or ""
    smarthost = config.EXO_SMARTHOST or settings_store.get("EXO_SMARTHOST") or ""
    cfg = {
        "TENANT_ID": (tenant[:8] + "…") if tenant else "(nicht konfiguriert)",
        "CLIENT_ID": (client[:8] + "…") if client else "(nicht konfiguriert)",
        "EXO_SMARTHOST": smarthost or "(nicht konfiguriert)",
        "SMTP_PORT": config.SMTP_PORT,
        "SMTP_TLS_CERT": config.SMTP_TLS_CERT,
        "WEBUI_PORT": config.WEBUI_PORT,
        "TEMPLATE_DIR": config.TEMPLATE_DIR,
        "VERSION": config.VERSION,
    }
    return templates.TemplateResponse(
        request=request, name="config.html",
        context={"cfg": cfg, "active": "config", "gateway_name": _gateway_name()},
    )


def _advanced_debug_context() -> dict:
    """Gemeinsamer Kontext für die Erweitert- (/advanced) und die link-lose
    Debug-Seite (/debug) — beide teilen sich denselben Template-Baukasten."""
    import hub_client
    return {"s": settings_store.public_view(),
            "gateway_name": _gateway_name(),
            "hub_configured": hub_client.is_configured(),
            "hub_registered": hub_client.is_registered(),
            "hub_cert_registered": hub_client.cert_is_registered(),
            "current_version": config.VERSION}


@app.get("/advanced", response_class=HTMLResponse)
async def advanced_page(request: Request, user: str = Depends(_require_admin)):
    ctx = _advanced_debug_context()
    ctx["active"] = "advanced"
    return templates.TemplateResponse(request=request, name="advanced.html", context=ctx)


# /debug: link-lose Diagnoseseite — bewusst NICHT im Menü, nur direkt per URL
# erreichbar (Selbsttest, Postfach-Health-Rohdaten, Observatory, ACME-Reset/-Proxy).
@app.get("/debug", response_class=HTMLResponse)
async def debug_page(request: Request, user: str = Depends(_require_admin)):
    ctx = _advanced_debug_context()
    ctx["active"] = "debug"
    return templates.TemplateResponse(request=request, name="debug.html", context=ctx)


@app.get("/log", response_class=HTMLResponse)
async def log_page(request: Request, user: str = Depends(_require_admin)):
    return templates.TemplateResponse(
        request=request, name="log.html",
        context={"active": "log", "stream_token": _make_log_token(),
                 "gateway_name": _gateway_name()},
    )


@app.get("/log/stream")
async def log_stream(request: Request, token: str = ""):
    import json as _json
    if not _check_log_token(token):
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    q: _queue_mod.Queue = _queue_mod.Queue(maxsize=200)
    with _LOG_SUBSCRIBERS_LOCK:
        _LOG_SUBSCRIBERS.append(q)

    async def generate():
        for line in list(_LOG_BUFFER):
            yield f"data: {_json.dumps(line)}\n\n"
        try:
            while True:
                try:
                    line = q.get_nowait()
                    yield f"data: {_json.dumps(line)}\n\n"
                except _queue_mod.Empty:
                    await asyncio.sleep(0.4)
                    yield ": keepalive\n\n"
        finally:
            with _LOG_SUBSCRIBERS_LOCK:
                try:
                    _LOG_SUBSCRIBERS.remove(q)
                except ValueError:
                    pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── S/MIME Lifecycle: CA config + self-service ────────────────────────────────






















# ── Persistent log search ──────────────────────────────────────────────────────

@app.get("/api/logs/search")
async def api_logs_search(q: str = "", time_from: str = "", time_to: str = "",
                          user: str = Depends(_require_admin)):
    if not q and not (time_from or time_to):
        raise HTTPException(400, "Suchbegriff oder Zeitraum fehlt")
    import log_manager
    results = log_manager.search(q, max_lines=500,
                                 time_from=time_from, time_to=time_to)
    return JSONResponse({"results": results, "count": len(results)})


@app.get("/api/logs/files")
async def api_logs_files(user: str = Depends(_require_admin)):
    import log_manager
    return JSONResponse({"files": log_manager.list_files()})


# ── Config export / import ─────────────────────────────────────────────────────

# Aus der Deklaration abgeleitet statt von Hand gepflegt: die frühere Liste
# nannte SECTIGO_PASSWORD (längst obsolet), aber weder SMIME_KEY_PASSWORD noch
# SSO_SESSION_SECRET, SMTP_SUBMIT_PASSWORD, APP_POOL oder LICENSE_KEY — der
# Konfigurations-Export enthielt sie also im Klartext.
_EXPORT_EXCLUDE = set(settings_store.SECRET_KEYS) | {"_SCHEMA_VERSION"}


@app.get("/api/config/export")
async def api_config_export(user: str = Depends(_require_admin)):
    import base64 as _b64
    import smime_store as _ss
    root = _ET.Element("exo-signature-config")
    root.set("version", config.VERSION)
    root.set("exported", datetime.now(timezone.utc).isoformat())

    s = settings_store.get_all()
    for key in sorted(s):
        if key in _EXPORT_EXCLUDE:
            continue
        value = s[key]
        elem = _ET.SubElement(root, "setting")
        elem.set("key", key)
        if isinstance(value, (dict, list)):
            elem.set("value", _json_mod.dumps(value, ensure_ascii=False))
            elem.set("type", "json")
        elif isinstance(value, bool):
            elem.set("value", "true" if value else "false")
            elem.set("type", "bool")
        elif isinstance(value, int):
            elem.set("value", str(value))
            elem.set("type", "int")
        else:
            elem.set("value", str(value or ""))

    # ── S/MIME signing certs (cert + private key) ─────────────────────────────
    smime_dir = _ss.SMIME_DIR
    if smime_dir.exists():
        for user_dir in sorted(smime_dir.iterdir()):
            cert_p = user_dir / "cert.pem"
            key_p  = user_dir / "key.pem"
            if not cert_p.exists():
                continue
            elem = _ET.SubElement(root, "smime-signing-cert")
            elem.set("email", user_dir.name)
            _ET.SubElement(elem, "cert").text = _b64.b64encode(cert_p.read_bytes()).decode()
            if key_p.exists():
                _ET.SubElement(elem, "key").text = _b64.b64encode(key_p.read_bytes()).decode()

    # ── S/MIME recipient certs (public only) ──────────────────────────────────
    rcpt_dir = _ss.RECIPIENT_DIR
    if rcpt_dir.exists():
        for user_dir in sorted(rcpt_dir.iterdir()):
            cert_p = user_dir / "cert.pem"
            if not cert_p.exists():
                continue
            elem = _ET.SubElement(root, "smime-recipient-cert")
            elem.set("email", user_dir.name)
            _ET.SubElement(elem, "cert").text = _b64.b64encode(cert_p.read_bytes()).decode()

    # ── Signature templates (HTML + TXT) ──────────────────────────────────────
    from pathlib import Path as _Path
    tpl_dir = _Path(config.TEMPLATE_DIR)
    if tpl_dir.exists():
        for tpl_file in sorted(tpl_dir.iterdir()):
            if tpl_file.suffix not in (".html", ".txt") or tpl_file.name.endswith(".bak"):
                continue
            elem = _ET.SubElement(root, "template")
            elem.set("name", tpl_file.name)
            elem.text = _b64.b64encode(tpl_file.read_bytes()).decode()

    # ── ACME account keys + URLs ───────────────────────────────────────────────
    import acme_state as _acme
    if _acme.ACME_DIR.exists():
        for acme_file in sorted(_acme.ACME_DIR.iterdir()):
            if acme_file.suffix not in (".pem", ".txt") or acme_file.name == "orders.json":
                continue
            elem = _ET.SubElement(root, "acme-file")
            elem.set("name", acme_file.name)
            elem.text = _b64.b64encode(acme_file.read_bytes()).decode()

    xml_bytes = b'<?xml version="1.0" encoding="utf-8"?>\n' + _ET.tostring(root, encoding="unicode").encode("utf-8")
    filename = f"exo-sig-config-{datetime.now().strftime('%Y%m%d')}.xml"
    return StreamingResponse(
        io.BytesIO(xml_bytes),
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/config/import")
async def api_config_import(
    request: Request,
    user: str = Depends(_require_admin),
    xml_file: UploadFile = File(...),
):
    content = await xml_file.read()
    try:
        root = _ET.fromstring(content.decode("utf-8"))
    except _ET.ParseError as exc:
        raise HTTPException(400, f"Ungültiges XML: {exc}")

    if root.tag != "exo-signature-config":
        raise HTTPException(400, "Kein gültiges EXO-Konfigurations-XML")

    patch: dict = {}
    for elem in root.findall("setting"):
        key = elem.get("key", "")
        value_str = elem.get("value", "")
        type_hint = elem.get("type", "str")
        if not key or key in _EXPORT_EXCLUDE or key not in settings_store.DEFAULTS:
            continue
        try:
            if type_hint == "json":
                value = _json_mod.loads(value_str)
            elif type_hint == "bool":
                value = value_str.lower() in ("true", "1", "yes")
            elif type_hint == "int":
                value = int(value_str)
            else:
                value = value_str
            patch[key] = value
        except Exception:
            pass

    settings_store.update(patch)

    # Apply timezone change immediately to all active log handlers
    if "LOG_TIMEZONE" in patch:
        import log_manager
        tz_name = patch["LOG_TIMEZONE"]
        fmt = log_manager._TZFormatter(tz_name, log_manager._LOG_FMT, datefmt=log_manager._DATE_FMT)
        for h in logging.getLogger().handlers:
            h.setFormatter(fmt)
        log.info("Log timezone updated to %s", tz_name)

    # ── Restore S/MIME certs ──────────────────────────────────────────────────
    import base64 as _b64
    import smime_store as _ss
    certs_restored = 0

    for elem in root.findall("smime-signing-cert"):
        email_addr = elem.get("email", "").lower().strip()
        cert_b64 = (elem.findtext("cert") or "").strip()
        key_b64  = (elem.findtext("key")  or "").strip()
        if not email_addr or not cert_b64:
            continue
        try:
            user_dir = _ss.SMIME_DIR / email_addr
            user_dir.mkdir(parents=True, exist_ok=True)
            (user_dir / "cert.pem").write_bytes(_b64.b64decode(cert_b64))
            if key_b64:
                # Privatschluessel aus dem Konfigurationsimport — ueber secure_io,
                # sonst 644 (Audit 2026-07-26).
                _secure_io.write_secret_bytes(user_dir / "key.pem",
                                              _b64.b64decode(key_b64))
            certs_restored += 1
        except Exception as exc:
            log.warning("Config import: could not restore signing cert for %s: %s", email_addr, exc)

    for elem in root.findall("smime-recipient-cert"):
        email_addr = elem.get("email", "").lower().strip()
        cert_b64 = (elem.findtext("cert") or "").strip()
        if not email_addr or not cert_b64:
            continue
        try:
            _ss.store_recipient_cert(email_addr, _b64.b64decode(cert_b64))
            certs_restored += 1
        except Exception as exc:
            log.warning("Config import: could not restore recipient cert for %s: %s", email_addr, exc)

    # ── Restore signature templates ───────────────────────────────────────────
    from pathlib import Path as _Path
    tpl_dir = _Path(config.TEMPLATE_DIR)
    tpl_dir.mkdir(parents=True, exist_ok=True)
    templates_restored = 0
    for elem in root.findall("template"):
        fname = elem.get("name", "").strip()
        content_b64 = (elem.text or "").strip()
        if not fname or not content_b64:
            continue
        if not (fname.endswith(".html") or fname.endswith(".txt")):
            continue
        try:
            (tpl_dir / fname).write_bytes(_b64.b64decode(content_b64))
            templates_restored += 1
        except Exception as exc:
            log.warning("Config import: could not restore template %s: %s", fname, exc)

    # ── Restore ACME account keys + URLs ─────────────────────────────────────
    import acme_state as _acme
    _acme.ACME_DIR.mkdir(parents=True, exist_ok=True)
    acme_restored = 0
    for elem in root.findall("acme-file"):
        fname = elem.get("name", "").strip()
        content_b64 = (elem.text or "").strip()
        if not fname or not content_b64:
            continue
        if not (fname.endswith(".pem") or fname.endswith(".txt")):
            continue
        try:
            dest = _acme.ACME_DIR / fname
            dest.write_bytes(_b64.b64decode(content_b64))
            dest.chmod(0o600)
            acme_restored += 1
        except Exception as exc:
            log.warning("Config import: could not restore ACME file %s: %s", fname, exc)

    log.info("Config imported by %s: %d settings, %d certs, %d templates, %d acme-files from %s",
             user, len(patch), certs_restored, templates_restored, acme_restored, root.get("exported", "?"))
    return JSONResponse({"ok": True, "imported": len(patch), "certs_restored": certs_restored,
                         "templates_restored": templates_restored, "acme_restored": acme_restored})


# ── MIME Observatory ──────────────────────────────────────────────────────────

@app.get("/api/test/acme-capture")
async def api_acme_capture_get(user: str = Depends(_require_admin)):
    """Return captured MIME payloads from the observatory."""
    import mime_observatory as _obs
    return JSONResponse({"captures": _obs.get_captures()})


@app.delete("/api/test/acme-capture")
async def api_acme_capture_clear(user: str = Depends(_require_admin)):
    import mime_observatory as _obs
    _obs.clear()
    return JSONResponse({"ok": True})


@app.post("/api/test/send-graph-acme")
async def api_send_graph_acme(request: Request, user: str = Depends(_require_admin)):
    """Send a fake ACME-style reply via Graph API so we can observe what Exchange adds.

    The subject uses the 'Re: ACME: TEST-' prefix which triggers the MIME
    Observatory capture when the mail arrives at our gateway outbound connector.
    """
    data = await request.json()
    from_email = (data.get("from_email") or "").strip().lower()
    to_email   = (data.get("to_email") or "acme@castle.cloud").strip().lower()
    label      = (data.get("label") or "graph-default").strip()

    if not from_email:
        raise HTTPException(400, "from_email ist erforderlich")

    import uuid, base64 as _b64, email.message, email.policy, email.utils, time as _time
    import graph_reinject as _gr

    test_id = uuid.uuid4().hex[:8]
    subject = f"Re: ACME: TEST-{test_id}"
    digest  = _b64.urlsafe_b64encode(b"TEST-FAKE-DIGEST-" + test_id.encode()).rstrip(b"=").decode()

    body_text = (
        "-----BEGIN ACME RESPONSE-----\r\n"
        f"{digest}\r\n"
        "-----END ACME RESPONSE-----\r\n"
    )

    mime = email.message.EmailMessage()
    mime["From"]           = from_email
    mime["To"]             = to_email
    mime["Subject"]        = subject
    mime["Date"]           = email.utils.formatdate(localtime=False)
    mime["Message-ID"]     = email.utils.make_msgid(domain=from_email.split("@", 1)[-1])
    mime["Auto-Submitted"] = "auto-generated"
    mime["X-ACME-Observatory"] = label
    mime.set_content(body_text, subtype="plain", charset="us-ascii")
    # SMTP policy ensures CRLF line endings — Exchange rejects bare-LF MIME on relay
    raw_mime = mime.as_bytes(policy=email.policy.SMTP)

    import asyncio as _asyncio
    ok = await _asyncio.get_event_loop().run_in_executor(
        None, _gr.send_via_graph_mime, from_email, [to_email], raw_mime
    )

    log.info("Graph ACME test sent from=%s to=%s label=%s id=%s ok=%s",
             from_email, to_email, label, test_id, ok)
    return JSONResponse({
        "ok": ok,
        "test_id": test_id,
        "subject": subject,
        "label": label,
        "note": "Mail sent via Graph API. Wait ~15s, then check /api/test/acme-capture for what Exchange delivered to the gateway.",
    })


# ── Mail-Processor Self-Tests ─────────────────────────────────────────────────

@app.get("/api/test/mail-processor/options")
async def api_test_mail_processor_options(user: str = Depends(_require_admin)):
    """Return available templates and configured mailbox emails for the self-test UI."""
    import os
    templates = []
    try:
        for f in sorted(os.listdir(config.TEMPLATE_DIR)):
            if f.endswith(".html") and not f.endswith(".bak"):
                templates.append(f[:-5])
    except Exception:
        pass
    import mailbox_match
    mailbox_cfg = settings_store.get("MAILBOX_CONFIG") or {}
    emails = sorted(mailbox_match.configured_addresses(mailbox_cfg))
    return JSONResponse({"templates": templates, "emails": emails})


@app.post("/api/test/mail-processor")
async def api_test_mail_processor(request: Request, user: str = Depends(_require_admin)):
    """Run in-process self-tests for mail_processor.inject().

    Accepts optional JSON body {"template": "...", "email": "..."}.
    When both are given the real rendered signature is used instead of the
    built-in test signature.
    """
    import self_test as _st
    import asyncio
    import signature_engine
    from graph_client import get_user

    sig_html = sig_txt = None
    try:
        body = await request.json()
        template = (body.get("template") or "").strip() or None
        email = (body.get("email") or "").strip() or None
        if template or email:
            ud = await get_user(email) if email else __import__("graph_client").UserData(mail="test@example.com")
            sig_html, sig_txt = signature_engine.render(ud, template)
    except Exception:
        pass  # malformed body or graph error → fall back to test sig

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: _st.run_all(sig_html, sig_txt))
    return JSONResponse(result)









# ── ACME Reply Method ─────────────────────────────────────────────────────────

@app.get("/api/acme/reply-method")
async def api_acme_reply_method_get(user: str = Depends(_require_admin)):
    method = (settings_store.get("ACME_REPLY_METHOD") or "auto").strip().lower()
    return JSONResponse({"ok": True, "method": method})


@app.post("/api/acme/reply-method")
async def api_acme_reply_method_set(request: Request, user: str = Depends(_require_admin)):
    data = await request.json()
    method = (data.get("method") or "auto").strip().lower()
    if method not in ("auto", "graph", "direct_smtp"):
        return JSONResponse({"ok": False, "error": "method must be 'auto', 'graph' or 'direct_smtp'"}, status_code=400)
    settings_store.update({"ACME_REPLY_METHOD": method})
    log.info("ACME reply method set to '%s' by %s", method, user)
    return JSONResponse({"ok": True, "method": method})


@app.get("/api/acme/http-proxy")
async def api_acme_http_proxy_get(user: str = Depends(_require_admin)):
    proxy = settings_store.get("ACME_HTTP_PROXY") or ""
    return JSONResponse({"ok": True, "proxy": proxy})


@app.post("/api/acme/http-proxy")
async def api_acme_http_proxy_set(request: Request, user: str = Depends(_require_admin)):
    data = await request.json()
    proxy = (data.get("proxy") or "").strip()
    if proxy and not (proxy.startswith("http://") or proxy.startswith("https://") or proxy.startswith("socks5://")):
        return JSONResponse({"ok": False, "error": "proxy muss mit http://, https:// oder socks5:// beginnen"}, status_code=400)
    settings_store.update({"ACME_HTTP_PROXY": proxy})
    log.info("ACME HTTP proxy %s by %s", "cleared" if not proxy else "set", user)
    return JSONResponse({"ok": True, "proxy": proxy})


@app.post("/api/acme/http-proxy/test")
async def api_acme_http_proxy_test(request: Request, user: str = Depends(_require_admin)):
    """Test connectivity to the configured CA directory through the ACME HTTP proxy."""
    import httpx as _httpx
    import acme_state as _acme_state
    proxy = settings_store.get("ACME_HTTP_PROXY") or None
    directory_url = _acme_state.CASTLE_DIRECTORY
    try:
        async with _httpx.AsyncClient(timeout=15, proxy=proxy) as c:
            r = await c.get(directory_url)
        if r.status_code == 200:
            return JSONResponse({"ok": True, "message": f"Verbindung erfolgreich (HTTP {r.status_code}) über {'Proxy' if proxy else 'Direktverbindung'}."})
        return JSONResponse({"ok": False, "message": f"Unerwarteter Status {r.status_code}: {r.text[:200]}"})
    except Exception as exc:
        return JSONResponse({"ok": False, "message": f"Verbindung fehlgeschlagen: {exc}"})


# ── Sectigo Certificate Manager config ────────────────────────────────────────

# ── ACME Account Reset ────────────────────────────────────────────────────────

@app.get("/api/acme/account-users")
async def api_acme_account_users(user: str = Depends(_require_admin)):
    """Return users with castle_acme backend + per-user account key status."""
    import acme_state
    ca_cfg = settings_store.get("CA_USER_CONFIG") or {}
    users = []
    for email, cfg in ca_cfg.items():
        if (cfg.get("backend") or "") != "castle_acme":
            continue
        # Trigger one-time migration of legacy global key to per-user file
        if not acme_state.account_key_exists(email):
            acme_state._migrate_legacy_key(email)
        users.append({
            "email": email,
            "key_exists": acme_state.account_key_exists(email),
            "staging": bool(cfg.get("staging")),
        })
    return JSONResponse({"ok": True, "users": users})


@app.post("/api/acme/account-reset")
async def api_acme_account_reset(request: Request, user: str = Depends(_require_admin)):
    """Delete per-user ACME account key + account URL files."""
    import acme_state
    data = await request.json()
    email = (data.get("email") or "").strip()
    if not email:
        return JSONResponse({"ok": False, "error": "email required"}, status_code=400)
    ca_cfg = settings_store.get("CA_USER_CONFIG") or {}
    if email not in ca_cfg or (ca_cfg[email].get("backend") or "") != "castle_acme":
        return JSONResponse({"ok": False, "error": "user not found in CASTLE ACME config"}, status_code=404)
    deleted = acme_state.reset_account(email)
    log.info("ACME account reset for %s by %s — deleted: %s", email, user, deleted or "nothing")
    return JSONResponse({"ok": True, "deleted": deleted})


# ── EXO PowerShell Certificate Export ─────────────────────────────────────────

@app.get("/api/cert/exo-ps-info")
async def api_cert_exo_ps_info(user: str = Depends(_require_admin)):
    """Return subject, thumbprint (SHA-1, as shown in Azure Portal) and expiry of the EXO PS auth.pfx."""
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.hazmat.primitives import hashes
    pfx_path = "/app/data/auth.pfx"
    try:
        with open(pfx_path, "rb") as f:
            pfx_data = f.read()
        _, cert, _ = pkcs12.load_key_and_certificates(pfx_data, password=None)
        thumbprint_sha1 = cert.fingerprint(hashes.SHA1()).hex().upper()  # noqa: S303 — display only
        return JSONResponse({
            "ok": True,
            "subject": cert.subject.rfc4514_string(),
            "thumbprint": thumbprint_sha1,
            "not_before": cert.not_valid_before_utc.isoformat(),
            "not_after": cert.not_valid_after_utc.isoformat(),
        })
    except FileNotFoundError:
        return JSONResponse({"ok": False, "error": "auth.pfx nicht gefunden"}, status_code=404)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/cert/exo-ps-export.cer")
async def api_cert_exo_ps_export(user: str = Depends(_require_admin)):
    """Export the public-key certificate from auth.pfx as DER-encoded .cer (no private key)."""
    from cryptography.hazmat.primitives.serialization import pkcs12, Encoding
    from starlette.responses import Response
    pfx_path = "/app/data/auth.pfx"
    try:
        with open(pfx_path, "rb") as f:
            pfx_data = f.read()
        _, cert, _ = pkcs12.load_key_and_certificates(pfx_data, password=None)
        der_bytes = cert.public_bytes(Encoding.DER)
        return Response(
            content=der_bytes,
            media_type="application/pkix-cert",
            headers={"Content-Disposition": 'attachment; filename="EXO-PS-Auth.cer"'},
        )
    except FileNotFoundError:
        return JSONResponse({"ok": False, "error": "auth.pfx nicht gefunden"}, status_code=404)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)









# ── Audit log API ─────────────────────────────────────────────────────────────

@app.get("/api/audit/events")
async def api_audit_events(
    request: Request,
    _user: str = Depends(_require_admin),
    date: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    action: str | None = None,
    sender: str | None = None,
    limit: int = 200,
    offset: int = 0,
):
    import mail_audit as _audit_mod
    import json as _json
    events = _audit_mod.query_events(
        date=date, date_from=date_from, date_to=date_to,
        action=action, sender=sender,
        limit=min(limit, 500), offset=offset,
    )
    total = _audit_mod.count_events(date=date, date_from=date_from, date_to=date_to,
                                    action=action, sender=sender)
    for e in events:
        try:
            e["recipients"] = _json.loads(e["recipients"] or "[]")
        except Exception:
            e["recipients"] = []
    return {"events": events, "total": total, "offset": offset, "limit": limit}


@app.get("/api/system/info")
async def api_system_info(user: str = Depends(_require_admin)):
    import time as _time_mod
    import mail_audit as _audit_mod
    import handler as _handler_mod

    # Disk usage of /app/data
    data_path = Path("/app/data")
    try:
        du = shutil.disk_usage(str(data_path))
        disk_total_mb  = round(du.total / 1024 / 1024, 1)
        disk_used_mb   = round(du.used  / 1024 / 1024, 1)
        disk_free_mb   = round(du.free  / 1024 / 1024, 1)
        disk_pct       = round(du.used / du.total * 100, 1) if du.total else 0
    except Exception:
        disk_total_mb = disk_used_mb = disk_free_mb = disk_pct = None

    # SQLite DB size
    db_path = _audit_mod.DB_PATH
    try:
        db_size_kb = round(db_path.stat().st_size / 1024, 1)
    except Exception:
        db_size_kb = None

    # Log files total size
    logs_path = data_path / "logs"
    try:
        logs_size_kb = round(sum(f.stat().st_size for f in logs_path.iterdir() if f.is_file()) / 1024, 1)
    except Exception:
        logs_size_kb = None

    # Process RSS memory from /proc/self/status
    rss_mb = None
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                rss_mb = round(int(line.split()[1]) / 1024, 1)
                break
    except Exception:
        pass

    # Process uptime
    uptime_s = None
    try:
        pid_stat = Path("/proc/self/stat").read_text().split()
        # field 22 (0-indexed 21) = starttime in clock ticks
        clk_tck = os.sysconf("SC_CLK_TCK")
        uptime_total = float(Path("/proc/uptime").read_text().split()[0])
        proc_start_ticks = int(pid_stat[21])
        uptime_s = int(uptime_total - proc_start_ticks / clk_tck)
    except Exception:
        pass

    # In-flight mail count
    in_flight = _handler_mod._in_flight

    # Avg processing time last 24h
    since_24h = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # rewind 24h
    from datetime import timedelta
    since_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    avg_ms = _audit_mod.avg_processing_ms(since_24h)

    # Peak hour today
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    peak = _audit_mod.peak_hour(today_str)

    return {
        "disk_total_mb":    disk_total_mb,
        "disk_used_mb":     disk_used_mb,
        "disk_free_mb":     disk_free_mb,
        "disk_pct":         disk_pct,
        "db_size_kb":       db_size_kb,
        "logs_size_kb":     logs_size_kb,
        "rss_mb":           rss_mb,
        "uptime_s":         uptime_s,
        "in_flight":        in_flight,
        "avg_ms_24h":       avg_ms,
        "peak_hour":        peak[0] if peak else None,
        "peak_hour_cnt":    peak[1] if peak else None,
        "maintenance_mode": bool(settings_store.get("MAINTENANCE_MODE")),
        "held_mail_count":  _held_mails_mod.count(),
    }


@app.get("/api/system/mail-hourly")
async def api_mail_hourly(user: str = Depends(_require_admin)):
    """Stündliche Mail-Statistik für heute aus mail_audit.db."""
    import mail_audit as _audit_mod
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _audit_mod.get_mail_hourly(today)


@app.get("/api/system/log-tail")
async def api_log_tail(n: int = 150, user: str = Depends(_require_admin)):
    """Letzte N Zeilen aus dem In-Memory-Log-Buffer."""
    lines = list(_LOG_BUFFER)[-n:]
    return {"lines": lines}


@app.post("/api/system/restart-container")
async def api_restart_container(user: str = Depends(_require_admin)):
    """Trigger-Datei schreiben → Host-Watcher führt docker compose restart aus."""
    import updater
    result = updater.request_container_restart(user)
    if not result["ok"]:
        return JSONResponse(result, status_code=409)
    log.info("Container restart requested by %s", user)
    return JSONResponse(result)


@app.get("/api/system/update/check")
async def api_update_check(channel: str = "main", user: str = Depends(_require_admin)):
    """GitHub-Prüfung: gibt es eine neuere Version im gewählten Kanal?"""
    import updater
    return JSONResponse(updater.check_update(channel, config.VERSION))


@app.get("/api/system/update/releases")
async def api_update_releases(user: str = Depends(_require_admin)):
    """Liste aller veröffentlichten Release-Tags (für Versionsauswahl / Rollback)."""
    import updater
    return JSONResponse({"releases": updater.list_release_tags()})


@app.post("/api/system/update")
async def api_system_update(request: Request, user: str = Depends(_require_admin)):
    """Trigger-Datei schreiben → Host-Watcher führt git pull + docker compose up --build aus."""
    import updater
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    channel = body.get("channel", "main")
    target_version = (body.get("target_version") or "").strip() or None
    result = updater.request_update(user, config.VERSION, channel=channel, target_version=target_version)
    if not result["ok"]:
        return JSONResponse(result, status_code=409)
    log.info("Update requested by %s (channel: %s, current version: %s, target: %s)",
              user, channel, config.VERSION, target_version or "latest")
    return JSONResponse(result)


@app.get("/api/system/update/status")
async def api_system_update_status(user: str = Depends(_require_admin)):
    """Aktuellen Update-Status aus data/.update-status lesen."""
    import updater
    return JSONResponse(updater.get_status())


@app.post("/api/system/update/clear")
async def api_system_update_clear(user: str = Depends(_require_admin)):
    """Status-Datei löschen (nach erfolgreichem Update oder Fehler)."""
    import updater
    updater.clear_status()
    return JSONResponse({"ok": True})


@app.get("/api/system/update/watcher-status")
async def api_watcher_status(user: str = Depends(_require_admin)):
    """Prüft ob der Host-Watcher-Service läuft (Heartbeat-Datei)."""
    import updater
    return JSONResponse({"ok": updater.watcher_ok()})


@app.get("/api/system/update/whats-new")
async def api_update_whats_new(from_version: str, to_version: str, user: str = Depends(_require_admin)):
    """Fetch changelog entries from GitHub between from_version (excl.) and to_version (incl.)."""
    import re, httpx, updater
    url = f"https://raw.githubusercontent.com/{updater.GITHUB_REPO}/main/CHANGELOG.md"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
            r.raise_for_status()
            text = r.text
    except Exception as exc:
        return JSONResponse({"entries": [], "error": str(exc)})

    def _vnum(v: str) -> int:
        # 1.5.135 → 1005135 — flache, vergleichbare Zahl (Distanz = Release-Schritte)
        parts = (list(int(x) for x in v.lstrip("v").split(".")) + [0, 0, 0])[:3]
        return parts[0] * 1_000_000 + parts[1] * 1_000 + parts[2]

    # Alle Einträge in Datei-Reihenfolge (neueste zuerst) sammeln
    all_entries: list[dict] = []
    cur_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## v"):
            if cur_lines:
                all_entries.append({"header": cur_lines[0],
                                    "body": "\n".join(cur_lines[1:]).strip()})
            cur_lines = [line]
        elif cur_lines:
            cur_lines.append(line)
    if cur_lines:
        all_entries.append({"header": cur_lines[0],
                            "body": "\n".join(cur_lines[1:]).strip()})

    # DRIFT-IMMUN: Changelog-Nummern driften von der VERSION-Datei (Hand-
    # Nummerierung + Pre-Commit-Bump). Statt Nummern zu matchen, zeigen wir die
    # obersten K Einträge, K = Versions-Distanz (Anzahl Releases seit dem
    # installierten Stand). So ist die Anzeige unabhängig von der Nummerierung.
    steps = max(1, _vnum(to_version) - _vnum(from_version))
    entries = all_entries[:min(steps, len(all_entries), 25)]

    return JSONResponse({"entries": entries})


@app.get("/api/system/changelog")
async def api_changelog(n: int = 10, user: str = Depends(_require_admin)):
    """Letzte N Einträge aus CHANGELOG.md."""
    try:
        text = (Path("/app/CHANGELOG.md")).read_text(encoding="utf-8")
    except FileNotFoundError:
        return JSONResponse({"entries": [], "error": "CHANGELOG.md nicht gefunden"})
    entries = []
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("## ") and current:
            entries.append("\n".join(current).strip())
            current = [line]
            if len(entries) >= n:
                break
        elif line.startswith("## "):
            current = [line]
        elif current:
            current.append(line)
    if current and len(entries) < n:
        entries.append("\n".join(current).strip())
    return JSONResponse({"entries": entries})






@app.get("/api/support/download")
async def api_support_download(user: str = Depends(_require_admin)):
    """Support-Bundle als ZIP herunterladen (lokal speichern)."""
    import support_upload as _sup
    import asyncio as _aio
    from fastapi.responses import Response as _Resp
    zip_bytes, blob_name = await _aio.get_event_loop().run_in_executor(
        None, _sup.build_bundle, list(_LOG_BUFFER)
    )
    return _Resp(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{blob_name}"'},
    )


@app.post("/api/support/upload")
async def api_support_upload(request: Request, user: str = Depends(_require_admin)):
    """Support-Bundle (Logs, Settings, Audit) an den Provider-Hub hochladen."""
    import hub_client
    import legal_consent
    # Gate C — Art. 28 Abs. 3 DSGVO: Die Verarbeitung muss durch einen Vertrag
    # geregelt SEIN, bevor sie beginnt. Das Bundle enthält Mail-Metadaten
    # (Absender/Empfänger/Betreff) Dritter — ohne AVV darf es nicht übertragen
    # werden.
    if not legal_consent.context_consented("support_upload"):
        raise HTTPException(
            403, "Für die Übermittlung von Diagnosepaketen muss zuerst der "
                 "Auftragsverarbeitungsvertrag abgeschlossen werden "
                 "(Einstellungen → Anbindung & Lizenzen → Rechtliche Dokumente).")
    try:
        body = await request.json()
    except Exception:
        body = {}
    note = str((body or {}).get("note") or "").strip()[:2000]
    result = await hub_client.upload_bundle(list(_LOG_BUFFER), note=note)
    return JSONResponse(result)


# ── Welcome-Banner (Erstinstallations-Hinweis) ────────────────────────────────

@app.post("/api/welcome/dismiss")
async def api_welcome_dismiss(user: str = Depends(_require_admin)):
    settings_store.update({"WELCOME_DISMISSED": True})
    return JSONResponse({"ok": True})


# ── Rechtliche Dokumente / Consent ────────────────────────────────────────────

@app.get("/api/legal/status")
async def api_legal_status(user: str = Depends(_require_admin)):
    import legal_consent
    return JSONResponse({"ok": True, "documents": legal_consent.consent_status_all()})


@app.get("/api/legal/pending")
async def api_legal_pending(user: str = Depends(_require_admin)):
    """Dokumente, deren geänderte Fassung noch zuzustimmen ist (Ziffer 13.3).

    Speist das Hinweisband in base.html. Bewusst leer auf einem frisch
    aufgesetzten Gateway — dort führen die Gates durch die Erstzustimmung.
    """
    import legal_consent
    return JSONResponse({"ok": True, "pending": legal_consent.pending_reconsent()})


@app.get("/api/legal/doc/{doc_id}")
async def api_legal_doc(doc_id: str, lang: str = "de", user: str = Depends(_require_admin)):
    import legal_consent
    if doc_id not in legal_consent.CURRENT_DOCUMENTS:
        raise HTTPException(404, "Dokument nicht gefunden.")
    text = legal_consent.get_document_text(doc_id, lang)
    if not text:
        raise HTTPException(404, "Dokumenttext nicht verfügbar.")
    doc = legal_consent.CURRENT_DOCUMENTS[doc_id]
    return JSONResponse({
        "ok": True,
        "doc_id": doc_id,
        "version": doc["version"],
        "label": doc.get(f"label_{lang}", doc.get("label_de", doc_id)),
        "text": text,
        "content_hash": legal_consent.compute_document_hash(doc_id),
    })


@app.post("/api/legal/consent")
async def api_legal_consent(request: Request, user: str = Depends(_require_admin)):
    import legal_consent
    data = await request.json()
    doc_id = (data.get("document_id") or "").strip()
    version = (data.get("version") or "").strip()
    content_hash = (data.get("content_hash") or "").strip()
    context = (data.get("context") or "").strip()
    if not doc_id or not version or not content_hash:
        raise HTTPException(400, "document_id, version und content_hash sind erforderlich.")
    if doc_id not in legal_consent.CURRENT_DOCUMENTS:
        raise HTTPException(404, "Unbekanntes Dokument.")
    doc = legal_consent.CURRENT_DOCUMENTS[doc_id]
    if doc.get("no_consent_required"):
        raise HTTPException(400, "Dieses Dokument erfordert keine Zustimmung.")
    if doc["version"] != version:
        raise HTTPException(409, f"Version stimmt nicht überein (aktuell: {doc['version']}).")
    expected_hash = legal_consent.compute_document_hash(doc_id)
    if expected_hash and content_hash != expected_hash:
        raise HTTPException(409, "Inhaltsprüfsumme stimmt nicht überein.")
    ok = legal_consent.record_consent(doc_id, version, content_hash, context)
    # Beleg nachreichen, damit der Hub nicht dauerhaft die alte Fassung
    # ausweist. Best effort: die Zustimmung ist bereits erteilt und bleibt
    # gültig, auch wenn der Hub gerade nicht erreichbar ist.
    if ok:
        try:
            import hub_client
            if hub_client.is_registered():
                res = await hub_client.submit_consent_receipts()
                if not res.get("ok"):
                    log.info("Zustimmungsbelege nicht übermittelt: %s", res.get("error"))
        except Exception as exc:
            log.info("Zustimmungsbelege nicht übermittelt: %s", exc)
    return JSONResponse({"ok": ok})



















# ── DigiCert-Direktanbindung (eigenes CertCentral-Konto des Kunden) ───────────

@app.get("/api/digicert/config")
async def api_digicert_config_get(user: str = Depends(_require_admin)):
    import digicert_client
    return JSONResponse({
        "ok": True,
        "configured": digicert_client.is_configured(),
        "api_base": settings_store.get("DIGICERT_API_BASE") or "",
        "org_id": str(settings_store.get("DIGICERT_ORG_ID") or ""),
        "validity_days": settings_store.get("DIGICERT_VALIDITY_DAYS") or 365,
        "payment_method": settings_store.get("DIGICERT_PAYMENT_METHOD") or "profile",
    })


@app.post("/api/digicert/config")
async def api_digicert_config_save(request: Request, user: str = Depends(_require_admin)):
    data = await request.json()
    to_save = {
        "DIGICERT_API_BASE": (data.get("api_base") or "").strip()
                             or "https://www.digicert.com/services/v2",
        "DIGICERT_ORG_ID": (data.get("org_id") or "").strip(),
        "DIGICERT_PAYMENT_METHOD": data.get("payment_method")
                                   if data.get("payment_method") in ("profile", "balance")
                                   else "profile",
    }
    try:
        to_save["DIGICERT_VALIDITY_DAYS"] = max(1, min(825, int(data.get("validity_days") or 365)))
    except (TypeError, ValueError):
        to_save["DIGICERT_VALIDITY_DAYS"] = 365
    key = (data.get("api_key") or "").strip()
    if key:  # leer = unverändert lassen
        to_save["DIGICERT_API_KEY"] = key
    settings_store.update(to_save)
    log.info("DigiCert-Direktanbindung konfiguriert von %s (key %s)",
             user, "gesetzt" if key else "unverändert")
    return JSONResponse({"ok": True})


@app.post("/api/digicert/test")
async def api_digicert_test(user: str = Depends(_require_admin)):
    import digicert_client
    return JSONResponse(await digicert_client.test())


@app.post("/api/digicert/domain/setup")
async def api_digicert_domain_setup(request: Request, user: str = Depends(_require_admin)):
    import digicert_client
    data = await request.json()
    return JSONResponse(await digicert_client.domain_setup((data.get("domain") or "").strip()))


@app.post("/api/digicert/domain/check")
async def api_digicert_domain_check(request: Request, user: str = Depends(_require_admin)):
    import digicert_client
    data = await request.json()
    domain_id = data.get("domain_id")
    if not domain_id:
        raise HTTPException(400, "domain_id erforderlich.")
    return JSONResponse(await digicert_client.domain_check(domain_id))












































