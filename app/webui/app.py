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
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import config
import graph_client
import held_mails as _held_mails_mod
import mail_processor
import pkce as pkce_mod
import settings_store
import signature_engine
import sso as sso_mod

log = logging.getLogger(__name__)

from contextlib import asynccontextmanager

@asynccontextmanager
async def _lifespan(application):
    import acme_state
    acme_state.resume_pending_polls()
    yield

app = FastAPI(title="EXO Signature Gateway", lifespan=_lifespan)
security = HTTPBasic(auto_error=False)


class _NotAuthenticated(Exception):
    def __init__(self, is_api: bool = False):
        self.is_api = is_api


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

_STATIC_DIR = Path(__file__).parent / "static"
_TEMPLATE_DIR = Path(__file__).parent / "templates"

app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
templates.env.globals["version"] = config.VERSION


def _gateway_name() -> str:
    """Return the configured gateway name (dynamically read so changes take effect without restart)."""
    return settings_store.get("GATEWAY_NAME") or "EXO Signature Gateway"



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

def _get_session_user(request: Request) -> str | None:
    """Extract user identity from session cookie. Returns UPN/username or None."""
    cookie = request.cookies.get(sso_mod.SESSION_COOKIE)
    if not cookie:
        return None
    payload = sso_mod.verify_session_cookie(cookie)
    if not payload:
        return None
    return payload.get("u")


def _get_session_role(request: Request) -> str:
    """Return role from session cookie ('admin' or 'editor'). Defaults to 'admin' for Basic auth."""
    cookie = request.cookies.get(sso_mod.SESSION_COOKIE)
    if cookie:
        payload = sso_mod.verify_session_cookie(cookie)
        if payload:
            return payload.get("r", sso_mod.ROLE_ADMIN)
    return sso_mod.ROLE_ADMIN  # HTTP Basic is always admin


