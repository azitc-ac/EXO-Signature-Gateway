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

# ⚠️ Die selbsterzeugte Schnittstellenbeschreibung ist ABGESCHALTET.
#
# FastAPI liefert /docs, /redoc und /openapi.json standardmässig aus — ohne
# Anmeldung. Am 19.08.2026 lagen dort 229 Endpunkte samt Parametern offen; das
# war nie entschieden, sondern die Voreinstellung. Für ein Gateway, das im
# Internet steht, ist die vollständige Landkarte der eigenen Angriffsfläche
# keine sinnvolle Beigabe — zumal die Oberfläche sie nirgends benutzt.
#
# Wer sie zur Entwicklung braucht: hier vorübergehend wieder eintragen.
app = FastAPI(title="EXO Signature Gateway", lifespan=_lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

# Gemeinsames Fundament — dieselben Objekte, die auch die Routenmodule nutzen.
# Weitergereicht statt neu definiert: Die Tests haengen sich ueber
# `app.dependency_overrides[_check_auth]` ein, und der Schluessel ist das
# Funktionsobjekt. Zwei Kopien, und die Umgehung passte zu keinem Depends mehr.
from webui.deps import (                                    # noqa: E402
    log, templates, _STATIC_DIR, _TEMPLATE_DIR, _gateway_name,
    _NotAuthenticated, _check_password,
    _get_session_user, _get_session_role, _check_auth, _require_admin,
    _LOG_BUFFER, _LOG_SUBSCRIBERS, _LOG_SUBSCRIBERS_LOCK,
    _make_log_token, _check_log_token,
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
from webui.routen import betrieb as _routen_betrieb
from webui.routen import aktualisierung as _routen_aktualisierung
from webui.routen import anmeldung as _routen_anmeldung
from webui.routen import settings as _routen_settings
from webui.routen import vorlagen as _routen_vorlagen        # noqa: E402
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
                _routen_portal, _routen_settings, _routen_setup, _routen_smime,
                _routen_vorlagen, _routen_betrieb, _routen_aktualisierung,
                _routen_anmeldung]

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










# ── Routes: public (no auth) ───────────────────────────────────────────────────





# ── Routes: PKCE auth flow ─────────────────────────────────────────────────────













# ── Routes: SSO login / logout ────────────────────────────────────────────────
























































# ── Routes: mailbox config ─────────────────────────────────────────────────────

























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
        # Damit eine leere Liste nicht als „es gibt keine Anbieter" gelesen
        # wird, wenn in Wahrheit der Abruf scheiterte — hub_catalog.zustand().
        "zustand": _hub_cat.zustand(),
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
async def api_test_mail(request: Request, user: str = Depends(_require_admin)):
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






# ── S/MIME Lifecycle: CA config + self-service ────────────────────────────────






















# ── Persistent log search ──────────────────────────────────────────────────────





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












































