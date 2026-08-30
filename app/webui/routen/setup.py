"""Routen des Einrichtungsassistenten — Seite, Azure-Anbindung, Key Vault, App-Pool.

Siebtes Modul der Aufteilung von `app.py`. Muster siehe `addin.py`.

ZUM PFADFILTER
--------------
Aufgenommen ist `/setup` und alles unter `/api/setup/…` — 33 Adressen. Die
Anmelderouten (`/auth/…`) gehoeren NICHT dazu, obwohl der Assistent sie nutzt:
Sie tragen die Sitzung der gesamten Oberflaeche, nicht nur der Einrichtung.
Umgekehrt liegt `/api/setup/smime-rules` HIER und nicht im S/MIME-Modul — es
legt Transportregeln in Exchange an, gehoert also zur Einrichtung; dessen
Filter (`/smime`, `/api/smime…`) laesst es korrekt aus.

⚠️ `setup_wizard` IST HIER ZWEIERLEI
------------------------------------
`setup_wizard` heisst sowohl die Routenfunktion fuer `GET /setup` als auch das
Modul `app/setup_wizard.py`, das die PowerShell-Schritte ausfuehrt. Das geht
gut, weil das Modul ausschliesslich ALS LOKALER IMPORT in den einzelnen
Endpunkten geholt wird (`import setup_wizard` im Rumpf) und dort nur den
oertlichen Namen belegt. Ein Import auf Modulebene wuerde die Routenfunktion
ueberschreiben — je nach Reihenfolge lautlos. Also: lokal lassen.

ZUR ANMELDUNG
-------------
Alle Endpunkte verlangen `_require_admin`. Einzige Ausnahme ist die Seite
`GET /setup` selbst: Sie prueft Sitzung bzw. Basic-Auth IM RUMPF und ist
anonym erreichbar, solange ueberhaupt noch kein Anmeldeweg eingerichtet ist
(`_setup_requires_auth()`) — sonst waere ein frisch aufgesetztes Gateway nicht
einzurichten. `tests/test_wachen.py` fuehrt sie deshalb mit dieser Begruendung
in `ERLAUBT_OHNE_ANMELDUNG`.
"""
from __future__ import annotations

import asyncio
import secrets
import subprocess
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasicCredentials

import config
import graph_client
import pkce as pkce_mod
import settings_store

from webui.deps import (
    templates, log, security, _gateway_name, _require_admin,
    _check_password, _hash_password, _verify_password, _get_session_user,
)
from webui.hilfen import (
    _addin_base_url, _build_redirect_uri, _password_change_required,
)

router = APIRouter()

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


@router.get("/setup", response_class=HTMLResponse)
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

    # Maskierte Sicht: `s` landet unten im Vorlagen-Kontext. Die hier gelesenen
    # Schlüssel (TENANT_ID, CLIENT_ID, Setup-Marker) sind keine Geheimnisse.
    s = settings_store.public_view()
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
        # Kernkonfiguration steht = das Gateway ist arbeitsfähig. Wird nur benutzt,
        # um im Wizard auf den fehlenden Abschluss-Klick hinzuweisen (siehe Banner
        # in setup.html) — ohne diesen Klick leitet "/" dauerhaft hierher zurück.
        "core_config_done": bool(
            (config.TENANT_ID or s.get("TENANT_ID"))
            and (config.CLIENT_ID or s.get("CLIENT_ID"))
            and s.get("TENANT_DOMAIN")
            and (config.CLIENT_SECRET or s.get("CLIENT_SECRET"))
        ),
        "bootstrap_client_id": s.get("BOOTSTRAP_CLIENT_ID", ""),
        "bootstrap_redirect_uris": s.get("BOOTSTRAP_REDIRECT_URIS", []),
        "sso_redirect_uri": _build_redirect_uri(sso=True),
        "redirect_uri": _build_redirect_uri(),
        "webui_port": config.WEBUI_PORT,
        # Region/Ressourcengruppe der VM als Vorschlag für den Key-Vault-Schritt
        # — leer außerhalb Azure, dann greifen die statischen Vorgaben.
        "vm_location": __import__("azure_imds").location(),
        "vm_resource_group": __import__("azure_imds").resource_group(),
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


@router.post("/api/setup/auth-paste")
async def api_auth_paste(request: Request, user: str = Depends(_require_admin)):
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


# ── Routes: setup API endpoints ────────────────────────────────────────────────