def _check_auth(request: Request, credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """Auth dependency: session cookie → local Basic auth → 401/redirect."""
    # 1. Session cookie (SSO or local login)
    user = _get_session_user(request)
    if user:
        return user
    # 2. HTTP Basic (local admin fallback — always available as emergency access)
    if credentials and credentials.username and credentials.password:
        username = settings_store.get("WEBUI_USERNAME") or "admin"
        if (secrets.compare_digest(credentials.username.encode(), username.encode())
                and _check_password(credentials.password)):
            return credentials.username
    # 3. Not authenticated
    path = request.url.path
    is_api = path.startswith("/api/") or path.startswith("/log/")
    raise _NotAuthenticated(is_api=is_api)


def _require_admin(request: Request, user: str = Depends(_check_auth)) -> str:
    """Dependency: requires admin role. Raises 403 for editors."""
    if _get_session_role(request) != sso_mod.ROLE_ADMIN:
        raise HTTPException(403, "Admin-Berechtigung erforderlich")
    return user


# Platzhalter-Passwörter, die als unsicher gelten und einen Wechsel erzwingen:
#   "admin"    = Code-Default (config.py)
#   "changeme" = Platzhalter aus azure-vm-setup.ps1 cloud-init (.env)
# Beide MÜSSEN hier stehen, sonst meldet der Setup-Wizard Schritt 1 (Passwort
# ändern) fälschlich als erledigt, wenn noch der Deploy-Platzhalter aktiv ist.
_DEFAULT_PASSWORDS = {"admin", "changeme", ""}


def _password_change_required() -> bool:
    """True if the user is still on a default/placeholder password."""
    stored_hash = settings_store.get("ADMIN_PASSWORD_HASH") or ""
    if stored_hash:
        return False  # Has been changed
    # Using env-var fallback — flag change required if it's a known placeholder
    return config.WEBUI_PASSWORD in _DEFAULT_PASSWORDS


def _setup_requires_auth() -> bool:
    """True once any authentication method is configured (setup page must no longer be anonymous)."""
    # Explicit password hash stored → local password was changed from default
    if settings_store.get("ADMIN_PASSWORD_HASH"):
        return True
    # SSO admin users + Bootstrap client configured → Entra login possible
    if settings_store.get("ADMIN_USERS") and settings_store.get("BOOTSTRAP_CLIENT_ID"):
        return True
    # Custom password via env var (not the default 'admin')
    if config.WEBUI_PASSWORD and config.WEBUI_PASSWORD != "admin":
        return True
    return False


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


def _build_redirect_uri(sso: bool = False) -> str:
    """
    For SSO flow: prefer ADDIN_BASE_URL (canonical external URL, no port) over PUBLIC_HOSTNAME+port.
    For setup flow: always localhost — Azure native/desktop apps allow HTTP localhost in every tenant.
    After login the browser lands on localhost (fails to connect to Pi), the user
    copies the URL from the address bar and pastes it into the wizard.
    """
    if sso:
        external = (settings_store.get("ADDIN_BASE_URL") or "").rstrip("/")
        if external:
            return f"{external}/auth/callback"
        hostname = settings_store.get("PUBLIC_HOSTNAME") or ""
        if hostname:
            # Öffentlich wird HTTPS auf 443 ausgeliefert (Docker mappt 443:WEBUI_PORT).
            # WEBUI_PORT ist NUR der interne Bind-Port und darf NICHT in die öffentliche
            # Redirect-URI gelangen — sonst sendet der Wizard https://host:8080/... und es
            # gibt AADSTS50011. Für nicht-Standard-Außenports ADDIN_BASE_URL setzen.
            return f"https://{hostname}/auth/callback"
    return f"http://localhost:{config.WEBUI_PORT}/auth/callback"


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


# ── Outlook Add-in (no auth — signatures are not sensitive, gateway is internal) ──

@app.get("/addin/compose", response_class=HTMLResponse)
async def addin_compose(request: Request):
    return templates.TemplateResponse(request=request, name="addin_compose.html", context={})


@app.get("/addin/manifest.xml")
async def addin_manifest(request: Request):
    """Generate the Office Add-in manifest dynamically.

    Base URL priority: 1) ADDIN_BASE_URL setting  2) X-Forwarded-Host  3) request.url
    """
    base = _addin_base_url(request)
    hostname = base.split("://")[-1].split(":")[0]
    # Stable add-in ID derived from the hostname so it never changes across restarts.
    addin_id = str(_uuid.uuid5(_uuid.NAMESPACE_DNS, f"exo-signature-addin.{hostname}"))
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<OfficeApp
  xmlns="http://schemas.microsoft.com/office/appforoffice/1.1"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xmlns:bt="http://schemas.microsoft.com/office/officeappbasictypes/1.0"
  xsi:type="MailApp">

  <Id>{addin_id}</Id>
  <Version>1.0.0</Version>
  <ProviderName>{_gateway_name()}</ProviderName>
  <DefaultLocale>de-DE</DefaultLocale>
  <DisplayName DefaultValue="EXO Signatur"/>
  <Description DefaultValue="Zeigt und fügt die Gateway-Signatur beim Verfassen ein"/>
  <IconUrl DefaultValue="{base}/addin/icon/32.png"/>
  <HighResolutionIconUrl DefaultValue="{base}/addin/icon/64.png"/>
  <SupportUrl DefaultValue="{base}"/>
  <AppDomains>
    <AppDomain>{hostname}</AppDomain>
  </AppDomains>
  <Hosts>
    <Host Name="Mailbox"/>
  </Hosts>
  <Requirements>
    <Sets>
      <Set Name="Mailbox" MinVersion="1.1"/>
    </Sets>
  </Requirements>
  <FormSettings>
    <Form xsi:type="ItemEdit">
      <DesktopSettings>
        <SourceLocation DefaultValue="{base}/addin/compose"/>
      </DesktopSettings>
    </Form>
  </FormSettings>
  <Permissions>ReadWriteItem</Permissions>
  <Rule xsi:type="RuleCollection" Mode="Or">
    <Rule xsi:type="ItemIs" ItemType="Message" FormType="Edit"/>
  </Rule>

  <VersionOverrides
    xmlns="http://schemas.microsoft.com/office/mailappversionoverrides"
    xmlns:bt="http://schemas.microsoft.com/office/officeappbasictypes/1.0"
    xsi:type="VersionOverridesV1_0">

    <Requirements>
      <bt:Sets DefaultMinVersion="1.3">
        <bt:Set Name="Mailbox"/>
      </bt:Sets>
    </Requirements>

    <Hosts>
      <Host xsi:type="MailHost">
        <DesktopFormFactor>
          <FunctionFile resid="functionFile"/>
          <ExtensionPoint xsi:type="MessageComposeCommandSurface">
            <OfficeTab id="TabDefault">
              <Group id="exo.sig.group">
                <Label resid="groupLabel"/>
                <Control xsi:type="Button" id="exo.sig.btn">
                  <Label resid="btnLabel"/>
                  <Supertip>
                    <Title resid="btnTitle"/>
                    <Description resid="btnDesc"/>
                  </Supertip>
                  <Icon>
                    <bt:Image size="16" resid="icon16"/>
                    <bt:Image size="32" resid="icon32"/>
                    <bt:Image size="80" resid="icon80"/>
                  </Icon>
                  <Action xsi:type="ShowTaskpane">
                    <SourceLocation resid="taskpaneUrl"/>
                  </Action>
                </Control>
              </Group>
            </OfficeTab>
          </ExtensionPoint>
        </DesktopFormFactor>
      </Host>
    </Hosts>

    <Resources>
      <bt:Images>
        <bt:Image id="icon16" DefaultValue="{base}/addin/icon/16.png"/>
        <bt:Image id="icon32" DefaultValue="{base}/addin/icon/32.png"/>
        <bt:Image id="icon80" DefaultValue="{base}/addin/icon/80.png"/>
      </bt:Images>
      <bt:Urls>
        <bt:Url id="functionFile" DefaultValue="{base}/addin/function"/>
        <bt:Url id="taskpaneUrl"  DefaultValue="{base}/addin/compose"/>
      </bt:Urls>
      <bt:ShortStrings>
        <bt:String id="groupLabel" DefaultValue="Signatur"/>
        <bt:String id="btnLabel"   DefaultValue="Signatur"/>
        <bt:String id="btnTitle"   DefaultValue="EXO Signatur"/>
      </bt:ShortStrings>
      <bt:LongStrings>
        <bt:String id="btnDesc" DefaultValue="Gateway-Signatur einfügen"/>
      </bt:LongStrings>
    </Resources>
  </VersionOverrides>
</OfficeApp>"""
    from fastapi.responses import Response
    return Response(content=xml, media_type="application/xml")


@app.get("/addin/icon/{size_str}")
async def addin_icon(size_str: str):
    """Serve a pen+signature icon as PNG (no PIL required)."""
    import struct, zlib
    size = max(16, min(int(size_str.split(".")[0]), 128))
    BR, BG, BB = 0, 120, 212   # #0078d4 blue background
    WR, WG, WB = 255, 255, 255  # white foreground

    pixels = [[(BR, BG, BB)] * size for _ in range(size)]

    def put(row: int, col: int) -> None:
        if 0 <= row < size and 0 <= col < size:
            pixels[row][col] = (WR, WG, WB)

    def bline(r1: int, c1: int, r2: int, c2: int, w: int = 1) -> None:
        dr, dc = abs(r2 - r1), abs(c2 - c1)
        sr, sc = (1 if r1 < r2 else -1), (1 if c1 < c2 else -1)
        err = dr - dc
        while True:
            for tr in range(-(w // 2), w // 2 + 1):
                for tc in range(-(w // 2), w // 2 + 1):
                    put(r1 + tr, c1 + tc)
            if r1 == r2 and c1 == c2:
                break
            e2 = 2 * err
            if e2 > -dc:
                err -= dc; r1 += sr
            if e2 < dr:
                err += dr; c1 += sc

    s = size / 32.0
    sw = max(1, round(2 * s))  # stroke width

    # Pen body: diagonal from top-right to lower-left
    bline(round(2*s), round(24*s), round(20*s), round(6*s), sw)
    # Pen nib: small filled triangle at the tip
    tip_r, tip_c = round(22*s), round(4*s)
    for i in range(max(1, round(4*s))):
        for j in range(max(1, round(3*s)) - i):
            put(tip_r + i, tip_c + j)
            put(tip_r + i, tip_c - j)
    # Signature underline near bottom
    sig_row = round(27*s)
    for c in range(round(3*s), round(29*s)):
        for dr in range(sw):
            put(sig_row + dr, c)
    # Small flourish: short curve at end of signature line
    for i in range(max(1, round(3*s))):
        put(sig_row + sw + i, round(26*s) + i)

    raw = b""
    for row in pixels:
        raw += b"\x00" + b"".join(bytes(px) for px in row)

    def _chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )
    from fastapi.responses import Response
    return Response(content=png, media_type="image/png")


@app.get("/addin/function", response_class=HTMLResponse)
async def addin_function(request: Request):
    """Minimal function-file page required by Office Add-in DesktopFormFactor."""
    html = (
        "<!DOCTYPE html><html><head>"
        '<meta charset="UTF-8">'
        '<script src="https://appsforoffice.microsoft.com/lib/1.1/hosted/office.js"></script>'
        "<script>Office.onReady(function(){});</script>"
        "</head><body></body></html>"
    )
    return HTMLResponse(content=html)


@app.get("/api/addin/signature")
async def api_addin_signature(email: str, template: str = "", user: str = Depends(_check_auth)):
    """Return the rendered (marked) signature HTML for the add-in taskpane."""
    email = (email or "").strip().lower()
    if not email:
        return JSONResponse({"marked_html": "", "preview_html": ""})

    import mailbox_match
    mailbox_cfg = mailbox_match.match_sender(settings_store.get("MAILBOX_CONFIG") or {}, email)
    default_template = mailbox_cfg.get("template") if isinstance(mailbox_cfg, dict) else None

    # Use requested template only if it's in the user's allowed set
    allowed = _addin_allowed_templates(email, mailbox_cfg)
    req = (template or "").strip()
    template_name = req if (req and req in allowed) else default_template

    try:
        user_data = await graph_client.get_user(email)
    except Exception:
        user_data = graph_client.UserData(mail=email)

    sig_html, _sig_txt = signature_engine.render(user_data, template_name)
    if not sig_html:
        return JSONResponse({"marked_html": "", "preview_html": ""})

    # Wrap with both comment markers (survived by Exchange, used by gateway) and
    # div ID sentinels (survived by Outlook editor, used by add-in replaceSig()).
    marked = (
        mail_processor._SIG_MARKER_START
        + '<div id="exo-sig-s"></div>'
        + sig_html
        + '<div id="exo-sig-e"></div>'
        + mail_processor._SIG_MARKER_END
    )
    return JSONResponse({"marked_html": marked, "preview_html": sig_html})


@app.get("/api/addin/templates")
async def api_addin_templates(email: str, user: str = Depends(_check_auth)):
    """Return list of templates available for this user in the add-in."""
    email = (email or "").strip().lower()
    import mailbox_match
    mailbox_cfg = mailbox_match.match_sender(settings_store.get("MAILBOX_CONFIG") or {}, email)
    if mailbox_cfg.get("use_policy", True):
        policies = settings_store.get("TEMPLATE_POLICIES") or {}
        mailbox_cfg = dict(mailbox_cfg)
        mailbox_cfg["addin_templates"] = policies.get("addin", "*")
        mailbox_cfg["template"] = policies.get("sig") or mailbox_cfg.get("template") or "default"
    allowed = _addin_allowed_templates(email, mailbox_cfg)
    default_template = (mailbox_cfg.get("template") if isinstance(mailbox_cfg, dict) else None) or "default"
    return JSONResponse({"templates": allowed, "default": default_template})


@app.get("/api/addin/update-redirect-uri")
async def addin_update_redirect_uri(request: Request, user: str = Depends(_require_admin)):
    """Start PKCE flow to add the external (no-port) redirect URI to the Bootstrap app in Entra.

    Uses the OLD registered URI (with port) as PKCE callback so the roundtrip completes
    when triggered from the internal URL (e.g. https://sig.zarenko.net:8080).
    The callback handler then registers the NEW no-port URI via patch_bootstrap_redirect_uri.
    Afterwards SSO via App Proxy (port 443) works without hitting :8080.
    """
    hostname = settings_store.get("PUBLIC_HOSTNAME") or ""
    port = config.WEBUI_PORT
    suffix = f":{port}" if port and port != 443 else ""
    old_uri = f"https://{hostname}{suffix}/auth/callback" if hostname else f"http://localhost:{port}/auth/callback"
    _state, auth_url = pkce_mod.create_session(old_uri, flow="patch_redirect_uri")
    return RedirectResponse(auth_url)


@app.get("/setup", response_class=HTMLResponse)
async def setup_wizard(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(security),
):
    """Setup wizard. Anonymous only while Step 1 (initial password) is not yet set."""
    # Determine auth state: session cookie takes precedence, Basic-Auth as fallback
    authed = bool(_get_session_user(request))
    if not authed and credentials and credentials.username and credentials.password:
        username = settings_store.get("WEBUI_USERNAME") or "admin"
        authed = (
            secrets.compare_digest(credentials.username.encode(), username.encode())
            and _check_password(credentials.password)
        )

    # Once any auth method is configured, block anonymous access
    if _setup_requires_auth() and not authed:
        return RedirectResponse(
            f"/auth/login?next={urllib.parse.quote('/setup', safe='')}",
            status_code=302,
        )

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
        "watcher_ok": __import__("updater").watcher_ok(),
        "authed": authed,
        "bootstrap_client_id": s.get("BOOTSTRAP_CLIENT_ID", ""),
        "bootstrap_redirect_uris": s.get("BOOTSTRAP_REDIRECT_URIS", []),
        "sso_redirect_uri": _build_redirect_uri(sso=True),
        "redirect_uri": _build_redirect_uri(),
        "webui_port": config.WEBUI_PORT,
    }
    addin_base_url = _addin_base_url(request)
    return templates.TemplateResponse(
        request=request, name="setup.html",
        context={
            "s": s, "e": effective, "active": "setup", "gateway_name": _gateway_name(),
            "addin_manifest_url": addin_base_url + "/addin/manifest.xml",
            "addin_url_warning": _addin_url_warning(addin_base_url),
            "webui_port": config.WEBUI_PORT,
        },
    )


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
            context={"s": settings_store.get_all(), "e": {}, "active": "setup",
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
            context={"s": settings_store.get_all(), "e": {}, "active": "setup",
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


# ── Routes: setup API endpoints ────────────────────────────────────────────────

@app.post("/api/setup/bootstrap-client")
async def api_setup_bootstrap_client(request: Request, user: str = Depends(_check_auth)):
    data = await request.json()
    client_id = (data.get("client_id") or "").strip()
    if not client_id:
        raise HTTPException(400, "client_id darf nicht leer sein")
    settings_store.update({"BOOTSTRAP_CLIENT_ID": client_id})
    # Feinschliff: HTTPS-Redirect optimistisch vormerken, damit bereits der ERSTE Login
    # das selbstschließende Popup nutzt statt Localhost-Paste. Greift nur, wenn diese URI
    # an der App registriert ist (z.B. Migration auf gleichem Hostnamen); andernfalls
    # nutzt der Nutzer den Localhost-Notausgang. patch_bootstrap_redirect_uri korrigiert
    # BOOTSTRAP_REDIRECT_URIS nach dem ersten erfolgreichen Login auf den echten Stand.
    https_uri = _build_redirect_uri(sso=True)
    if https_uri.startswith("https://"):
        uris = settings_store.get("BOOTSTRAP_REDIRECT_URIS") or []
        if https_uri not in uris:
            settings_store.update({"BOOTSTRAP_REDIRECT_URIS": uris + [https_uri]})
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
    old_pw = (data.get("old_password") or "").strip()
    new_pw = (data.get("password") or "").strip()
    if len(new_pw) < 8:
        raise HTTPException(400, "Passwort muss mindestens 8 Zeichen haben")
    stored_hash = settings_store.get("ADMIN_PASSWORD_HASH") or ""
    if stored_hash and not _verify_password(old_pw, stored_hash):
        raise HTTPException(400, "Aktuelles Passwort falsch")
    hashed = _hash_password(new_pw)
    settings_store.update({"ADMIN_PASSWORD_HASH": hashed})
    log.info("Admin password changed by %s", user)
    return JSONResponse({"ok": True})


@app.post("/api/smime/key-password")
async def api_smime_key_password(request: Request, user: str = Depends(_check_auth)):
    import smime_store as _smime
    data = await request.json()
    old_pw = data.get("old_password") or ""
    new_pw = data.get("new_password") or ""
    stored = settings_store.get("SMIME_KEY_PASSWORD") or ""
    if stored and old_pw != stored:
        raise HTTPException(400, "Aktuelles Passwort falsch")
    settings_store.update({"SMIME_KEY_PASSWORD": new_pw})
    failed = _smime.reencrypt_all_keys(old_password=stored)
    log.info("SMIME key password changed by %s; re-encrypted keys, failed: %s", user, failed)
    if failed:
        return JSONResponse({"ok": True, "warnings": failed})
    return JSONResponse({"ok": True})


@app.get("/api/whoami")
async def api_whoami(request: Request):
    """Returns current user info. Returns nulls when unauthenticated — no Basic-Auth challenge."""
    user = _get_session_user(request)
    if not user:
        return JSONResponse({"upn": None, "role": None})
    return JSONResponse({"upn": user.lower(), "role": _get_session_role(request)})


@app.get("/api/admin-users")
async def api_get_admin_users(_=Depends(_check_auth)):
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

    reinject_mode = settings_store.get("REINJECT_MODE") or "smtp"
    skip_inbound = reinject_mode in ("graph", "imap", "smtp587")
    result = setup_wizard.run_exo_connector_setup(
        app_id=app_id,
        tenant_domain=tenant_domain,
        smtp_proxy_hostname=hostname,
        skip_inbound_connector=skip_inbound,
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
    reinject_mode = settings_store.get("REINJECT_MODE") or "smtp"
    smtp_mode = reinject_mode == "smtp"
    return setup_wizard.verify_connector(smtp_mode=smtp_mode)


@app.get("/api/setup/verify/imap")
async def api_verify_imap(_=Depends(_check_auth)):
    import setup_wizard
    return setup_wizard.verify_imap()


@app.get("/api/setup/verify/smime")
async def api_verify_smime(_=Depends(_check_auth)):
    import setup_wizard
    return setup_wizard.verify_smime_rules()


@app.get("/api/setup/verify/azure")
async def api_verify_azure(_=Depends(_check_auth)):
    token = graph_client._acquire_token()
    if not token:
        return JSONResponse({"ok": False, "error": "Keine Graph-Zugangsdaten konfiguriert"})
    try:
        async with __import__("httpx").AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://graph.microsoft.com/v1.0/organization?$select=displayName",
                headers={"Authorization": f"Bearer {token}"},
            )
        resp.raise_for_status()
        orgs = resp.json().get("value", [])
        org = orgs[0].get("displayName", "?") if orgs else "?"
        return JSONResponse({"ok": True, "org": org})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)})


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
    deleted = []
    for p in (html_path, txt_path):
        if p.exists():
            p.unlink()
            deleted.append(p.name)
    if not deleted:
        raise HTTPException(404, f"Template '{name}' nicht gefunden")
    import signature_engine
    signature_engine._reload_env()
    log.info("Template '%s' deleted", name)
    return {"ok": True, "deleted": deleted}


@app.get("/api/mailboxes")
async def api_get_mailboxes(_=Depends(_check_auth)):
    """List all EXO mailboxes + their current MAILBOX_CONFIG + cached health status."""
    import asyncio
    import exo_mailboxes
    import mailbox_match
    raw = await asyncio.to_thread(exo_mailboxes.list_mailboxes)
    users = [{"email": m["primary"], "name": m.get("display_name") or m["primary"],
             "type": "user" if m.get("type") == "UserMailbox" else "shared"}
            for m in raw if m.get("primary")]
    config_map: dict = settings_store.get("MAILBOX_CONFIG") or {}
    health_map: dict = settings_store.get("MAILBOX_HEALTH") or {}
    bookings_map: dict = settings_store.get("USER_BOOKINGS") or {}
    result = []
    for u in users:
        email = u["email"]
        cfg = mailbox_match.match_sender(config_map, email)
        h = health_map.get(email, {})
        result.append({
            "email": email,
            "name": u["name"],
            "type": u.get("type", "user"),
            "sig": cfg.get("sig", False),
            "smime": cfg.get("smime", False),
            "template": cfg.get("template", "default"),
            "addin_templates": cfg.get("addin_templates", []),
            "use_policy": cfg.get("use_policy", True),
            "health_overall": h.get("overall"),
            "health_checked": h.get("last_checked"),
            "health_checks": h.get("checks", {}),
            "bookings_url": bookings_map.get(email, ""),
        })
    # Also include configured mailboxes Graph didn't return (removed users / guid-keyed).
    for key, cfg in config_map.items():
        cemail = key.lower() if "@" in key else (cfg.get("primary") or "").lower()
        if cemail and not any(r["email"] == cemail for r in result):
            h = health_map.get(cemail, {})
            result.append({
                "email": cemail,
                "name": cfg.get("display_name") or cemail,
                "type": "user",
                "sig": cfg.get("sig", False),
                "smime": cfg.get("smime", False),
                "template": cfg.get("template", "default"),
                "addin_templates": cfg.get("addin_templates", []),
                "use_policy": cfg.get("use_policy", True),
                "health_overall": h.get("overall"),
                "health_checked": h.get("last_checked"),
                "health_checks": h.get("checks", {}),
                "bookings_url": bookings_map.get(cemail, ""),
            })
    result.sort(key=lambda r: (r.get("name") or r["email"]).lower())
    return {"mailboxes": result}


@app.get("/api/health/mailboxes")
async def api_health_mailboxes(_=Depends(_check_auth)):
    """Return current cached MAILBOX_HEALTH data."""
    return settings_store.get("MAILBOX_HEALTH") or {}


@app.post("/api/health/mailboxes")
async def api_health_run(_=Depends(_check_auth)):
    """Run health checks for all configured mailboxes and return results."""
    import health_check
    results = await health_check.run_all_checks()
    return {"ok": True, "results": results}


@app.get("/api/health/audit-log")
async def api_health_audit_log(_=Depends(_check_auth)):
    """Return GATEWAY_AUDIT_LOG entries."""
    return settings_store.get("GATEWAY_AUDIT_LOG") or []


@app.get("/api/mailboxes/migrate/preview")
async def api_mailbox_migrate_preview(user: str = Depends(_require_admin)):
    """Dry-run: show how MAILBOX_CONFIG would migrate to ExchangeGuid anchors.
    Reads live EXO mailboxes; writes NOTHING."""
    import asyncio
    import exo_mailboxes
    import mailbox_migrate
    mailboxes = await asyncio.to_thread(exo_mailboxes.list_mailboxes, True)
    if not mailboxes:
        return JSONResponse({"ok": False, "error": "EXO-Postfachliste leer/nicht verfügbar."},
                            status_code=503)
    current: dict = settings_store.get("MAILBOX_CONFIG") or {}
    plan = mailbox_migrate.plan_migration(current, mailboxes)
    return JSONResponse({
        "ok": True,
        "exo_mailbox_count": len(mailboxes),
        "current_keys": list(current.keys()),
        "migrated": plan["migrated"],
        "merges": plan["merges"],
        "orphans": plan["orphans"],
        "kept": plan["kept"],
        "new_config": plan["new_config"],
    })


@app.post("/api/mailboxes/migrate/apply")
async def api_mailbox_migrate_apply(user: str = Depends(_require_admin)):
    """Apply the guid migration: rewrite MAILBOX_CONFIG to ExchangeGuid anchors.
    Safe because handler/guard/health/UI all resolve via the address reverse-index."""
    import asyncio
    import exo_mailboxes
    import mailbox_migrate
    mailboxes = await asyncio.to_thread(exo_mailboxes.list_mailboxes, True)
    if not mailboxes:
        return JSONResponse({"ok": False, "error": "EXO-Postfachliste leer/nicht verfügbar."},
                            status_code=503)
    current: dict = settings_store.get("MAILBOX_CONFIG") or {}
    plan = mailbox_migrate.plan_migration(current, mailboxes)
    settings_store.update({"MAILBOX_CONFIG": plan["new_config"]})
    log.info("MAILBOX_CONFIG migrated to guid anchors by %s: %d entries, %d orphans",
             user, len(plan["new_config"]), len(plan["orphans"]))
    return JSONResponse({"ok": True, "entries": len(plan["new_config"]),
                         "migrated": plan["migrated"], "merges": plan["merges"],
                         "orphans": plan["orphans"]})


@app.post("/api/mailboxes/save")
async def api_save_mailboxes(body: dict, _=Depends(_check_auth)):
    """Save MAILBOX_CONFIG (ExchangeGuid-anchored) and update the EXO Distribution
    Group membership. (The transport rule 'Route via EXO Signature Gateway' is NOT
    touched — it targets the DG via FromMemberOf; only DG members change here.)

    Each mailbox is keyed by its ExchangeGuid + an address cache so the config
    survives rename/address changes; falls back to the e-mail key if EXO can't
    resolve it (nothing lost)."""
    import asyncio
    import exo_mailboxes
    mailboxes = body.get("mailboxes", [])
    # address → EXO record (cached; empty on EXO failure → graceful e-mail-key fallback)
    exo_list = await asyncio.to_thread(exo_mailboxes.list_mailboxes, False)
    addr_to_mb: dict = {}
    for mb in exo_list:
        for a in mb.get("addresses", []):
            addr_to_mb[str(a).lower()] = mb
        p = (mb.get("primary") or "").lower()
        if p:
            addr_to_mb[p] = mb
    config_map: dict = {}
    enabled_members: list[str] = []
    for m in mailboxes:
        email = (m.get("email") or "").lower().strip()
        if not email:
            continue
        sig = bool(m.get("sig", False))
        smime = bool(m.get("smime", False))
        if not (sig or smime):
            continue    # both off → passthrough by default, not stored
        template = (m.get("template") or "default").strip()
        addin_tpl = m.get("addin_templates", [])
        use_policy = bool(m.get("use_policy", True))
        entry: dict = {"sig": sig, "smime": smime, "use_policy": use_policy}
        if template and template != "default":
            entry["template"] = template
        if addin_tpl == "*" or (isinstance(addin_tpl, list) and addin_tpl):
            entry["addin_templates"] = addin_tpl
        mb = addr_to_mb.get(email)
        if mb:
            key = mb["guid"]
            entry["known_addresses"] = list(mb.get("addresses", []))
            entry["primary"] = mb.get("primary", email)
            entry["display_name"] = mb.get("display_name", "")
            member = mb.get("primary", email)
        else:
            key = email          # EXO couldn't resolve → keep e-mail-keyed
            member = email
        if key in config_map:    # two addresses of the same mailbox → OR the flags
            config_map[key]["sig"] = config_map[key].get("sig") or sig
            config_map[key]["smime"] = config_map[key].get("smime") or smime
        else:
            config_map[key] = entry
        if member not in enabled_members:
            enabled_members.append(member)
    settings_store.update({"MAILBOX_CONFIG": config_map})

    s = settings_store.get_all()
    app_id = s.get("CLIENT_ID") or config.CLIENT_ID
    tenant_domain = s.get("TENANT_DOMAIN") or ""

    # Bookings-URLs für neu hinzugekommene Postfächer im Hintergrund ermitteln
    existing_bookings: dict = settings_store.get("USER_BOOKINGS") or {}
    new_emails = [e for e in enabled_members if e not in existing_bookings]
    if new_emails and app_id and tenant_domain:
        import setup_wizard as _sw
        async def _fetch_new():
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _sw.run_fetch_bookings_urls(app_id, tenant_domain, new_emails)
            )
            if result.get("ok") and result.get("urls"):
                current: dict = settings_store.get("USER_BOOKINGS") or {}
                current.update(result["urls"])
                settings_store.update({"USER_BOOKINGS": current})
        asyncio.create_task(_fetch_new())

    # Update EXO Distribution Group if wizard is complete
    if body.get("update_dg") and app_id and tenant_domain:
        import setup_wizard
        result = setup_wizard.run_mailbox_dg_update(app_id, tenant_domain, enabled_members)
        return {"ok": result["ok"], "saved": True, "dg_output": result.get("output", "")}
    return {"ok": True, "saved": True}


@app.post("/api/mailboxes/fetch-bookings-urls")
async def api_fetch_bookings_urls(_=Depends(_check_auth)):
    """Fetch ExchangeGuid for all configured mailboxes via PS and compute Bookings URLs."""
    import setup_wizard as _sw
    app_id = settings_store.get("CLIENT_ID") or config.CLIENT_ID or ""
    tenant = settings_store.get("TENANT_DOMAIN") or ""
    import mailbox_match
    mailbox_cfg: dict = settings_store.get("MAILBOX_CONFIG") or {}
    emails = mailbox_match.configured_addresses(mailbox_cfg)
    if not emails:
        return JSONResponse({"ok": False, "error": "Keine Postfächer in MAILBOX_CONFIG konfiguriert."})
    result = await asyncio.get_event_loop().run_in_executor(
        None, lambda: _sw.run_fetch_bookings_urls(app_id, tenant, emails)
    )
    if result["ok"] and result["urls"]:
        existing: dict = settings_store.get("USER_BOOKINGS") or {}
        existing.update(result["urls"])
        settings_store.update({"USER_BOOKINGS": existing})
    return JSONResponse(result)


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
    from datetime import datetime as _dt
    import smime_store as _smime_store
    import stats as _stats_mod2
    pw_change = _password_change_required()
    total = get_stats()
    daily = _stats_mod2.get_daily()
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
        },
    )


@app.get("/template", response_class=HTMLResponse)
async def template_editor(request: Request, user: str = Depends(_check_auth)):
    import signature_engine as _sig_engine
    name = request.query_params.get("name") or "default"
    # Resolve filenames
    if name == "default":
        html_path = Path(config.TEMPLATE_DIR) / "signature.html"
        txt_path = Path(config.TEMPLATE_DIR) / "signature.txt"
    else:
        html_path = Path(config.TEMPLATE_DIR) / f"{name}.html"
        txt_path = Path(config.TEMPLATE_DIR) / f"{name}.txt"
    template_list = _sig_engine.list_templates()
    custom_vars = [cv["name"] for cv in (settings_store.get("CUSTOM_TEMPLATE_VARS") or []) if cv.get("name")]
    return templates.TemplateResponse(
        request=request, name="template_editor.html",
        context={
            "html_content": html_path.read_text() if html_path.exists() else "",
            "txt_content": txt_path.read_text() if txt_path.exists() else "",
            "active": "template",
            "saved": request.query_params.get("saved"),
            "current_template": name,
            "template_list": template_list,
            "custom_vars": custom_vars,
            "gateway_name": _gateway_name(),
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
    user: str = Depends(_check_auth),
):
    """Render a signature template for a given email address (Graph lookup)."""
    import graph_client as _gc
    user_data = _gc.UserData()
    error = None
    if email:
        try:
            user_data = await _gc.get_user(email)
        except Exception as exc:
            error = str(exc)
    sig_html, sig_txt = signature_engine.render(user_data, template_name=template)
    return JSONResponse({"html": sig_html, "txt": sig_txt, "error": error})


@app.get("/mailboxes", response_class=HTMLResponse)
async def mailboxes_page(request: Request, user: str = Depends(_require_admin)):
    import signature_engine as _sig_engine
    templates_list = _sig_engine.list_templates()
    return templates.TemplateResponse(
        request=request, name="mailboxes.html",
        context={"active": "mailboxes", "templates_list": templates_list,
                 "gateway_name": _gateway_name(),
                 "addin_enabled": bool(settings_store.get("ADDIN_ENABLED"))},
    )


def _addin_base_url(request: Request) -> str:
    """Return the public base URL for the add-in manifest.

    Priority: 1) ADDIN_BASE_URL setting  2) X-Forwarded-Host header  3) request.url
    """
    explicit = (settings_store.get("ADDIN_BASE_URL") or "").rstrip("/")
    if explicit:
        return explicit
    fwd_host  = request.headers.get("x-forwarded-host") or request.headers.get("x-original-host")
    fwd_proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    if fwd_host:
        return f"{fwd_proto}://{fwd_host.split(':')[0]}"
    return f"{request.url.scheme}://{request.url.netloc}"


def _addin_allowed_templates(email: str, mailbox_cfg: dict) -> list[str]:
    """Return sorted list of templates the user may access in the add-in.

    addin_templates == "*"  → all available templates
    addin_templates == []   → only the mailbox default template
    addin_templates == [..] → explicit list (intersected with existing templates)
    """
    all_tpls = signature_engine.list_templates()
    default = (mailbox_cfg.get("template") if isinstance(mailbox_cfg, dict) else None) or "default"
    setting = mailbox_cfg.get("addin_templates") if isinstance(mailbox_cfg, dict) else None
    if setting == "*":
        return all_tpls
    if isinstance(setting, list) and setting:
        # keep declared order, filter non-existing
        known = set(all_tpls)
        result = [t for t in setting if t in known]
        if default not in result:
            result = [default] + result
        return result
    # No setting → only the default template
    return [default] if default in all_tpls else ["default"]


def _addin_url_warning(base_url: str) -> str:
    """Return a warning string if the URL is unlikely to be publicly reachable, else ''."""
    from urllib.parse import urlparse
    import ipaddress
    p = urlparse(base_url)
    if p.scheme != "https":
        return "Kein HTTPS — M365 erfordert eine sichere Verbindung"
    if p.port:
        return f"Nicht-Standard-Port :{p.port} — extern möglicherweise nicht erreichbar"
    host = p.hostname or ""
    try:
        addr = ipaddress.ip_address(host)
        if addr.is_private or addr.is_loopback:
            return "Private/lokale IP-Adresse — extern nicht erreichbar"
    except ValueError:
        if host in ("localhost",):
            return "Localhost — extern nicht erreichbar"
    return ""


@app.get("/api/settings/template-policies")
async def api_get_template_policies(_=Depends(_check_auth)):
    return JSONResponse(settings_store.get("TEMPLATE_POLICIES") or {"sig": "default"})


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
    return JSONResponse({"ok": True})


@app.post("/api/maintenance/mode")
async def api_set_maintenance_mode(request: Request, _: str = Depends(_require_admin)):
    body = await request.json()
    enabled = bool(body.get("enabled", False))
    settings_store.update({"MAINTENANCE_MODE": enabled})
    return JSONResponse({"ok": True, "maintenance_mode": enabled})


@app.post("/api/settings/sender-mailboxes/refresh")
async def api_refresh_sender_mailboxes(user: str = Depends(_require_admin)):
    import asyncio
    import exo_mailboxes
    try:
        await asyncio.to_thread(exo_mailboxes.list_mailboxes, True)  # force refresh
    except Exception as exc:
        raise HTTPException(500, str(exc))
    return JSONResponse({"ok": True, "mailboxes": exo_mailboxes.as_sender_list()})


@app.post("/api/settings/notification-mailbox/create-shared")
async def api_create_notification_shared_mailbox(user: str = Depends(_require_admin)):
    import asyncio
    import setup_wizard
    import exo_mailboxes
    result = await asyncio.to_thread(setup_wizard.run_create_notification_mailbox)
    if not result.get("ok"):
        raise HTTPException(500, result.get("output") or "Anlage fehlgeschlagen")
    try:
        await asyncio.to_thread(exo_mailboxes.list_mailboxes, True)
    except Exception:
        pass
    return JSONResponse({"ok": True, "email": result.get("email", ""), "mailboxes": exo_mailboxes.as_sender_list()})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user: str = Depends(_require_admin)):
    import asyncio
    import exo_mailboxes
    try:
        await asyncio.to_thread(exo_mailboxes.list_mailboxes)
        sender_mailboxes = exo_mailboxes.as_sender_list()
    except Exception:
        sender_mailboxes = []
    return templates.TemplateResponse(
        request=request, name="settings.html",
        context={
            "s": settings_store.get_all(),
            "active": "settings",
            "saved": request.query_params.get("saved"),
            "gateway_name": _gateway_name(),
            "sender_mailboxes": sender_mailboxes,
        },
    )


@app.get("/settings/signature", response_class=HTMLResponse)
async def settings_signature_page(request: Request, user: str = Depends(_require_admin)):
    import asyncio
    import exo_mailboxes
    try:
        await asyncio.to_thread(exo_mailboxes.list_mailboxes)
        sender_mailboxes = exo_mailboxes.as_sender_list()
    except Exception:
        sender_mailboxes = []
    return templates.TemplateResponse(
        request=request, name="settings_signature.html",
        context={
            "s": settings_store.get_all(),
            "active": "settings-signature",
            "saved": request.query_params.get("saved"),
            "gateway_name": _gateway_name(),
            "sender_mailboxes": sender_mailboxes,
            "custom_var_entra_fields": graph_client.CUSTOM_VAR_ENTRA_FIELDS,
        },
    )


@app.get("/settings/smime", response_class=HTMLResponse)
async def settings_smime_page(request: Request, user: str = Depends(_require_admin)):
    return templates.TemplateResponse(
        request=request, name="settings_smime.html",
        context={
            "s": settings_store.get_all(),
            "active": "settings-smime",
            "saved": request.query_params.get("saved"),
            "gateway_name": _gateway_name(),
        },
    )


@app.get("/settings/connect", response_class=HTMLResponse)
async def settings_connect_page(request: Request, user: str = Depends(_require_admin)):
    import hub_client
    import ca_backends as _ca
    return templates.TemplateResponse(
        request=request, name="settings_connect.html",
        context={
            "s": settings_store.get_all(),
            "active": "settings-connect",
            "gateway_name": _gateway_name(),
            "hub_registered": hub_client.is_registered(),
            "hub_cert_registered": hub_client.cert_is_registered(),
            "sectigo_ready": _ca.get_backend("sectigo").is_ready(),
        },
    )


@app.get("/settings/update")
async def settings_update_redirect(user: str = Depends(_require_admin)):
    # Update-Tab wurde mit Backup zusammengelegt
    return RedirectResponse("/backup", status_code=308)


@app.get("/outlook-addin")
async def outlook_addin_page_redirect(user: str = Depends(_require_admin)):
    # Outlook Add-in ist jetzt Teil von Einrichtung (eigener wizard-step)
    return RedirectResponse("/setup#step-addin", status_code=308)


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
    to = _notif._get_notify_to()
    if not to:
        raise HTTPException(400, "Kein Benachrichtigungs-Empfänger konfiguriert")
    ok = _notif._graph_send(to, "EXO Gateway – Test-Benachrichtigung",
                            _notif._html_wrap("Test-Benachrichtigung", "#27ae60",
                                              "<p>Die Benachrichtigungsfunktion ist korrekt konfiguriert.</p>"))
    if not ok:
        raise HTTPException(500, "Senden fehlgeschlagen – Einstellungen prüfen")
    return JSONResponse({"ok": True})


@app.post("/api/setup/notification-dg")
async def api_setup_notification_dg(request: Request, user: str = Depends(_check_auth)):
    """Create/update notification Distribution Group in EXO and save recipients."""
    import setup_wizard
    data = await request.json()
    recipients = [r.strip().lower() for r in (data.get("recipients") or []) if r.strip()]
    result = await asyncio.get_event_loop().run_in_executor(
        None, setup_wizard.run_notification_dg_update, recipients
    )
    if result.get("ok"):
        patch: dict = {"NOTIFICATION_RECIPIENTS": recipients}
        if result.get("email"):
            patch["NOTIFICATION_DG_EMAIL"] = result["email"]
        settings_store.update(patch)
        log.info("Notification DG updated by %s: %d members, DG=%s", user, len(recipients), result.get("email"))
    return JSONResponse({
        "ok": result.get("ok", False),
        "email": result.get("email", ""),
        "output": result.get("output", ""),
    })


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
        context={"cfg": cfg, "active": "config", "gateway_name": _gateway_name()},
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


@app.get("/backup", response_class=HTMLResponse)
async def backup_page(request: Request, user: str = Depends(_require_admin)):
    return templates.TemplateResponse(
        request=request, name="backup.html",
        context={"active": "backup", "gateway_name": _gateway_name(),
                 "version": config.VERSION},
    )


@app.get("/api/backup/download")
async def api_backup_download(user: str = Depends(_require_admin)):
    """Vollständiges Backup als ZIP herunterladen."""
    import backup_manager as _bm
    import asyncio as _aio
    from fastapi.responses import Response as _Resp
    zip_bytes, filename = await _aio.get_event_loop().run_in_executor(
        None, _bm.create_backup
    )
    return _Resp(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/backup/restore")
async def api_backup_restore(
    file: UploadFile = File(...),
    _user: str = Depends(_require_admin),
):
    """Backup-ZIP hochladen und wiederherstellen."""
    import backup_manager as _bm
    data = await file.read()
    result = await __import__("asyncio").get_event_loop().run_in_executor(
        None, _bm.restore_backup, data
    )
    return JSONResponse(result)


@app.get("/debug", response_class=HTMLResponse)
async def debug_page(request: Request, user: str = Depends(_require_admin)):
    import hub_client
    return templates.TemplateResponse(
        request=request, name="debug.html",
        context={"active": "debug", "s": settings_store.get_all(),
                 "gateway_name": _gateway_name(),
                 "hub_configured": hub_client.is_configured(),
                 "hub_registered": hub_client.is_registered(),
                 "hub_cert_registered": hub_client.cert_is_registered(),
                 "current_version": config.VERSION},
    )


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


@app.get("/smime", response_class=HTMLResponse)
async def smime_page_v2(request: Request, user: str = Depends(_require_admin)):
    import smime_store
    import ca_backends as _ca
    import acme_state as _acme_state
    import keyvault as _kv
    config_map: dict = settings_store.get("MAILBOX_CONFIG") or {}
    smime_from_config = {
        (key.lower() if "@" in key else (cfg.get("primary") or "").lower())
        for key, cfg in config_map.items() if cfg.get("smime")
    } - {""}
    smime_from_certs = {c["email"] for c in smime_store.list_certs()}
    all_emails = sorted(smime_from_config | smime_from_certs)
    smime_users = [{"email": email, "certs": smime_store.list_user_certs(email)} for email in all_emails]
    acme_orders = {em: _acme_state.get_order(em) for em in all_emails if _acme_state.get_order(em)}
    # Key Vault status per email (only if configured) — use cached status, not live queries
    kv_configured = _kv.is_configured()
    kv_status: dict = settings_store.get("KV_KEY_STATUS") or {}
    kv_keys: dict[str, bool | None] = {
        em: kv_status.get(em, {}).get("exists", None) for em in all_emails
    }
    kv_mode = settings_store.get("KV_KEY_MODE") or "fallback"
    has_any_local_key = any(
        c.get("has_local_key") or c.get("has_kv_backup")
        for u in smime_users for c in u["certs"]
    )
    has_any_unmigrated_key = any(
        c.get("has_local_key")
        for u in smime_users for c in u["certs"]
    )
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
            "kv_configured": kv_configured,
            "kv_keys": kv_keys,
            "kv_url": _kv.vault_url(),
            "kv_mode": kv_mode,
            "has_any_local_key": has_any_local_key,
            "has_any_unmigrated_key": has_any_unmigrated_key,
            "gateway_name": _gateway_name(),
        },
    )


@app.post("/api/smime/kv-status/refresh")
async def api_smime_kv_status_refresh(_=Depends(_check_auth)):
    """Refresh Azure Key Vault key-existence status for all S/MIME users (parallel queries)."""
    import smime_store
    import keyvault as _kv
    if not _kv.is_configured():
        return JSONResponse({"ok": False, "detail": "Key Vault nicht konfiguriert"}, status_code=400)
    config_map: dict = settings_store.get("MAILBOX_CONFIG") or {}
    smime_from_config = {
        (key.lower() if "@" in key else (cfg.get("primary") or "").lower())
        for key, cfg in config_map.items() if cfg.get("smime")
    } - {""}
    smime_from_certs = {c["email"] for c in smime_store.list_certs()}
    all_emails = sorted(smime_from_config | smime_from_certs)
    if not all_emails:
        return JSONResponse({"ok": True, "results": {}})
    results_list = await asyncio.gather(*[_kv.key_exists(em) for em in all_emails])
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_status = {em: {"exists": bool(ex), "checked": now_iso}
                  for em, ex in zip(all_emails, results_list)}
    settings_store.update({"KV_KEY_STATUS": new_status})
    log.info("KV key status refreshed for %d emails", len(all_emails))
    return JSONResponse({"ok": True, "results": new_status})


@app.get("/api/smime/cert/download/{email}/{slot_id}")
async def api_smime_cert_download(email: str, slot_id: str, _=Depends(_check_auth)):
    """Download a signing certificate as DER-encoded .cer file."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from fastapi.responses import Response as _Response
    import smime_store
    email = urllib.parse.unquote(email).lower().strip()
    cert_path = smime_store.SMIME_DIR / email / "certs" / slot_id / "cert.pem"
    if not cert_path.exists():
        raise HTTPException(404, "Zertifikat nicht gefunden")
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        der_bytes = cert.public_bytes(serialization.Encoding.DER)
        safe_email = email.replace("@", "_").replace(".", "_")
        filename = f"{safe_email}_{slot_id}.cer"
        return _Response(
            content=der_bytes,
            media_type="application/pkix-cert",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        log.error("Cert download error for %s/%s: %s", email, slot_id, exc)
        raise HTTPException(500, str(exc))


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

    # Guard: a mailbox must be activated for S/MIME before it may obtain a cert
    # (clean 400 here; also enforced in initiate_acme_order so nothing bypasses it).
    import mailbox_match
    if not mailbox_match.match_sender(settings_store.get("MAILBOX_CONFIG") or {}, email).get("smime"):
        raise HTTPException(400, f"Postfach {email} ist nicht für S/MIME aktiviert — "
                                 f"erst das Postfach für S/MIME aktivieren, dann Zertifikat beziehen.")

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
    except _acme_state.EnrollmentNotAllowed as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        log.error("ACME initiate failed for %s: %s", email, exc)
        raise HTTPException(500, str(exc))


# ── Azure Key Vault API endpoints ─────────────────────────────────────────────

@app.get("/api/setup/keyvault/test")
async def api_keyvault_test(url: str = "", _: str = Depends(_require_admin)):
    """Test Key Vault connectivity. ?url=https://... to test a specific URL."""
    import keyvault
    ok, msg = await keyvault.test_connection(url or None)
    return JSONResponse({"ok": ok, "message": msg})


@app.get("/api/setup/keyvault/arm-auth-url")
async def api_keyvault_arm_auth_url(request: Request, _: str = Depends(_require_admin)):
    """Return an auth URL for the user to grant delegated ARM access."""
    redirect_uri = _build_redirect_uri(sso=True)
    _state, auth_url = pkce_mod.create_session(
        redirect_uri, scopes=pkce_mod.ARM_SCOPES, flow="arm"
    )
    return JSONResponse({"auth_url": auth_url})


@app.post("/api/setup/keyvault/arm-paste")
async def api_keyvault_arm_paste(request: Request, user: str = Depends(_require_admin)):
    """Exchange pasted ARM callback URL for a delegated ARM token and store it in-memory."""
    import keyvault
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
        raise HTTPException(400, "URL enthält keinen Code oder State")
    session_obj = pkce_mod.pop_session(state)
    if not session_obj:
        raise HTTPException(400, "Sitzung abgelaufen — bitte erneut auf 'Azure-Zugriff holen' klicken")
    if session_obj.get("flow") != "arm":
        raise HTTPException(400, "Falscher Flow-Typ")
    try:
        token_resp = await pkce_mod.exchange_code(
            code, session_obj["verifier"], session_obj["redirect_uri"],
            scopes=pkce_mod.ARM_SCOPES,
        )
    except Exception as exc:
        raise HTTPException(400, f"Token-Austausch fehlgeschlagen: {exc}")
    arm_token = token_resp.get("access_token")
    if not arm_token:
        raise HTTPException(400, "Kein ARM-Token erhalten")
    expires_in = int(token_resp.get("expires_in", 3600))
    upn = _get_session_user(request) or user
    keyvault.store_user_arm_token(upn, arm_token, expires_in)
    log.info("ARM delegated token stored for %s (expires_in=%s)", upn, expires_in)
    return JSONResponse({"ok": True})


@app.get("/api/setup/keyvault/subscriptions")
async def api_keyvault_subscriptions(request: Request, _: str = Depends(_require_admin)):
    """List Azure subscriptions — uses delegated user token if available, else app SP."""
    import keyvault
    upn = _get_session_user(request) or ""
    user_tok = keyvault.get_user_arm_token(upn) if upn else None
    ok, msg, subs = await keyvault.list_subscriptions(arm_token=user_tok)
    return JSONResponse({"ok": ok, "message": msg, "subscriptions": subs,
                         "delegated": bool(user_tok)})


@app.get("/api/setup/keyvault/resource-groups")
async def api_keyvault_resource_groups(request: Request, subscription_id: str,
                                        _: str = Depends(_require_admin)):
    """List resource groups — uses delegated user token if available, else app SP."""
    import keyvault
    upn = _get_session_user(request) or ""
    user_tok = keyvault.get_user_arm_token(upn) if upn else None
    ok, msg, rgs = await keyvault.list_resource_groups(subscription_id, arm_token=user_tok)
    return JSONResponse({"ok": ok, "message": msg, "resource_groups": rgs})


@app.get("/api/setup/keyvault/vaults")
async def api_keyvault_vaults(request: Request, subscription_id: str,
                               _: str = Depends(_require_admin)):
    """List Key Vaults in subscription — uses delegated user token if available."""
    import keyvault
    upn = _get_session_user(request) or ""
    user_tok = keyvault.get_user_arm_token(upn) if upn else None
    ok, msg, vaults = await keyvault.list_vaults(subscription_id, arm_token=user_tok)
    return JSONResponse({"ok": ok, "message": msg, "vaults": vaults})


@app.post("/api/setup/keyvault/create")
async def api_keyvault_create(request: Request, user: str = Depends(_require_admin)):
    """Create a new Azure Key Vault — uses delegated user token if available."""
    import keyvault
    import graph_client as _gc
    data = await request.json()
    subscription_id = (data.get("subscription_id") or "").strip()
    resource_group = (data.get("resource_group") or "").strip()
    vault_name = (data.get("vault_name") or "").strip()
    location = (data.get("location") or "").strip()
    create_rg = bool(data.get("create_rg", False))
    if not all([subscription_id, resource_group, vault_name, location]):
        raise HTTPException(400, "subscription_id, resource_group, vault_name, location sind Pflichtfelder")
    tenant_id, client_id, _ = _gc._get_effective_credentials()
    if not tenant_id or not client_id:
        raise HTTPException(400, "Entra-App-Registrierung noch nicht konfiguriert")
    upn = _get_session_user(request) or user
    user_tok = keyvault.get_user_arm_token(upn) if upn else None
    ok, message, vault_url, *_rest = await keyvault.create_vault(
        subscription_id, resource_group, vault_name, location,
        tenant_id, client_id, create_rg, arm_token=user_tok,
    )
    resource_id = _rest[0] if _rest else ""
    return JSONResponse({"ok": ok, "message": message, "vault_url": vault_url, "resource_id": resource_id})


@app.post("/api/setup/keyvault/assign-role")
async def api_keyvault_assign_role(request: Request, user: str = Depends(_require_admin)):
    """Idempotently assign Key Vault Crypto Officer role to the app SP on a given vault."""
    import keyvault
    import graph_client as _gc
    data = await request.json()
    resource_id = (data.get("resource_id") or "").strip()
    vault_url = (data.get("vault_url") or "").strip()
    upn = _get_session_user(request) or user
    user_tok = keyvault.get_user_arm_token(upn) if upn else None
    if not resource_id:
        # Frontend doesn't always know the resource_id (e.g. after a page reload where
        # only KEYVAULT_URL was persisted) — resolve it by vault name via Resource Graph.
        if not vault_url:
            raise HTTPException(400, "resource_id oder vault_url ist Pflichtfeld")
        resource_id = await keyvault.find_vault_resource_id(vault_url, arm_token=user_tok) or ""
        if not resource_id:
            return JSONResponse({
                "ok": False,
                "message": (
                    f"Vault '{vault_url}' wurde in keiner sichtbaren Subscription gefunden — "
                    "prüfe, ob das angemeldete Azure-Konto Zugriff auf die Subscription/Resource "
                    "Group des Vaults hat."
                ),
            })
    _, client_id, _ = _gc._get_effective_credentials()
    if not client_id:
        raise HTTPException(400, "Entra-App-Registrierung noch nicht konfiguriert")
    ok, message = await keyvault.ensure_crypto_officer_role(resource_id, client_id, arm_token=user_tok)
    if ok:
        settings_store.update({"KEYVAULT_RESOURCE_ID": resource_id})
    return JSONResponse({"ok": ok, "message": message, "resource_id": resource_id})


@app.post("/api/setup/keyvault/save")
async def api_keyvault_save(request: Request, _: str = Depends(_require_admin)):
    """Save Key Vault URL to settings."""
    data = await request.json()
    kv_url = (data.get("url") or "").strip().rstrip("/")
    resource_id = (data.get("resource_id") or "").strip()
    to_save = {"KEYVAULT_URL": kv_url}
    if resource_id:
        to_save["KEYVAULT_RESOURCE_ID"] = resource_id
    settings_store.update(to_save)
    log.info("Key Vault URL saved: %s", kv_url or "(cleared)")
    return JSONResponse({"ok": True})


@app.post("/api/smime/keyvault/migrate/{email}")
async def api_keyvault_migrate(email: str, _: str = Depends(_require_admin)):
    """Migrate active S/MIME private key slot to Azure Key Vault."""
    import smime_store
    email = email.lower().strip()
    result = await smime_store.migrate_key_to_keyvault(email)
    if not result["ok"]:
        raise HTTPException(400, result["error"])
    log.info("S/MIME key migrated to Key Vault for %s slot %s (key_id=%s)", email, result.get("slot_id"), result.get("key_id"))
    return JSONResponse(result)


@app.post("/api/smime/keyvault/migrate/{email}/{slot_id}")
async def api_keyvault_migrate_slot(email: str, slot_id: str, _: str = Depends(_require_admin)):
    """Migrate a specific S/MIME cert slot's private key to Azure Key Vault."""
    import smime_store
    email = email.lower().strip()
    result = await smime_store.migrate_key_to_keyvault(email, slot_id=slot_id)
    if not result["ok"]:
        raise HTTPException(400, result["error"])
    log.info("S/MIME key migrated to Key Vault for %s slot %s (key_id=%s)", email, slot_id, result.get("key_id"))
    return JSONResponse(result)


@app.get("/api/smime/backup-key/{email}/{slot_id}")
async def api_smime_backup_key_download(
    email: str, slot_id: str, _: str = Depends(_require_admin)
):
    """Download the local backup key (key.pem.bak or key.pem) for a cert slot.
    If the key is unencrypted and SMIME_KEY_PASSWORD is set, encrypt on the fly before download."""
    from pathlib import Path as _Path
    from fastapi.responses import Response as _Resp
    import config as _cfg

    if settings_store.get("KV_KEY_MODE") == "strict":
        raise HTTPException(403, "Backup-Download im Strict-Modus deaktiviert")

    email = email.lower().strip()
    smime_dir = _Path("/app/data/smime")
    slot_dir = smime_dir / email / "certs" / slot_id

    bak = slot_dir / "key.pem.bak"
    key = slot_dir / "key.pem"
    if bak.exists():
        key_bytes = bak.read_bytes()
    elif key.exists():
        key_bytes = key.read_bytes()
    else:
        raise HTTPException(404, "Kein lokaler Schlüssel für diesen Slot vorhanden")

    # Encrypt on-the-fly if the file is plaintext PEM and a password is configured
    pw = settings_store.get("SMIME_KEY_PASSWORD") or _cfg.SMIME_KEY_PASSWORD or ""
    if pw and b"ENCRYPTED" not in key_bytes:
        try:
            from cryptography.hazmat.primitives.serialization import (
                load_pem_private_key, Encoding, PrivateFormat, BestAvailableEncryption
            )
            loaded = load_pem_private_key(key_bytes, password=None)
            key_bytes = loaded.private_bytes(
                Encoding.PEM, PrivateFormat.TraditionalOpenSSL,
                BestAvailableEncryption(pw.encode())
            )
        except Exception as exc:
            log.warning("Could not encrypt backup key for %s/%s on download: %s", email, slot_id, exc)

    safe_email = email.replace("@", "_at_").replace(".", "_")
    filename = f"smime-backup-{safe_email}-{slot_id[:8]}.pem"
    log.info("Backup key downloaded for %s slot %s by admin", email, slot_id)
    return _Resp(
        content=key_bytes,
        media_type="application/x-pem-file",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/smime/backup-all-keys")
async def api_smime_backup_all_keys(_: str = Depends(_require_admin)):
    """Download a ZIP of all local key files (key.pem + key.pem.bak), encrypted where needed."""
    import io
    import zipfile
    from pathlib import Path as _Path
    import config as _cfg
    from fastapi.responses import Response as _Resp

    if settings_store.get("KV_KEY_MODE") == "strict":
        raise HTTPException(403, "Backup-Download im Strict-Modus deaktiviert")

    smime_dir = _Path("/app/data/smime")
    pw = settings_store.get("SMIME_KEY_PASSWORD") or _cfg.SMIME_KEY_PASSWORD or ""

    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for user_dir in sorted(smime_dir.iterdir()):
            if not user_dir.is_dir() or user_dir.name == "recipients":
                continue
            certs_dir = user_dir / "certs"
            if not certs_dir.exists():
                continue
            for slot_dir in sorted(certs_dir.iterdir()):
                if not slot_dir.is_dir():
                    continue
                for fname in ("key.pem", "key.pem.bak"):
                    key_path = slot_dir / fname
                    if not key_path.exists():
                        continue
                    key_bytes = key_path.read_bytes()
                    if pw and b"ENCRYPTED" not in key_bytes:
                        try:
                            from cryptography.hazmat.primitives.serialization import (
                                load_pem_private_key, Encoding, PrivateFormat, BestAvailableEncryption,
                            )
                            loaded = load_pem_private_key(key_bytes, password=None)
                            key_bytes = loaded.private_bytes(
                                Encoding.PEM, PrivateFormat.TraditionalOpenSSL,
                                BestAvailableEncryption(pw.encode()),
                            )
                        except Exception as exc:
                            log.warning("backup-all: could not encrypt %s: %s", key_path, exc)
                    safe_email = user_dir.name.replace("@", "_at_")
                    zf.writestr(f"{safe_email}/{slot_dir.name}/{fname}", key_bytes)
                    count += 1

    if count == 0:
        raise HTTPException(404, "Keine lokalen Schlüsseldateien vorhanden")

    buf.seek(0)
    log.info("Bulk key backup downloaded by admin (%d files)", count)
    return _Resp(
        content=buf.read(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="smime-key-backup.zip"'},
    )


@app.post("/api/smime/migrate-all-to-keyvault")
async def api_smime_migrate_all(request: Request, _: str = Depends(_require_admin)):
    """Migrate all local key.pem files to Azure Key Vault (creates key.pem.bak in fallback mode)."""
    import smime_store
    import keyvault as _kv
    from pathlib import Path as _Path

    if not _kv.is_configured():
        raise HTTPException(400, "Azure Key Vault ist nicht konfiguriert")

    smime_dir = _Path("/app/data/smime")
    results = []
    for user_dir in sorted(smime_dir.iterdir()):
        if not user_dir.is_dir() or user_dir.name == "recipients":
            continue
        certs_dir = user_dir / "certs"
        if not certs_dir.exists():
            continue
        email = user_dir.name
        for slot_dir in sorted(certs_dir.iterdir()):
            if not slot_dir.is_dir():
                continue
            if not (slot_dir / "key.pem").exists():
                continue
            slot_id = slot_dir.name
            result = await smime_store.migrate_key_to_keyvault(email, slot_id=slot_id)
            results.append({"email": email, "slot_id": slot_id, **result})
            log.info("bulk migrate: %s/%s → %s", email, slot_id, "ok" if result["ok"] else result.get("error"))

    ok_count = sum(1 for r in results if r["ok"])
    return JSONResponse({"ok": True, "migrated": ok_count, "total": len(results), "results": results})


@app.get("/api/smime/keyvault/status")
async def api_keyvault_status(_: str = Depends(_require_admin)):
    """Return per-mailbox Key Vault key presence status."""
    import keyvault
    import smime_store
    if not keyvault.is_configured():
        return JSONResponse({"configured": False, "keys": {}})
    import mailbox_match
    config_map: dict = settings_store.get("MAILBOX_CONFIG") or {}
    cert_emails = {c["email"] for c in smime_store.list_certs()}
    all_emails = sorted(set(mailbox_match.configured_addresses(config_map)) | cert_emails)
    keys: dict[str, bool] = {}
    for em in all_emails:
        keys[em] = await keyvault.key_exists(em)
    return JSONResponse({"configured": True, "vault_url": keyvault.vault_url(), "keys": keys})


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

_EXPORT_EXCLUDE = {"ADMIN_PASSWORD_HASH", "CLIENT_SECRET", "RELAY_PASSWORD", "SECTIGO_PASSWORD",
                   "HUB_API_KEY", "_SCHEMA_VERSION"}


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
async def api_acme_capture_get(user: str = Depends(_check_auth)):
    """Return captured MIME payloads from the observatory."""
    import mime_observatory as _obs
    return JSONResponse({"captures": _obs.get_captures()})


@app.delete("/api/test/acme-capture")
async def api_acme_capture_clear(user: str = Depends(_check_auth)):
    import mime_observatory as _obs
    _obs.clear()
    return JSONResponse({"ok": True})


@app.post("/api/test/send-graph-acme")
async def api_send_graph_acme(request: Request, user: str = Depends(_check_auth)):
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
async def api_test_mail_processor_options(user: str = Depends(_check_auth)):
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
async def api_test_mail_processor(request: Request, user: str = Depends(_check_auth)):
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


# ── Remote Domain: castle.cloud ───────────────────────────────────────────────

@app.get("/api/setup/remote-domain-castle")
async def api_remote_domain_get(user: str = Depends(_check_auth)):
    import setup_wizard as _sw
    result = await asyncio.get_event_loop().run_in_executor(
        None, _sw.get_remote_domain_castle
    )
    return JSONResponse(result)


@app.post("/api/setup/remote-domain-castle")
async def api_remote_domain_configure(user: str = Depends(_check_auth)):
    import setup_wizard as _sw
    result = await asyncio.get_event_loop().run_in_executor(
        None, _sw.configure_remote_domain_castle
    )
    log.info("Remote Domain castle.cloud configured by %s: %s", user, result.get("ok"))
    return JSONResponse(result)


@app.delete("/api/setup/remote-domain-castle")
async def api_remote_domain_remove(user: str = Depends(_check_auth)):
    import setup_wizard as _sw
    result = await asyncio.get_event_loop().run_in_executor(
        None, _sw.remove_remote_domain_castle
    )
    log.info("Remote Domain castle.cloud removed by %s: %s", user, result)
    return JSONResponse(result)


# ── ACME Reply Method ─────────────────────────────────────────────────────────

@app.get("/api/acme/reply-method")
async def api_acme_reply_method_get(user: str = Depends(_check_auth)):
    method = (settings_store.get("ACME_REPLY_METHOD") or "auto").strip().lower()
    return JSONResponse({"ok": True, "method": method})


@app.post("/api/acme/reply-method")
async def api_acme_reply_method_set(request: Request, user: str = Depends(_check_auth)):
    data = await request.json()
    method = (data.get("method") or "auto").strip().lower()
    if method not in ("auto", "graph", "direct_smtp"):
        return JSONResponse({"ok": False, "error": "method must be 'auto', 'graph' or 'direct_smtp'"}, status_code=400)
    settings_store.update({"ACME_REPLY_METHOD": method})
    log.info("ACME reply method set to '%s' by %s", method, user)
    return JSONResponse({"ok": True, "method": method})


@app.get("/api/acme/http-proxy")
async def api_acme_http_proxy_get(user: str = Depends(_check_auth)):
    proxy = settings_store.get("ACME_HTTP_PROXY") or ""
    return JSONResponse({"ok": True, "proxy": proxy})


@app.post("/api/acme/http-proxy")
async def api_acme_http_proxy_set(request: Request, user: str = Depends(_check_auth)):
    data = await request.json()
    proxy = (data.get("proxy") or "").strip()
    if proxy and not (proxy.startswith("http://") or proxy.startswith("https://") or proxy.startswith("socks5://")):
        return JSONResponse({"ok": False, "error": "proxy muss mit http://, https:// oder socks5:// beginnen"}, status_code=400)
    settings_store.update({"ACME_HTTP_PROXY": proxy})
    log.info("ACME HTTP proxy %s by %s", "cleared" if not proxy else "set", user)
    return JSONResponse({"ok": True, "proxy": proxy})


@app.post("/api/acme/http-proxy/test")
async def api_acme_http_proxy_test(request: Request, user: str = Depends(_check_auth)):
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

_SECTIGO_KEYS = ["SECTIGO_MODE", "CERT_PROVIDER", "SECTIGO_API_BASE", "SECTIGO_LOGIN", "SECTIGO_PASSWORD",
                 "SECTIGO_CUSTOMER_URI", "SECTIGO_ORG_ID", "SECTIGO_CERT_TYPE", "SECTIGO_TERM"]


@app.get("/api/sectigo/config")
async def api_sectigo_config_get(user: str = Depends(_check_auth)):
    cfg = {k: settings_store.get(k) or "" for k in _SECTIGO_KEYS}
    # Never return the password itself — only whether one is set.
    cfg["SECTIGO_PASSWORD"] = ""
    cfg["password_set"] = bool(settings_store.get("SECTIGO_PASSWORD"))
    return JSONResponse({"ok": True, "config": cfg})


@app.post("/api/sectigo/config")
async def api_sectigo_config_set(request: Request, user: str = Depends(_check_auth)):
    data = await request.json()
    updates = {}
    for k in _SECTIGO_KEYS:
        if k not in data:
            continue
        val = data.get(k)
        # Keep existing password if the field was left blank (avoid clobbering on save).
        if k == "SECTIGO_PASSWORD" and not (val or "").strip():
            continue
        updates[k] = (val or "").strip()
    if updates:
        settings_store.update(updates)
    log.info("Sectigo config updated by %s (%d fields)", user, len(updates))
    return JSONResponse({"ok": True})


@app.post("/api/sectigo/config/test")
async def api_sectigo_config_test(user: str = Depends(_check_auth)):
    """Lightweight connectivity/auth check against the SCM API (organization list)."""
    import httpx as _httpx
    base = (settings_store.get("SECTIGO_API_BASE") or "https://cert-manager.com/api").strip().rstrip("/")
    login = (settings_store.get("SECTIGO_LOGIN") or "").strip()
    password = settings_store.get("SECTIGO_PASSWORD") or ""
    customer = (settings_store.get("SECTIGO_CUSTOMER_URI") or "").strip()
    if not (login and password and customer):
        return JSONResponse({"ok": False, "message": "Login, Passwort und Customer-URI müssen gesetzt sein."})
    headers = {"login": login, "password": password, "customerUri": customer,
               "Content-Type": "application/json;charset=utf-8"}
    try:
        # /organization/v1 is a common read-only endpoint that validates auth.
        async with _httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{base}/organization/v1", headers=headers)
        if r.status_code == 200:
            return JSONResponse({"ok": True, "message": "SCM-Authentifizierung erfolgreich (Organisationen abrufbar)."})
        if r.status_code in (401, 403):
            return JSONResponse({"ok": False, "message": f"Authentifizierung abgelehnt (HTTP {r.status_code}) — Login/Passwort/Customer-URI prüfen."})
        return JSONResponse({"ok": False, "message": f"Unerwarteter Status {r.status_code}: {r.text[:200]}"})
    except Exception as exc:
        return JSONResponse({"ok": False, "message": f"Verbindung fehlgeschlagen: {exc}"})


# ── ACME Account Reset ────────────────────────────────────────────────────────

@app.get("/api/acme/account-users")
async def api_acme_account_users(user: str = Depends(_check_auth)):
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
async def api_acme_account_reset(request: Request, user: str = Depends(_check_auth)):
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
async def api_cert_exo_ps_info(user: str = Depends(_check_auth)):
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
async def api_cert_exo_ps_export(user: str = Depends(_check_auth)):
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


# ── App-Pool API ──────────────────────────────────────────────────────────────

@app.get("/api/setup/app-pool/status")
async def api_app_pool_status(user: str = Depends(_check_auth)):
    import graph_client as _gc
    pool = _gc.get_pool_status()
    raw = settings_store.get("APP_POOL") or []
    return {"pool": pool, "count": len(pool), "configured": len(raw)}


@app.post("/api/setup/app-pool/add")
async def api_app_pool_add(request: Request, user: str = Depends(_require_admin)):
    """Create a new pool app via Bootstrap PKCE token and append to APP_POOL."""
    data = await request.json()
    token = (data.get("token") or "").strip()
    if not token:
        raise HTTPException(400, "PKCE-Token fehlt")
    import setup_wizard as _sw
    import graph_client as _gc
    current_pool: list[dict] = list(settings_store.get("APP_POOL") or [])
    # Primary app counts as index 1, pool starts at 2
    index = len(current_pool) + 2
    try:
        entry = await _sw.create_pool_app(token, index)
    except Exception as exc:
        raise HTTPException(500, f"App-Erstellung fehlgeschlagen: {exc}")
    current_pool.append(entry)
    settings_store.update({"APP_POOL": current_pool})
    _gc.reset_msal_app()
    log.info("App pool extended to %d entries by %s", len(current_pool) + 1, user)
    return {"ok": True, "label": entry["label"], "client_id": entry["client_id"], "pool_size": len(current_pool) + 1}


@app.post("/api/setup/app-pool/add-from-url")
async def api_app_pool_add_from_url(request: Request, user: str = Depends(_require_admin)):
    """Accept callback URL from PKCE flow, exchange code, create pool app."""
    data = await request.json()
    pasted = (data.get("url") or "").strip()
    if not pasted:
        raise HTTPException(400, "URL fehlt")
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
        raise HTTPException(400, "URL enthält keinen Code oder State.")
    session = pkce_mod.pop_session(state)
    if not session:
        raise HTTPException(400, "PKCE-Session abgelaufen — bitte erneut auf 'Anmelden' klicken.")
    try:
        token_resp = await pkce_mod.exchange_code(code, session["verifier"], session["redirect_uri"])
        access_token = token_resp["access_token"]
    except Exception as exc:
        raise HTTPException(500, f"Token-Austausch fehlgeschlagen: {exc}")
    import setup_wizard as _sw
    import graph_client as _gc
    current_pool: list[dict] = list(settings_store.get("APP_POOL") or [])
    index = len(current_pool) + 2
    try:
        entry = await _sw.create_pool_app(access_token, index)
    except Exception as exc:
        raise HTTPException(500, f"App-Erstellung fehlgeschlagen: {exc}")
    current_pool.append(entry)
    settings_store.update({"APP_POOL": current_pool})
    _gc.reset_msal_app()
    log.info("App pool extended to %d entries (via URL paste) by %s", len(current_pool) + 1, user)
    return {"ok": True, "label": entry["label"], "client_id": entry["client_id"], "pool_size": len(current_pool) + 1}


# ── Audit log API ─────────────────────────────────────────────────────────────

@app.get("/api/audit/events")
async def api_audit_events(
    request: Request,
    _user: str = Depends(_check_auth),
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
async def api_system_info(user: str = Depends(_check_auth)):
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
async def api_mail_hourly(user: str = Depends(_check_auth)):
    """Stündliche Mail-Statistik für heute aus mail_audit.db."""
    import mail_audit as _audit_mod
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _audit_mod.get_mail_hourly(today)


@app.get("/api/system/log-tail")
async def api_log_tail(n: int = 150, user: str = Depends(_check_auth)):
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
async def api_update_whats_new(from_version: str, to_version: str, user: str = Depends(_check_auth)):
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

    def _pv(v: str) -> tuple:
        try:
            return tuple(int(x) for x in v.lstrip("v").split("."))
        except Exception:
            return (0,)

    from_v, to_v = _pv(from_version), _pv(to_version)
    entries: list[dict] = []
    cur_lines: list[str] = []
    cur_ver: tuple = (0,)

    def _flush():
        if cur_lines and from_v < cur_ver <= to_v:
            header = cur_lines[0]
            body = "\n".join(cur_lines[1:]).strip()
            entries.append({"header": header, "body": body})

    for line in text.splitlines():
        if line.startswith("## v"):
            _flush()
            m = re.match(r"## v([\d.]+)", line)
            cur_ver = _pv(m.group(1)) if m else (0,)
            cur_lines = [line]
        elif cur_lines:
            cur_lines.append(line)
    _flush()

    return JSONResponse({"entries": entries})


@app.get("/api/system/changelog")
async def api_changelog(n: int = 10, user: str = Depends(_check_auth)):
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


@app.get("/api/setup/app-pool/history")
async def api_pool_history(days: int = 7, user: str = Depends(_check_auth)):
    """Tägliche Graph-API-Aufrufhistorie pro App aus mail_audit.db."""
    import mail_audit as _audit_mod
    pool = graph_client.get_pool_status()
    return {
        "pool": [
            {
                "client_id": p["client_id"],
                "label": p["label"],
                "days": _audit_mod.get_graph_calls_range(p["client_id"], days),
            }
            for p in pool
        ]
    }


@app.get("/api/setup/app-pool/day")
async def api_pool_day(app_id: str, date: str, user: str = Depends(_check_auth)):
    """24h-Stundendaten für eine App an einem bestimmten Tag."""
    import mail_audit as _audit_mod
    hours = _audit_mod.get_graph_calls_hours(app_id, date)
    return {"app_id": app_id, "date": date, "hours": hours}


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
async def api_support_upload(user: str = Depends(_require_admin)):
    """Support-Bundle (Logs, Settings, Audit) an den Provider-Hub hochladen."""
    import hub_client
    result = await hub_client.upload_bundle(list(_LOG_BUFFER))
    return JSONResponse(result)


# ── Provider Hub (sig-provider) client ────────────────────────────────────────

@app.get("/api/hub/config")
async def api_hub_config_get(user: str = Depends(_require_admin)):
    import hub_client
    return JSONResponse({
        "ok": True,
        "base_url": settings_store.get("HUB_BASE_URL") or "",
        "email": settings_store.get("HUB_CUSTOMER_EMAIL") or "",
        "name": settings_store.get("HUB_CUSTOMER_NAME") or "",
        "registered": hub_client.is_registered(),
        "claim_pending": bool((settings_store.get("HUB_CLAIM_TOKEN") or "").strip()
                              and not (settings_store.get("HUB_API_KEY") or "").strip()),
        "gateway_id": settings_store.get("GATEWAY_ID") or "",
    })


@app.post("/api/hub/config")
async def api_hub_config_set(request: Request, user: str = Depends(_require_admin)):
    data = await request.json()
    updates = {
        "HUB_BASE_URL": (data.get("base_url") or "").strip().rstrip("/"),
        "HUB_CUSTOMER_EMAIL": (data.get("email") or "").strip().lower(),
        "HUB_CUSTOMER_NAME": (data.get("name") or "").strip(),
    }
    if updates["HUB_BASE_URL"] and not updates["HUB_BASE_URL"].startswith(("http://", "https://")):
        return JSONResponse({"ok": False, "error": "Hub-Adresse muss mit http(s):// beginnen."}, status_code=400)
    settings_store.update(updates)
    return JSONResponse({"ok": True})


@app.post("/api/hub/register")
async def api_hub_register(user: str = Depends(_require_admin)):
    import hub_client
    return JSONResponse(await hub_client.register())


@app.post("/api/hub/claim")
async def api_hub_claim(user: str = Depends(_require_admin)):
    """Poll the hub for the issued API key after email confirmation (self-service)."""
    import hub_client
    return JSONResponse(await hub_client.poll_claim())


@app.post("/api/hub/cert/request-invoice")
async def api_hub_cert_request_invoice(user: str = Depends(_require_admin)):
    import hub_client
    return JSONResponse(await hub_client.cert_request_invoice())


@app.post("/api/hub/cert/billing")
async def api_hub_cert_billing(request: Request, user: str = Depends(_require_admin)):
    import hub_client
    data = await request.json()
    return JSONResponse(await hub_client.cert_submit_billing(
        (data.get("billing_company") or "").strip(), (data.get("billing_address") or "").strip(),
        (data.get("billing_vat") or "").strip(), (data.get("billing_contact") or "").strip()))


@app.post("/api/hub/cert/domain/request")
async def api_hub_cert_domain_request(request: Request, user: str = Depends(_require_admin)):
    import hub_client
    data = await request.json()
    return JSONResponse(await hub_client.cert_domain_request((data.get("domain") or "").strip()))


@app.post("/api/hub/cert/domain/verify")
async def api_hub_cert_domain_verify(request: Request, user: str = Depends(_require_admin)):
    import hub_client
    data = await request.json()
    return JSONResponse(await hub_client.cert_domain_verify((data.get("domain") or "").strip()))


@app.post("/api/hub/cert/opt-out")
async def api_hub_cert_opt_out(user: str = Depends(_require_admin)):
    import hub_client
    return JSONResponse(await hub_client.cert_opt_out())


@app.post("/api/hub/disconnect")
async def api_hub_disconnect(request: Request, user: str = Depends(_require_admin)):
    import hub_client
    ctype = request.headers.get("content-type", "")
    data = await request.json() if ctype.startswith("application/json") else {}
    return JSONResponse(await hub_client.disconnect(close_remote=bool(data.get("close_remote"))))


@app.post("/api/hub/api-key")
async def api_hub_set_key(request: Request, user: str = Depends(_require_admin)):
    """Store the API key the operator issued after approving this gateway."""
    data = await request.json()
    key = (data.get("api_key") or "").strip()
    settings_store.update({"HUB_API_KEY": key})
    log.info("Hub API key %s by %s", "cleared" if not key else "set", user)
    return JSONResponse({"ok": True})


@app.get("/api/hub/status")
async def api_hub_status(user: str = Depends(_require_admin)):
    import hub_client
    return JSONResponse(await hub_client.status())


# ── Provider Hub — CERT capability (same account/key as the support anbindung) ─
# Accepting the paid terms IS the request (no separate "beantragen" step) — the
# hub auto-enables the capability once terms are accepted + a balance is loaded.

@app.get("/api/hub/cert/terms")
async def api_hub_cert_terms(user: str = Depends(_require_admin)):
    import hub_client
    return JSONResponse(await hub_client.cert_terms())


@app.post("/api/hub/cert/accept-terms")
async def api_hub_cert_accept_terms(request: Request, user: str = Depends(_require_admin)):
    import hub_client
    data = await request.json()
    return JSONResponse(await hub_client.cert_accept_terms(version=str(data.get("version") or "1")))


@app.get("/api/hub/cert/eligibility")
async def api_hub_cert_eligibility(user: str = Depends(_require_admin)):
    import hub_client
    return JSONResponse(await hub_client.cert_eligibility())


@app.post("/api/hub/cert/topup")
async def api_hub_cert_topup(request: Request, user: str = Depends(_require_admin)):
    """Create a prepaid top-up Checkout session at the hub; return its URL."""
    import hub_client
    data = await request.json()
    amount_cents = data.get("amount_cents")
    if amount_cents is None and data.get("amount_eur") is not None:
        try:
            amount_cents = int(round(float(data["amount_eur"]) * 100))
        except (TypeError, ValueError):
            amount_cents = None
    if not amount_cents:
        raise HTTPException(400, "Betrag erforderlich.")
    return JSONResponse(await hub_client.cert_topup(int(amount_cents)))
