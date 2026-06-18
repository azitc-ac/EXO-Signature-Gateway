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
import xml.etree.ElementTree as _ET
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

from fastapi import FastAPI, Request, Form, Depends, HTTPException, status, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import config
import graph_client
import pkce as pkce_mod
import settings_store
import signature_engine

log = logging.getLogger(__name__)

from contextlib import asynccontextmanager

@asynccontextmanager
async def _lifespan(application):
    import acme_state
    acme_state.resume_pending_polls()
    yield

app = FastAPI(title="EXO Signature Gateway", lifespan=_lifespan)
security = HTTPBasic(auto_error=False)

_STATIC_DIR = Path(__file__).parent / "static"
_TEMPLATE_DIR = Path(__file__).parent / "templates"

app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
templates.env.globals["version"] = config.VERSION

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


# ── Password helpers ───────────────────────────────────────────────────────────

def _hash_password(password: str) -> str:
    """Return pbkdf2:sha256:<salt>:<hash> string."""
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
    return f"pbkdf2:sha256:{salt}:{key.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    """Verify a password against a pbkdf2:sha256:<salt>:<hash> string."""
    try:
        _, alg, salt, key_hex = stored.split(":", 3)
        assert alg == "sha256"
    except Exception:
        return False
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
    return hmac.compare_digest(key.hex(), key_hex)


def _check_password(password: str) -> bool:
    """Check password against stored hash or env-var fallback."""
    stored_hash = settings_store.get("ADMIN_PASSWORD_HASH") or ""
    if stored_hash:
        return _verify_password(password, stored_hash)
    # Fall back to env var (supports existing deployments)
    return secrets.compare_digest(password.encode(), config.WEBUI_PASSWORD.encode())


# ── Auth ───────────────────────────────────────────────────────────────────────

def _check_auth(credentials: HTTPBasicCredentials = Depends(security)):
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    username = settings_store.get("WEBUI_USERNAME") or "admin"
    correct_user = secrets.compare_digest(credentials.username.encode(), username.encode())
    correct_pass = _check_password(credentials.password)
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def _password_change_required() -> bool:
    """True if the user is still on the default 'admin' password."""
    stored_hash = settings_store.get("ADMIN_PASSWORD_HASH") or ""
    if stored_hash:
        return False  # Has been changed
    # Using env-var fallback — flag change required if it's the default
    return config.WEBUI_PASSWORD == "admin"


# ── Helpers ────────────────────────────────────────────────────────────────────
def _cert_expiry() -> str:
    cert_path = Path(config.SMTP_TLS_CERT)
    if not cert_path.exists():
        return "kein Zertifikat"
    try:
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes(), default_backend())
        expires = cert.not_valid_after_utc
        days = (expires - datetime.now(timezone.utc)).days
        color = "red" if days < 14 else "orange" if days < 30 else "green"
        return f'<span style="color:{color}">{expires.strftime("%d.%m.%Y")} (noch {days} Tage)</span>'
    except Exception as exc:
        return f"Fehler: {exc}"


def _webui_scheme() -> str:
    """https if TLS cert is present, http otherwise."""
    return "https" if Path(config.SMTP_TLS_CERT).exists() else "http"


def _build_redirect_uri() -> str:
    """
    Always localhost — Azure native/desktop apps allow HTTP localhost in every tenant.
    After login the browser lands on localhost (fails to connect to Pi), the user
    copies the URL from the address bar and pastes it into the wizard.
    """
    return f"http://localhost:{config.WEBUI_PORT}/auth/callback"


# ── Routes: public (no auth) ───────────────────────────────────────────────────

@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "service": "exo-signature-service"})


@app.get("/setup", response_class=HTMLResponse)
async def setup_wizard(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(security),
):
    """Setup wizard — accessible even with default credentials."""
    # Allow access with default credentials or any valid credentials
    # (so the wizard works before Azure is configured)
    authed = False
    if credentials:
        username = settings_store.get("WEBUI_USERNAME") or "admin"
        if (
            secrets.compare_digest(credentials.username.encode(), username.encode())
            and _check_password(credentials.password)
        ):
            authed = True

    s = settings_store.get_all()
    # Effective values (env overrides settings)
    effective = {
        "tenant_id": config.TENANT_ID or s.get("TENANT_ID", ""),
        "client_id": config.CLIENT_ID or s.get("CLIENT_ID", ""),
        "exo_smarthost": config.EXO_SMARTHOST or s.get("EXO_SMARTHOST", ""),
        "tenant_domain": s.get("TENANT_DOMAIN", ""),
        "public_hostname": s.get("PUBLIC_HOSTNAME", ""),
        "setup_complete": s.get("SETUP_COMPLETE", False),
        "azure_app_created": s.get("AZURE_APP_CREATED", False),
        "exo_connector_created": s.get("EXO_CONNECTOR_CREATED", False),
        "smime_rules_created": s.get("SMIME_RULES_CREATED", False),
        "imap_access_configured": s.get("IMAP_ACCESS_CONFIGURED", False),
        "password_change_needed": _password_change_required(),
        "cert_exists": Path(config.SMTP_TLS_CERT).exists(),
        "auth_cert_exists": Path("/app/data/auth.pfx").exists(),
        "authed": authed,
        "bootstrap_client_id": s.get("BOOTSTRAP_CLIENT_ID", ""),
        "redirect_uri": _build_redirect_uri(),
        "webui_port": config.WEBUI_PORT,
    }
    return templates.TemplateResponse(
        request=request, name="setup.html",
        context={"s": s, "e": effective, "active": "setup"},
    )


# ── Routes: PKCE auth flow ─────────────────────────────────────────────────────

@app.get("/auth/start")
async def auth_start(request: Request, user: str = Depends(_check_auth)):
    """Return Azure AD auth URL as JSON (for fetch callers)."""
    redirect_uri = _build_redirect_uri()
    _state, auth_url = pkce_mod.create_session(redirect_uri)
    return JSONResponse({"auth_url": auth_url})


@app.get("/auth/start-redirect")
async def auth_start_redirect(request: Request, user: str = Depends(_check_auth)):
    """Redirect browser directly to Azure AD for PKCE login."""
    redirect_uri = _build_redirect_uri()
    _state, auth_url = pkce_mod.create_session(redirect_uri)
    return RedirectResponse(auth_url)


@app.post("/api/setup/auth-paste")
async def api_auth_paste(request: Request, user: str = Depends(_check_auth)):
    """
    Accept the URL the browser was redirected to after Azure login
    (user copies it from the address bar after the expected connection-refused page).
    Extracts code+state, runs token exchange and post-auth setup.
    """
    data = await request.json()
    pasted = (data.get("url") or "").strip()

    try:
        parsed = urllib.parse.urlparse(pasted)
        params = urllib.parse.parse_qs(parsed.query)
        code  = params.get("code",  [""])[0]
        state = params.get("state", [""])[0]
        error = params.get("error", [""])[0]
    except Exception:
        raise HTTPException(400, "Ungültige URL")

    if error:
        err_desc = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query).get("error_description", [""])[0]
        raise HTTPException(400, f"Azure-Fehler: {error} — {err_desc}")
    if not code or not state:
        raise HTTPException(
            400,
            "URL enthält keinen Code oder State. "
            "Bitte die vollständige URL aus der Adressleiste kopieren "
            "(beginnt mit http://localhost:8080/auth/callback?code=…).",
        )

    session = pkce_mod.pop_session(state)
    if not session:
        raise HTTPException(400, "PKCE-Session abgelaufen — bitte erneut auf 'Anmelden' klicken.")

    try:
        token_resp = await pkce_mod.exchange_code(code, session["verifier"], session["redirect_uri"])
        access_token = token_resp["access_token"]
    except Exception as exc:
        raise HTTPException(500, f"Token-Austausch fehlgeschlagen: {exc}")

    try:
        import setup_wizard
        result = await setup_wizard.run_post_auth_setup(access_token)
        log.info("Post-auth setup complete: %s", result)
        return JSONResponse({"ok": True})
    except Exception as exc:
        log.error("Post-auth setup failed: %s", exc)
        raise HTTPException(500, f"Setup-Fehler nach Login: {exc}")


@app.get("/auth/callback", response_class=HTMLResponse)
async def auth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
    user: str = Depends(_check_auth),
):
    """Azure AD redirects here with the authorization code."""
    if error:
        return templates.TemplateResponse(
            request=request, name="setup.html",
            context={
                "s": settings_store.get_all(),
                "e": {},
                "active": "setup",
                "auth_error": f"{error}: {error_description}",
            },
        )

    session = pkce_mod.pop_session(state)
    if not session:
        raise HTTPException(400, "Ungültige oder abgelaufene PKCE-Session — bitte erneut anmelden")

    try:
        token_resp = await pkce_mod.exchange_code(code, session["verifier"], session["redirect_uri"])
        access_token = token_resp["access_token"]
    except Exception as exc:
        log.error("PKCE token exchange failed: %s", exc)
        return templates.TemplateResponse(
            request=request, name="setup.html",
            context={
                "s": settings_store.get_all(),
                "e": {},
                "active": "setup",
                "auth_error": str(exc),
            },
        )

    # Run tenant discovery + app creation
    try:
        import setup_wizard
        result = await setup_wizard.run_post_auth_setup(access_token)
        log.info("Post-auth setup complete: %s", result)
    except Exception as exc:
        log.error("Post-auth setup failed: %s", exc)
        return templates.TemplateResponse(
            request=request, name="setup.html",
            context={
                "s": settings_store.get_all(),
                "e": {},
                "active": "setup",
                "auth_error": f"Setup-Fehler nach Login: {exc}",
            },
        )

    return RedirectResponse("/setup?entra_done=1", status_code=303)


# ── Routes: setup API endpoints ────────────────────────────────────────────────

@app.post("/api/setup/bootstrap-client")
async def api_setup_bootstrap_client(request: Request, user: str = Depends(_check_auth)):
    data = await request.json()
    client_id = (data.get("client_id") or "").strip()
    if not client_id:
        raise HTTPException(400, "client_id darf nicht leer sein")
    settings_store.update({"BOOTSTRAP_CLIENT_ID": client_id})
    log.info("Bootstrap client ID set by %s", user)
    return JSONResponse({"ok": True, "redirect_uri": _build_redirect_uri()})


@app.post("/api/setup/hostname")
async def api_setup_hostname(request: Request, user: str = Depends(_check_auth)):
    data = await request.json()
    hostname = (data.get("hostname") or "").strip()
    if not hostname:
        raise HTTPException(400, "hostname darf nicht leer sein")
    settings_store.update({"PUBLIC_HOSTNAME": hostname})
    log.info("Public hostname set to %s by %s", hostname, user)
    return JSONResponse({"ok": True})


@app.post("/api/setup/change-password")
async def api_change_password(request: Request, user: str = Depends(_check_auth)):
    data = await request.json()
    new_pw = (data.get("password") or "").strip()
    if len(new_pw) < 8:
        raise HTTPException(400, "Passwort muss mindestens 8 Zeichen haben")
    hashed = _hash_password(new_pw)
    settings_store.update({"ADMIN_PASSWORD_HASH": hashed})
    log.info("Admin password changed by %s", user)
    return JSONResponse({"ok": True})


@app.post("/api/setup/exo-connector")
async def api_setup_exo_connector(request: Request, user: str = Depends(_check_auth)):
    """Trigger PowerShell EXO connector setup."""
    import setup_wizard

    app_id = config.CLIENT_ID or settings_store.get("CLIENT_ID") or ""
    tenant_domain = settings_store.get("TENANT_DOMAIN") or ""
    hostname = settings_store.get("PUBLIC_HOSTNAME") or ""

    missing = []
    if not app_id:
        missing.append("CLIENT_ID")
    if not tenant_domain:
        missing.append("TENANT_DOMAIN")
    if not hostname:
        missing.append("PUBLIC_HOSTNAME")
    if missing:
        raise HTTPException(400, f"Fehlende Konfiguration: {', '.join(missing)}")

    result = setup_wizard.run_exo_connector_setup(
        app_id=app_id,
        tenant_domain=tenant_domain,
        smtp_proxy_hostname=hostname,
    )
    if result["ok"]:
        return JSONResponse({"ok": True, "output": result["output"]})
    raise HTTPException(500, result["output"])


@app.post("/api/setup/gen-auth-cert")
async def api_gen_auth_cert(request: Request, user: str = Depends(_check_auth)):
    """Generate a self-signed auth cert, save PFX locally, return public cert PEM."""
    import base64 as _b64
    from setup_wizard import _generate_auth_cert, _AUTH_CERT_PATH

    try:
        cert_der, pfx_bytes = _generate_auth_cert()
    except Exception as exc:
        raise HTTPException(500, f"Zertifikat-Generierung fehlgeschlagen: {exc}")

    _AUTH_CERT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _AUTH_CERT_PATH.write_bytes(pfx_bytes)
    log.info("Auth certificate generated and saved to %s by %s", _AUTH_CERT_PATH, user)

    # Convert DER → PEM for display/download
    pem_proc = subprocess.run(
        ["openssl", "x509", "-inform", "DER", "-outform", "PEM"],
        input=cert_der, capture_output=True, check=True,
    )
    cert_pem = pem_proc.stdout.decode()
    return JSONResponse({"ok": True, "cert_pem": cert_pem})


@app.post("/api/setup/smime-rules")
async def api_setup_smime_rules(request: Request, user: str = Depends(_check_auth)):
    """Create S/MIME inbound transport rules in Exchange Online."""
    import setup_wizard

    app_id = config.CLIENT_ID or settings_store.get("CLIENT_ID") or ""
    tenant_domain = settings_store.get("TENANT_DOMAIN") or ""

    missing = []
    if not app_id:
        missing.append("CLIENT_ID")
    if not tenant_domain:
        missing.append("TENANT_DOMAIN")
    if missing:
        raise HTTPException(400, f"Fehlende Konfiguration: {', '.join(missing)}")

    result = setup_wizard.run_smime_rules_setup(
        app_id=app_id,
        tenant_domain=tenant_domain,
    )
    if result["ok"]:
        return JSONResponse({"ok": True, "output": result["output"]})
    raise HTTPException(500, result["output"])


@app.post("/api/setup/imap-access")
async def api_setup_imap_access(request: Request, user: str = Depends(_check_auth)):
    """Register EXO Service Principal and grant IMAP FullAccess to all mailboxes."""
    import setup_wizard

    app_id = config.CLIENT_ID or settings_store.get("CLIENT_ID") or ""
    tenant_domain = settings_store.get("TENANT_DOMAIN") or ""

    missing = []
    if not app_id:
        missing.append("CLIENT_ID")
    if not tenant_domain:
        missing.append("TENANT_DOMAIN")
    if missing:
        raise HTTPException(400, f"Fehlende Konfiguration: {', '.join(missing)}")

    result = setup_wizard.run_imap_access_setup(
        app_id=app_id,
        tenant_domain=tenant_domain,
    )
    if result["ok"]:
        return JSONResponse({"ok": True, "output": result["output"]})
    raise HTTPException(500, result["output"])


@app.get("/api/setup/verify/connector")
async def api_verify_connector(_=Depends(_check_auth)):
    import setup_wizard
    return setup_wizard.verify_connector()


@app.get("/api/setup/verify/imap")
async def api_verify_imap(_=Depends(_check_auth)):
    import setup_wizard
    return setup_wizard.verify_imap()


@app.get("/api/setup/verify/smime")
async def api_verify_smime(_=Depends(_check_auth)):
    import setup_wizard
    return setup_wizard.verify_smime_rules()


@app.post("/api/setup/mark-complete")
async def api_setup_complete(request: Request, user: str = Depends(_check_auth)):
    settings_store.update({"SETUP_COMPLETE": True})
    log.info("Setup marked complete by %s", user)
    return JSONResponse({"ok": True})


@app.post("/api/setup/test-graph")
async def api_test_graph(request: Request, user: str = Depends(_check_auth)):
    """Quick connectivity test — fetch own organization info."""
    token = graph_client._acquire_token()
    if not token:
        raise HTTPException(503, "Keine Graph-Zugangsdaten konfiguriert")
    try:
        async with __import__("httpx").AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://graph.microsoft.com/v1.0/organization?$select=displayName",
                headers={"Authorization": f"Bearer {token}"},
            )
        resp.raise_for_status()
        orgs = resp.json().get("value", [])
        name = orgs[0].get("displayName", "?") if orgs else "?"
        return JSONResponse({"ok": True, "org": name})
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ── Routes: mailbox config ─────────────────────────────────────────────────────

@app.get("/api/mailboxes")
async def api_get_mailboxes(_=Depends(_check_auth)):
    """List all EXO mailboxes + their current MAILBOX_CONFIG."""
    import graph_client
    users = await graph_client.list_mailboxes()
    config_map: dict = settings_store.get("MAILBOX_CONFIG") or {}
    result = []
    for u in users:
        email = u["email"]
        cfg = config_map.get(email, {})
        result.append({
            "email": email,
            "name": u["name"],
            "type": u.get("type", "user"),
            "sig": cfg.get("sig", False),
            "smime": cfg.get("smime", False),
        })
    # Also include mailboxes in config that Graph didn't return (e.g. removed users)
    for email, cfg in config_map.items():
        if not any(r["email"] == email for r in result):
            result.append({
                "email": email,
                "name": email,
                "type": "user",
                "sig": cfg.get("sig", False),
                "smime": cfg.get("smime", False),
            })
    return {"mailboxes": result}


@app.post("/api/mailboxes/save")
async def api_save_mailboxes(body: dict, _=Depends(_check_auth)):
    """Save MAILBOX_CONFIG and update EXO Distribution Group + transport rule."""
    mailboxes = body.get("mailboxes", [])
    config_map = {}
    enabled_members = []
    for m in mailboxes:
        email = (m.get("email") or "").lower().strip()
        if not email:
            continue
        sig = bool(m.get("sig", False))
        smime = bool(m.get("smime", False))
        if sig or smime:
            config_map[email] = {"sig": sig, "smime": smime}
            enabled_members.append(email)
        # If both false, don't include in config (passthrough by default)
    settings_store.update({"MAILBOX_CONFIG": config_map})

    # Update EXO Distribution Group if wizard is complete
    s = settings_store.get_all()
    app_id = s.get("CLIENT_ID") or config.CLIENT_ID
    tenant_domain = s.get("TENANT_DOMAIN") or ""
    if body.get("update_dg") and app_id and tenant_domain:
        import setup_wizard
        result = setup_wizard.run_mailbox_dg_update(app_id, tenant_domain, enabled_members)
        return {"ok": result["ok"], "saved": True, "dg_output": result.get("output", "")}
    return {"ok": True, "saved": True}


# ── Routes: authenticated pages ────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: str = Depends(_check_auth)):
    import smime_store as _smime_store
    import stats as _stats_mod2
    pw_change = _password_change_required()
    total = get_stats()
    daily = _stats_mod2.get_daily()
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
            "cert_expiry": _cert_expiry(),
            "signing_certs": signing_certs,
            "expiring_certs": expiring_certs,
            "active": "dashboard",
            "password_change_needed": pw_change,
        },
    )


@app.get("/template", response_class=HTMLResponse)
async def template_editor(request: Request, user: str = Depends(_check_auth)):
    html_path = Path(config.TEMPLATE_DIR) / "signature.html"
    txt_path = Path(config.TEMPLATE_DIR) / "signature.txt"
    return templates.TemplateResponse(
        request=request, name="template_editor.html",
        context={
            "html_content": html_path.read_text() if html_path.exists() else "",
            "txt_content": txt_path.read_text() if txt_path.exists() else "",
            "active": "template",
            "saved": request.query_params.get("saved"),
        },
    )


@app.post("/template", response_class=HTMLResponse)
async def template_save(
    request: Request,
    html_content: str = Form(""),
    txt_content: str = Form(""),
    user: str = Depends(_check_auth),
):
    Path(config.TEMPLATE_DIR, "signature.html").write_text(html_content)
    Path(config.TEMPLATE_DIR, "signature.txt").write_text(txt_content)
    signature_engine._reload_env()
    log.info("Templates saved by user %s", user)
    return RedirectResponse(url="/template?saved=1", status_code=303)


@app.get("/preview", response_class=HTMLResponse)
async def preview(request: Request, email: str = "", user: str = Depends(_check_auth)):
    user_data = graph_client.UserData()
    error = None
    if email:
        try:
            user_data = await graph_client.get_user(email)
        except Exception as exc:
            error = str(exc)
    sig_html, sig_txt = signature_engine.render(user_data)
    return templates.TemplateResponse(
        request=request, name="preview.html",
        context={
            "email": email, "sig_html": sig_html, "sig_txt": sig_txt,
            "error": error, "active": "preview",
        },
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user: str = Depends(_check_auth)):
    return templates.TemplateResponse(
        request=request, name="settings.html",
        context={
            "s": settings_store.get_all(),
            "active": "settings",
            "cert_expiry": _cert_expiry(),
            "smtp_port": config.SMTP_PORT,
            "saved": request.query_params.get("saved"),
        },
    )


@app.post("/settings")
async def settings_save(request: Request, user: str = Depends(_check_auth)):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Ungültige JSON-Daten")
    clean = {k: v for k, v in data.items() if k in settings_store.DEFAULTS}
    settings_store.update(clean)
    log.info("Settings updated by %s: %s", user, list(clean.keys()))
    return JSONResponse({"ok": True})


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
async def api_letsencrypt(request: Request, user: str = Depends(_check_auth)):
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
async def api_notification_test(user: str = Depends(_check_auth)):
    import notification as _notif
    import config as _config
    to = settings_store.get("NOTIFICATION_MAILBOX") or ""
    if not to:
        raise HTTPException(400, "NOTIFICATION_MAILBOX nicht konfiguriert")
    ok = _notif.send_startup_notification(_config.VERSION)
    if not ok:
        raise HTTPException(500, "Senden fehlgeschlagen – SMTP-Einstellungen prüfen")
    return JSONResponse({"ok": True})


@app.post("/api/restart")
async def api_restart(user: str = Depends(_check_auth)):
    log.info("Service restart requested by %s", user)

    def _do_restart():
        import time
        time.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=_do_restart, daemon=True).start()
    return JSONResponse({"ok": True})


@app.get("/config-view", response_class=HTMLResponse)
async def config_view(request: Request, user: str = Depends(_check_auth)):
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
        context={"cfg": cfg, "active": "config"},
    )


@app.post("/api/smime/upload")
async def api_smime_upload(
    request: Request,
    user: str = Depends(_check_auth),
    email: str = Form(...),
    p12_file: UploadFile = File(...),
    password: str = Form(""),
):
    import smime_store
    p12_bytes = await p12_file.read()
    try:
        info = smime_store.store_p12_slot(email.lower().strip(), p12_bytes, password)
        log.info("S/MIME cert uploaded for %s by %s (slot %s)", email, user, info.get("slot_id"))
        return JSONResponse({"ok": True, "info": info})
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        log.error("S/MIME upload error: %s", exc)
        raise HTTPException(500, str(exc))


@app.post("/api/smime/delete/{cert_email}")
async def api_smime_delete(cert_email: str, user: str = Depends(_check_auth)):
    import smime_store
    smime_store.delete_cert(cert_email)
    log.info("S/MIME certs deleted for %s by %s", cert_email, user)
    return JSONResponse({"ok": True})


@app.post("/api/smime/delete-slot/{cert_email}/{slot_id}")
async def api_smime_delete_slot(cert_email: str, slot_id: str, user: str = Depends(_check_auth)):
    import smime_store
    try:
        smime_store.delete_cert_slot(cert_email, slot_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    log.info("S/MIME cert slot %s deleted for %s by %s", slot_id, cert_email, user)
    return JSONResponse({"ok": True})


@app.post("/api/smime/set-default/{cert_email}/{slot_id}")
async def api_smime_set_default(cert_email: str, slot_id: str, user: str = Depends(_check_auth)):
    import smime_store
    try:
        smime_store.set_default_slot(cert_email, slot_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    log.info("S/MIME default cert set to slot %s for %s by %s", slot_id, cert_email, user)
    return JSONResponse({"ok": True})


@app.get("/log", response_class=HTMLResponse)
async def log_page(request: Request, user: str = Depends(_check_auth)):
    return templates.TemplateResponse(
        request=request, name="log.html",
        context={"active": "log", "stream_token": _make_log_token()},
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


@app.get("/smime", response_class=HTMLResponse)
async def smime_page_v2(request: Request, user: str = Depends(_check_auth)):
    import smime_store
    import ca_backends as _ca
    import acme_state as _acme_state
    config_map: dict = settings_store.get("MAILBOX_CONFIG") or {}
    smime_from_config = {email for email, cfg in config_map.items() if cfg.get("smime")}
    smime_from_certs = {c["email"] for c in smime_store.list_certs()}
    all_emails = sorted(smime_from_config | smime_from_certs)
    smime_users = [{"email": email, "certs": smime_store.list_user_certs(email)} for email in all_emails]
    acme_orders = {em: _acme_state.get_order(em) for em in all_emails if _acme_state.get_order(em)}
    return templates.TemplateResponse(
        request=request, name="smime.html",
        context={
            "smime_users": smime_users,
            "ca_user_config": settings_store.get("CA_USER_CONFIG") or {},
            "backends": _ca.list_backends(),
            "recipient_certs": smime_store.list_recipient_certs(),
            "active": "smime",
            "cert_expiry": _cert_expiry(),
            "acme_orders": acme_orders,
        },
    )


@app.post("/api/smime/recipient/upload")
async def api_smime_recipient_upload(
    request: Request,
    user: str = Depends(_check_auth),
    email: str = Form(...),
    cert_file: UploadFile = File(...),
):
    import smime_store
    cert_bytes = await cert_file.read()
    # Accept PEM directly; also try DER → PEM conversion via openssl
    if not cert_bytes.strip().startswith(b"-----"):
        result = subprocess.run(
            ["openssl", "x509", "-inform", "DER", "-outform", "PEM"],
            input=cert_bytes, capture_output=True, timeout=10,
        )
        if result.returncode != 0:
            raise HTTPException(400, "Ungültige Zertifikatsdatei (weder PEM noch DER)")
        cert_bytes = result.stdout
    try:
        info = smime_store.store_recipient_cert(email.lower().strip(), cert_bytes)
        log.info("Recipient S/MIME cert uploaded for %s by %s", email, user)
        return JSONResponse({"ok": True, "info": info})
    except Exception as exc:
        log.error("Recipient cert upload error: %s", exc)
        raise HTTPException(400, str(exc))


@app.post("/api/smime/recipient/delete/{cert_email}")
async def api_smime_recipient_delete(cert_email: str, user: str = Depends(_check_auth)):
    import smime_store
    smime_store.delete_recipient_cert(cert_email)
    log.info("Recipient S/MIME cert deleted for %s by %s", cert_email, user)
    return JSONResponse({"ok": True})


@app.get("/api/smime/cert/details")
async def api_smime_cert_details(
    email: str, kind: str = "recipient", slot: str = "",
    user: str = Depends(_check_auth),
):
    """Return human-readable cert details for the detail modal (no download)."""
    import smime_store
    from cryptography import x509 as _x509
    from cryptography.hazmat.primitives.asymmetric import rsa, ec
    from cryptography.x509.oid import NameOID, ExtensionOID
    import hashlib

    if kind == "signing":
        if slot:
            cert_path = smime_store.get_signing_cert_path_for_slot(email, slot)
        else:
            cert_path = smime_store.get_signing_cert_path(email)
    else:
        cert_path = smime_store.get_recipient_cert_path(email) or (smime_store.RECIPIENT_DIR / "nope")

    if not cert_path or not cert_path.exists():
        raise HTTPException(404, "Zertifikat nicht gefunden")

    cert = _x509.load_pem_x509_certificate(cert_path.read_bytes())

    def _dn(name) -> str:
        parts = []
        for oid in (NameOID.COMMON_NAME, NameOID.EMAIL_ADDRESS,
                    NameOID.ORGANIZATION_NAME, NameOID.COUNTRY_NAME):
            attrs = name.get_attributes_for_oid(oid)
            if attrs:
                parts.append(attrs[0].value)
        return ", ".join(parts) if parts else name.rfc4514_string()

    pub = cert.public_key()
    if isinstance(pub, rsa.RSAPublicKey):
        key_info = f"RSA {pub.key_size} bit"
    elif isinstance(pub, ec.EllipticCurvePublicKey):
        key_info = f"EC {pub.curve.name}"
    else:
        key_info = type(pub).__name__

    der = cert.public_bytes(__import__("cryptography").hazmat.primitives.serialization.Encoding.DER)
    sha1 = ":".join(f"{b:02X}" for b in hashlib.sha1(der).digest())

    san_emails: list[str] = []
    try:
        san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
        san_emails = san.value.get_values_for_type(_x509.RFC822Name)
    except Exception:
        pass

    from datetime import timezone
    try:
        not_after = cert.not_valid_after_utc
        not_before = cert.not_valid_before_utc
    except AttributeError:
        not_after = cert.not_valid_after.replace(tzinfo=timezone.utc)
        not_before = cert.not_valid_before.replace(tzinfo=timezone.utc)

    return JSONResponse({
        "subject":  _dn(cert.subject),
        "issuer":   _dn(cert.issuer),
        "san":      san_emails,
        "serial":   format(cert.serial_number, "X"),
        "not_before": not_before.strftime("%d.%m.%Y"),
        "not_after":  not_after.strftime("%d.%m.%Y"),
        "key":      key_info,
        "sha1":     sha1,
    })


# ── S/MIME Lifecycle: CA config + self-service ────────────────────────────────

@app.get("/api/smime/ca-config")
async def api_ca_config_get(user: str = Depends(_check_auth)):
    import ca_backends as _ca
    return JSONResponse({
        "config": settings_store.get("CA_USER_CONFIG") or {},
        "backends": _ca.list_backends(),
    })


@app.post("/api/smime/ca-config/{email}")
async def api_ca_config_save(email: str, request: Request, user: str = Depends(_check_auth)):
    data = await request.json()
    cfg: dict = settings_store.get("CA_USER_CONFIG") or {}
    cfg[email.lower().strip()] = {
        "backend": data.get("backend", "assisted_manual"),
        "portal_url": (data.get("portal_url") or "").strip(),
        "notify_user": bool(data.get("notify_user", False)),
        "staging": bool(data.get("staging", False)),
    }
    settings_store.update({"CA_USER_CONFIG": cfg})
    return JSONResponse({"ok": True})


@app.post("/api/smime/renewal/token/{email}")
async def api_renewal_token_generate(email: str, user: str = Depends(_check_auth)):
    import selfservice, scheduler
    token = selfservice.generate_token(email)
    gw_url = scheduler._get_gateway_url()
    return JSONResponse({
        "ok": True,
        "token": token,
        "url": f"{gw_url}/smime/renew/{token}",
        "expires_days": selfservice.TOKEN_TTL_DAYS,
    })


@app.get("/api/smime/renewal/token-info/{email}")
async def api_renewal_token_info(email: str, user: str = Depends(_check_auth)):
    import selfservice, scheduler
    info = selfservice.get_token_info(email)
    if not info:
        return JSONResponse({"exists": False})
    gw_url = scheduler._get_gateway_url()
    return JSONResponse({
        "exists": True,
        "expires": info["expires"],
        "url": f"{gw_url}/smime/renew/{info['token']}",
    })


@app.post("/api/smime/renewal/notify/{email}")
async def api_renewal_notify(email: str, user: str = Depends(_check_auth)):
    import smime_store, selfservice, notification, scheduler
    certs = smime_store.list_user_certs(email)
    if not certs:
        raise HTTPException(400, "Kein Zertifikat für diesen Benutzer vorhanden")
    c = next((x for x in certs if x.get("is_default")), certs[0])
    ca_cfg: dict = (settings_store.get("CA_USER_CONFIG") or {}).get(email, {})
    token = selfservice.generate_token(email)
    gw_url = scheduler._get_gateway_url()
    upload_url = f"{gw_url}/smime/renew/{token}"
    ok = notification.send_renewal_notification_to_user(
        user_email=email,
        cert_info=c,
        upload_url=upload_url,
        backend_name=ca_cfg.get("backend", "assisted_manual"),
        user_config=ca_cfg,
    )
    if not ok:
        raise HTTPException(500, "Benachrichtigung konnte nicht gesendet werden")
    return JSONResponse({"ok": True, "upload_url": upload_url})


@app.get("/smime/renew/{token}", response_class=HTMLResponse)
async def smime_selfservice_page(token: str, request: Request):
    import selfservice, smime_store
    email = selfservice.validate_token(token)
    if not email:
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:40px'>"
            "<h2 style='color:#e74c3c'>Link abgelaufen oder ungültig</h2>"
            "<p>Bitte fordern Sie beim Administrator einen neuen Link an.</p>"
            "</body></html>",
            status_code=403,
        )
    certs = smime_store.list_user_certs(email)
    current_cert = next((c for c in certs if c.get("is_default")), certs[0] if certs else None)
    return templates.TemplateResponse(
        request=request, name="smime_selfservice.html",
        context={"email": email, "token": token, "current_cert": current_cert},
    )


@app.post("/api/smime/renew/{token}")
async def api_smime_selfservice_upload(
    token: str,
    request: Request,
    p12_file: UploadFile = File(...),
    password: str = Form(""),
):
    import selfservice, smime_store, notification
    email = selfservice.validate_token(token)
    if not email:
        raise HTTPException(403, "Link abgelaufen oder ungültig")
    try:
        p12_bytes = await p12_file.read()
        info = smime_store.store_p12_slot(email, p12_bytes, password)
        log.info("Self-service cert upload for %s (slot %s)", email, info.get("slot_id"))
        if settings_store.get("NOTIFY_CERT_RENEWAL") is not False:
            notification.send_cert_renewal_success(email, info)
        return JSONResponse({"ok": True, "info": info})
    except ValueError as exc:
        if settings_store.get("NOTIFY_CERT_RENEWAL") is not False:
            notification.send_cert_renewal_failure(email, str(exc))
        raise HTTPException(400, str(exc))
    except Exception as exc:
        log.error("Self-service upload error for %s: %s", email, exc)
        if settings_store.get("NOTIFY_CERT_RENEWAL") is not False:
            notification.send_cert_renewal_failure(email, str(exc))
        raise HTTPException(500, str(exc))


@app.get("/api/smime/renewal/status/{email}")
async def api_acme_status(email: str, user: str = Depends(_check_auth)):
    import acme_state
    order = acme_state.get_order(email.lower().strip())
    if not order:
        return JSONResponse({"active": False})
    return JSONResponse({
        "active": True,
        "status": order.get("status"),
        "error": order.get("error"),
        "created": order.get("created"),
    })


@app.post("/api/smime/renewal/clear/{email}")
async def api_acme_clear_order(email: str, user: str = Depends(_check_auth)):
    import acme_state
    acme_state.clear_order(email)
    log.info("ACME order cleared for %s by %s", email, user)
    return JSONResponse({"ok": True})


@app.post("/api/smime/renewal/initiate/{email}")
async def api_acme_initiate(email: str, user: str = Depends(_check_auth)):
    import ca_backends as _ca
    import acme_state as _acme_state
    email = email.lower().strip()
    ca_cfg: dict = (settings_store.get("CA_USER_CONFIG") or {}).get(email, {})
    backend_name = ca_cfg.get("backend", "assisted_manual")
    backend = _ca.get_backend(backend_name)
    if not backend.can_auto_renew():
        raise HTTPException(400, f"Backend '{backend_name}' unterstützt kein Auto-Enroll")

    # If there's already a waiting_challenge order, restart the mailbox poller
    # instead of creating a redundant CASTLE order.
    existing = _acme_state.get_order(email)
    if existing and existing.get("status") == "waiting_challenge":
        import asyncio
        asyncio.create_task(_acme_state._poll_mailbox_for_challenge(email))
        log.info("ACME mailbox poll restarted for %s by %s (order already placed)", email, user)
        return JSONResponse({"ok": True, "resumed": True})

    try:
        await backend.initiate_renewal(email, ca_cfg)
        log.info("ACME renewal initiated for %s by %s", email, user)
        return JSONResponse({"ok": True})
    except Exception as exc:
        log.error("ACME initiate failed for %s: %s", email, exc)
        raise HTTPException(500, str(exc))


@app.get("/api/smime/recipient/download/{cert_email}")
async def api_smime_recipient_download(cert_email: str, user: str = Depends(_check_auth)):
    import smime_store
    from fastapi.responses import Response
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding
    p = smime_store.get_recipient_cert_path(cert_email)
    if not p or not p.exists():
        raise HTTPException(404, "Zertifikat nicht gefunden")
    cert = x509.load_pem_x509_certificate(p.read_bytes())
    der_bytes = cert.public_bytes(Encoding.DER)
    safe_name = cert_email.replace("/", "_").replace("..", "_")
    return Response(
        content=der_bytes,
        media_type="application/pkix-cert",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.cer"'},
    )


# ── Persistent log search ──────────────────────────────────────────────────────

@app.get("/api/logs/search")
async def api_logs_search(q: str = "", user: str = Depends(_check_auth)):
    if not q:
        raise HTTPException(400, "Suchbegriff fehlt")
    import log_manager
    results = log_manager.search(q, max_lines=500)
    return JSONResponse({"results": results, "count": len(results)})


@app.get("/api/logs/files")
async def api_logs_files(user: str = Depends(_check_auth)):
    import log_manager
    return JSONResponse({"files": log_manager.list_files()})


# ── Config export / import ─────────────────────────────────────────────────────

_EXPORT_EXCLUDE = {"ADMIN_PASSWORD_HASH", "CLIENT_SECRET", "RELAY_PASSWORD"}


@app.get("/api/config/export")
async def api_config_export(user: str = Depends(_check_auth)):
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
    user: str = Depends(_check_auth),
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
                (user_dir / "key.pem").write_bytes(_b64.b64decode(key_b64))
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

    log.info("Config imported by %s: %d settings, %d certs from %s",
             user, len(patch), certs_restored, root.get("exported", "?"))
    return JSONResponse({"ok": True, "imported": len(patch), "certs_restored": certs_restored})