@router.post("/api/setup/bootstrap-client")
async def api_setup_bootstrap_client(request: Request, user: str = Depends(_require_admin)):
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


@router.post("/api/setup/hostname")
async def api_setup_hostname(request: Request, user: str = Depends(_require_admin)):
    data = await request.json()
    hostname = (data.get("hostname") or "").strip()
    if not hostname:
        raise HTTPException(400, "hostname darf nicht leer sein")
    settings_store.update({"PUBLIC_HOSTNAME": hostname})
    log.info("Public hostname set to %s by %s", hostname, user)
    return JSONResponse({"ok": True})


@router.post("/api/setup/change-password")
async def api_change_password(request: Request, user: str = Depends(_require_admin)):
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


@router.post("/api/setup/exo-connector")
async def api_setup_exo_connector(request: Request, user: str = Depends(_require_admin)):
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


@router.post("/api/setup/gen-auth-cert")
async def api_gen_auth_cert(request: Request, user: str = Depends(_require_admin)):
    """Generate a self-signed auth cert, save PFX locally, return public cert PEM."""
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


@router.post("/api/setup/smime-rules")
async def api_setup_smime_rules(request: Request, user: str = Depends(_require_admin)):
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


@router.post("/api/setup/imap-access")
async def api_setup_imap_access(request: Request, user: str = Depends(_require_admin)):
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


@router.get("/api/setup/verify/connector")
async def api_verify_connector(_=Depends(_require_admin)):
    import setup_wizard
    reinject_mode = settings_store.get("REINJECT_MODE") or "smtp"
    smtp_mode = reinject_mode == "smtp"
    return setup_wizard.verify_connector(smtp_mode=smtp_mode)


@router.get("/api/setup/verify/imap")
async def api_verify_imap(_=Depends(_require_admin)):
    import setup_wizard
    return setup_wizard.verify_imap()


@router.get("/api/setup/verify/smime")
async def api_verify_smime(_=Depends(_require_admin)):
    import setup_wizard
    return setup_wizard.verify_smime_rules()


@router.get("/api/setup/verify/azure")
async def api_verify_azure(_=Depends(_require_admin)):
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


