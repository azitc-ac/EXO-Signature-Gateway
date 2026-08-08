"""Routen des Outlook-Add-ins.

Erstes Modul der Aufteilung von `app.py` (5.655 Zeilen, 232 Routen). Bewusst
dieses zuerst: Die acht Routen hängen an zwei eigenen Hilfsfunktionen und sonst
nur am gemeinsamen Fundament — ein Schnitt, bei dem sich die Tragfähigkeit des
Musters zeigt, ohne dass viel schiefgehen kann.

MUSTER FÜR DIE WEITEREN MODULE
------------------------------
    router = APIRouter()          hier, NICHT `app`
    from webui.deps import …      nur das Fundament, nie `webui.app`
    app.py:  app.include_router(addin.router)

Die Routen sind ABSICHTLICH ohne Anmeldung: Signaturen sind nicht vertraulich,
und das Add-in ruft sie aus Outlook heraus auf. `/api/addin/*` prüft die
Sitzung dagegen sehr wohl — dort geht es um die Zuordnung zum Postfach.
"""
from __future__ import annotations

import json as _json_mod
import uuid as _uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import config
import graph_client
import mail_processor
import mailbox_match
import settings_store
import pkce as pkce_mod
import signature_engine
import sso as sso_mod

from webui.deps import (
    templates, log, _gateway_name, _check_auth, _require_admin,
)

router = APIRouter()


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



# ── Outlook Add-in (no auth — signatures are not sensitive, gateway is internal) ──

_NO_STORE = "no-store, no-cache, must-revalidate, max-age=0"


@router.get("/addin/compose", response_class=HTMLResponse)
async def addin_compose(request: Request):
    # no-store: the taskpane is loaded in Outlook's WebView, which otherwise
    # caches it aggressively — an updated add-in never reaches the user until
    # they clear the Office web cache. Always serve fresh.
    resp = templates.TemplateResponse(request=request, name="addin_compose.html", context={})
    resp.headers["Cache-Control"] = _NO_STORE
    return resp


@router.get("/addin/auth-complete", response_class=HTMLResponse)
async def addin_auth_complete(request: Request):
    """Landing page for the add-in login dialog.

    The dialog runs the full OIDC flow; the callback set the session cookie on
    THIS (dialog) context. HttpOnly cookies aren't readable via JS but ARE sent
    to the server, so we read it here and hand the signed token back to the
    taskpane via Office's messageParent — the taskpane then sends it as the
    X-Addin-Session header (cookie sharing between dialog and taskpane is not
    guaranteed). If opened outside a dialog, messageParent throws → show a hint."""
    token = request.cookies.get(sso_mod.SESSION_COOKIE) or ""
    payload = _json_mod.dumps({"status": "ok" if token else "no_session", "token": token})
    html = (
        "<!DOCTYPE html><html lang=\"de\"><head><meta charset=\"UTF-8\">"
        "<title>Anmeldung…</title>"
        "<script src=\"https://appsforoffice.microsoft.com/lib/1.1/hosted/office.js\"></script>"
        "</head><body style=\"font-family:'Segoe UI',Arial,sans-serif;padding:20px;color:#1e293b;font-size:13px\">"
        "<p id=\"msg\">Anmeldung abgeschlossen – Fenster schließt…</p>"
        "<script>"
        "Office.onReady(function(){"
        "  try { Office.context.ui.messageParent(JSON.stringify(" + payload + ")); }"
        "  catch(e){ document.getElementById('msg').textContent = "
        "    'Anmeldung abgeschlossen. Bitte dieses Fenster schließen und im Add-in „Erneut versuchen“ klicken.'; }"
        "});"
        "</script></body></html>"
    )
    # no-store: the page carries the session token — never cache it.
    return HTMLResponse(content=html, headers={"Cache-Control": _NO_STORE})


@router.get("/addin/manifest.xml")
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
        <!-- Im Menuband steht der Gruppenname UNTEN, die Knopfbeschriftung
             direkt unter dem Symbol. Beide hiessen "Signatur" — damit stand
             das Wort zweimal untereinander und nannte den Urheber nirgends. -->
        <bt:String id="groupLabel" DefaultValue="Signatur"/>
        <bt:String id="btnLabel"   DefaultValue="EXO Signatur"/>
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


@router.get("/addin/icon/{size_str}")
async def addin_icon(size_str: str):
    """Serve an envelope icon as PNG (no PIL required)."""
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
    w = max(2, round(2.5 * s))  # bold stroke so the shape reads at 16px

    # Envelope — a bold white outline on the blue tile. A thin pen glyph
    # disappears at 16/32px (reads as a plain blue box); a chunky envelope
    # outline stays legible at ribbon sizes.
    top, bot = round(9 * s), round(24 * s)
    left, right = round(5 * s), round(27 * s)
    midR, midC = round(17 * s), round(16 * s)
    bline(top, left,  top,  right, w)   # top edge
    bline(bot, left,  bot,  right, w)   # bottom edge
    bline(top, left,  bot,  left,  w)   # left edge
    bline(top, right, bot,  right, w)   # right edge
    bline(top, left,  midR, midC,  w)   # flap: left corner → centre
    bline(top, right, midR, midC,  w)   # flap: right corner → centre

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


@router.get("/addin/function", response_class=HTMLResponse)
async def addin_function(request: Request):
    """Minimal function-file page required by Office Add-in DesktopFormFactor."""
    html = (
        "<!DOCTYPE html><html><head>"
        '<meta charset="UTF-8">'
        '<script src="https://appsforoffice.microsoft.com/lib/1.1/hosted/office.js"></script>'
        "<script>Office.onReady(function(){});</script>"
        "</head><body></body></html>"
    )
    return HTMLResponse(content=html, headers={"Cache-Control": _NO_STORE})


@router.get("/api/addin/signature")
async def api_addin_signature(email: str, template: str = "", user: str = Depends(_check_auth)):
    """Return the rendered (marked) signature HTML for the add-in taskpane."""
    email = (email or "").strip().lower()
    if not email:
        return JSONResponse({"marked_html": "", "preview_html": ""})

    import mail_processor
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

    # Outlook Classic compose getAsync strips: HTML comments, custom class attrs,
    # <a name> anchors. id attrs on block elements survive in compose mode (only
    # x_-prefixed in read/quote mode). Template has no inner divs → _matchCloseDiv
    # is reliable. Comment/class markers kept as OWA fallback.
    marked = (
        mail_processor._SIG_MARKER_START
        + f'<div id="exo-sig-s" class="{mail_processor._SIG_CLASS}">'
        + sig_html
        + "</div>"
        + mail_processor._SIG_MARKER_END
    )
    return JSONResponse({"marked_html": marked, "preview_html": sig_html})


@router.get("/api/addin/templates")
async def api_addin_templates(email: str, user: str = Depends(_check_auth)):
    """Return list of templates available for this user in the add-in."""
    email = (email or "").strip().lower()
    import mail_processor
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


@router.get("/api/addin/update-redirect-uri")
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