@router.get("/api/setup/verify/sso")
async def api_verify_sso(_=Depends(_require_admin)):
    """Kann sich jemand über Entra anmelden? Live bei Microsoft nachgefragt.

    ANLASS (24.08.2026): Die Einrichtungsseite behauptete „✓ … ist in Entra
    registriert — SSO-Login funktioniert." Geschlossen war das aus einem
    einzigen Vergleich gegen `BOOTSTRAP_REDIRECT_URIS` — also gegen die EIGENE
    Kopie, die beim letzten Schreibvorgang entstand. Ändert jemand die
    Registrierung in Azure, merkt das Gateway nichts und behauptet weiter, es
    funktioniere.

    ⚠️ Registriert ist nicht dasselbe wie funktioniert. Diese Prüfung nennt
    deshalb einzeln, was sie tatsächlich weiss:

      * die Rückadresse steht in der Registrierung — LIVE gelesen, nicht aus
        der eigenen Ablage,
      * es ist mindestens ein Entra-Konto zugelassen (ohne das kommt niemand
        durch, auch bei tadelloser Registrierung),
      * die Bootstrap-Anwendung existiert überhaupt noch.

    Was auch sie NICHT weiss: ob die Anmeldung durchläuft. Das zeigt erst ein
    Versuch — Zustimmungsrichtlinien, bedingten Zugriff und gesperrte Konten
    sieht man von hier aus nicht.
    """
    ergebnis: dict = {"ok": False, "schritte": []}

    def schritt(name: str, erfuellt: bool, hinweis: str = ""):
        ergebnis["schritte"].append({"name": name, "ok": erfuellt, "hinweis": hinweis})

    admins = settings_store.get("ADMIN_USERS") or []
    schritt("Mindestens ein Entra-Konto zugelassen", bool(admins),
            f"{len(admins)} eingetragen" if admins
            else "Ohne zugelassenes Konto scheitert die Anmeldung, auch wenn "
                 "alles andere stimmt.")

    client_id = (settings_store.get("BOOTSTRAP_CLIENT_ID") or "").strip()
    schritt("Anwendungskennung hinterlegt", bool(client_id),
            client_id or "Es ist keine Bootstrap-Anwendung eingetragen.")

    import aussenadresse
    basis = aussenadresse.konfiguriert()
    erwartet = f"{basis}/auth/callback" if basis else ""
    schritt("Aussenadresse gesetzt", bool(basis),
            erwartet or "Weder ADDIN_BASE_URL noch PUBLIC_HOSTNAME gesetzt — "
                        "die Rückadresse lässt sich nicht bilden.")

    if not (client_id and erwartet):
        return JSONResponse(ergebnis)

    token = graph_client._acquire_token()
    if not token:
        schritt("Registrierung bei Microsoft gelesen", False,
                "Keine Graph-Zugangsdaten — die Registrierung lässt sich nicht "
                "abfragen.")
        return JSONResponse(ergebnis)

    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://graph.microsoft.com/v1.0/applications"
                f"?$filter=appId eq '{client_id}'"
                "&$select=id,displayName,publicClient,web",
                headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        apps = resp.json().get("value", [])
    except Exception as exc:
        schritt("Registrierung bei Microsoft gelesen", False, str(exc)[:200])
        return JSONResponse(ergebnis)

    if not apps:
        schritt("Bootstrap-Anwendung vorhanden", False,
                f"Zu {client_id} gibt es in diesem Tenant keine Registrierung.")
        return JSONResponse(ergebnis)

    app_obj = apps[0]
    schritt("Bootstrap-Anwendung vorhanden", True, app_obj.get("displayName") or "")

    # Beide Stellen lesen: Eine Anwendung kann die Rückadresse als öffentlicher
    # Client ODER unter „web" führen — je nachdem, wie sie angelegt wurde. Nur
    # eine davon zu prüfen ergäbe einen Fehlalarm.
    registriert = list((app_obj.get("publicClient") or {}).get("redirectUris") or [])
    registriert += list((app_obj.get("web") or {}).get("redirectUris") or [])
    passt = erwartet in registriert
    schritt("Rückadresse in der Registrierung", passt,
            erwartet if passt else
            f"Gebraucht wird {erwartet}. Dort steht: "
            + (", ".join(registriert) or "nichts"))

    # Den eigenen Stand nachziehen, wenn er abweicht — genau diese Abweichung
    # war der Anlass für die Prüfung.
    if sorted(registriert) != sorted(settings_store.get("BOOTSTRAP_REDIRECT_URIS") or []):
        settings_store.update({"BOOTSTRAP_REDIRECT_URIS": registriert})
        ergebnis["nachgezogen"] = True

    ergebnis["ok"] = all(s["ok"] for s in ergebnis["schritte"])
    return JSONResponse(ergebnis)


@router.post("/api/setup/mark-complete")
async def api_setup_complete(request: Request, user: str = Depends(_require_admin)):
    settings_store.update({"SETUP_COMPLETE": True})
    log.info("Setup marked complete by %s", user)
    return JSONResponse({"ok": True})


@router.post("/api/setup/test-graph")
async def api_test_graph(request: Request, user: str = Depends(_require_admin)):
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


@router.post("/api/setup/notification-dg")
async def api_setup_notification_dg(request: Request, user: str = Depends(_require_admin)):
    """Create/update notification Distribution Group in EXO and save recipients."""
    import setup_wizard
    data = await request.json()
    recipients = [r.strip().lower() for r in (data.get("recipients") or []) if r.strip()]
    extern = bool(data.get("accept_external"))
    result = await asyncio.get_event_loop().run_in_executor(
        None, setup_wizard.run_notification_dg_update, recipients, extern
    )
    if result.get("ok"):
        patch: dict = {"NOTIFICATION_RECIPIENTS": recipients,
                       "NOTIFICATION_DG_ACCEPT_EXTERNAL": extern}
        if result.get("email"):
            patch["NOTIFICATION_DG_EMAIL"] = result["email"]
        settings_store.update(patch)
        log.info("Notification DG updated by %s: %d members, DG=%s, extern=%s",
                 user, len(recipients), result.get("email"), extern)
    return JSONResponse({
        "ok": result.get("ok", False),
        "email": result.get("email", ""),
        "output": result.get("output", ""),
    })


# ── Azure Key Vault API endpoints ─────────────────────────────────────────────

@router.get("/api/setup/keyvault/test")
async def api_keyvault_test(url: str = "", _: str = Depends(_require_admin)):
    """Test Key Vault connectivity. ?url=https://... to test a specific URL."""
    import keyvault
    ok, msg = await keyvault.test_connection(url or None)
    return JSONResponse({"ok": ok, "message": msg})


@router.get("/api/setup/keyvault/arm-auth-url")
async def api_keyvault_arm_auth_url(request: Request, _: str = Depends(_require_admin)):
    """Return an auth URL for the user to grant delegated ARM access."""
    redirect_uri = _build_redirect_uri(sso=True)
    _state, auth_url = pkce_mod.create_session(
        redirect_uri, scopes=pkce_mod.ARM_SCOPES, flow="arm"
    )
    return JSONResponse({"auth_url": auth_url})


@router.post("/api/setup/keyvault/arm-paste")
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


@router.get("/api/setup/keyvault/subscriptions")
async def api_keyvault_subscriptions(request: Request, _: str = Depends(_require_admin)):
    """List Azure subscriptions — uses delegated user token if available, else app SP."""
    import keyvault
    upn = _get_session_user(request) or ""
    user_tok = keyvault.get_user_arm_token(upn) if upn else None
    ok, msg, subs = await keyvault.list_subscriptions(arm_token=user_tok)
    return JSONResponse({"ok": ok, "message": msg, "subscriptions": subs,
                         "delegated": bool(user_tok)})


@router.get("/api/setup/keyvault/resource-groups")
async def api_keyvault_resource_groups(request: Request, subscription_id: str,
                                        _: str = Depends(_require_admin)):
    """List resource groups — uses delegated user token if available, else app SP."""
    import keyvault
    upn = _get_session_user(request) or ""
    user_tok = keyvault.get_user_arm_token(upn) if upn else None
    ok, msg, rgs = await keyvault.list_resource_groups(subscription_id, arm_token=user_tok)
    return JSONResponse({"ok": ok, "message": msg, "resource_groups": rgs})


@router.get("/api/setup/keyvault/vaults")
async def api_keyvault_vaults(request: Request, subscription_id: str,
                               _: str = Depends(_require_admin)):
    """List Key Vaults in subscription — uses delegated user token if available."""
    import keyvault
    upn = _get_session_user(request) or ""
    user_tok = keyvault.get_user_arm_token(upn) if upn else None
    ok, msg, vaults = await keyvault.list_vaults(subscription_id, arm_token=user_tok)
    return JSONResponse({"ok": ok, "message": msg, "vaults": vaults})


@router.post("/api/setup/keyvault/create")
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


@router.post("/api/setup/keyvault/assign-role")
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


@router.post("/api/setup/keyvault/save")
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


# ── Remote Domain: castle.cloud ───────────────────────────────────────────────

@router.get("/api/setup/remote-domain-castle")
async def api_remote_domain_get(user: str = Depends(_require_admin)):
    import setup_wizard as _sw
    result = await asyncio.get_event_loop().run_in_executor(
        None, _sw.get_remote_domain_castle
    )
    return JSONResponse(result)


@router.post("/api/setup/remote-domain-castle")
async def api_remote_domain_configure(user: str = Depends(_require_admin)):
    import setup_wizard as _sw
    result = await asyncio.get_event_loop().run_in_executor(
        None, _sw.configure_remote_domain_castle
    )
    log.info("Remote Domain castle.cloud configured by %s: %s", user, result.get("ok"))
    return JSONResponse(result)


@router.delete("/api/setup/remote-domain-castle")
async def api_remote_domain_remove(user: str = Depends(_require_admin)):
    import setup_wizard as _sw
    result = await asyncio.get_event_loop().run_in_executor(
        None, _sw.remove_remote_domain_castle
    )
    log.info("Remote Domain castle.cloud removed by %s: %s", user, result)
    return JSONResponse(result)


# ── App-Pool API ──────────────────────────────────────────────────────────────

@router.get("/api/setup/app-pool/status")
async def api_app_pool_status(user: str = Depends(_require_admin)):
    import graph_client as _gc
    pool = _gc.get_pool_status()
    raw = settings_store.get("APP_POOL") or []
    return {"pool": pool, "count": len(pool), "configured": len(raw)}


@router.post("/api/setup/app-pool/add")
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


@router.post("/api/setup/app-pool/add-from-url")
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


@router.get("/api/setup/app-pool/history")
async def api_pool_history(days: int = 7, user: str = Depends(_require_admin)):
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


@router.get("/api/setup/app-pool/day")
async def api_pool_day(app_id: str, date: str, user: str = Depends(_require_admin)):
    """24h-Stundendaten für eine App an einem bestimmten Tag."""
    import mail_audit as _audit_mod
    hours = _audit_mod.get_graph_calls_hours(app_id, date)
    return {"app_id": app_id, "date": date, "hours": hours}

