"""Routen der Einstellungen — Seiten und Speichern.

Sechstes Modul der Aufteilung von `app.py`. Muster siehe `addin.py`.

⚠️ ZUM PFADFILTER: `/settings/smime` gehört HIERHER, nicht ins S/MIME-Modul.
Der Filter dort greift auf `/smime`, `/smime/…` und `/api/smime…` — die
Einstellungsseite für S/MIME faellt also korrekt heraus und liegt bei den
uebrigen Einstellungsseiten, wo sie hingehoert. Ein schlichtes `"smime" in
pfad` haette sie auseinandergerissen.

⚠️ `settings_store.public_view()`, NIEMALS `get_all()`
------------------------------------------------------
Die Seiten geben den Einstellungsstand an eine Vorlage. `public_view()`
maskiert dabei die als geheim deklarierten Schluessel (`SECRET_KEYS`);
`get_all()` gaebe sie im Klartext ins HTML. Geheimnisfelder bekommen im
Formular ausserdem `placeholder` statt `value` — kaeme die Maskierung
zurueck, wuerde sie das echte Kennwort ueberschreiben. Beides steht als
verbindliche Regel in `CLAUDE.md`.

ZUM SPEICHERN
-------------
`POST /settings` (`settings_save`) ist der Sammel-Endpunkt des Formulars,
`POST /api/settings/partial` der Weg fuer einzelne Felder aus dem laufenden
Betrieb. Beide verlangen die Verwaltungsrolle; `tests/test_wachen.py` fuehrt
`/settings` deshalb namentlich unter den besonders folgenreichen Adressen.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import graph_client
import settings_store

from webui.deps import (
    templates, log, _gateway_name, _check_auth, _require_admin,
)

router = APIRouter()


@router.get("/api/settings/template-policies")
async def api_get_template_policies(_=Depends(_require_admin)):
    return JSONResponse(settings_store.get("TEMPLATE_POLICIES") or {"sig": "default"})


@router.get("/api/settings/internal-groups")
async def api_get_internal_groups(_=Depends(_require_admin)):
    return JSONResponse(settings_store.get("INTERNAL_GROUPS") or {})


@router.post("/api/settings/internal-groups/save")
async def api_save_internal_groups(request: Request, _=Depends(_require_admin)):
    data = await request.json()
    groups = data.get("groups")
    if not isinstance(groups, dict):
        raise HTTPException(400, "groups must be a dict")
    settings_store.update({"INTERNAL_GROUPS": groups})
    return JSONResponse({"ok": True})


@router.get("/api/settings/custom-policies")
async def api_get_custom_policies(_=Depends(_require_admin)):
    return JSONResponse(settings_store.get("CUSTOM_POLICIES") or [])


@router.post("/api/settings/custom-policies/save")
async def api_save_custom_policies(request: Request, _=Depends(_require_admin)):
    data = await request.json()
    policies = data.get("policies")
    if not isinstance(policies, list):
        raise HTTPException(400, "policies must be a list")
    settings_store.update({"CUSTOM_POLICIES": policies})
    return JSONResponse({"ok": True})


@router.post("/api/settings/partial")
async def api_settings_partial(request: Request, _: str = Depends(_require_admin)):
    """Generic single/multi-key settings update for simple admin toggles
    (Lexware-Formatkorrektur, Logging, Let's Encrypt domain/email, …) that
    don't warrant their own dedicated endpoint. Caller is trusted to send
    only known setting keys — this is admin-authenticated already."""
    body = await request.json()
    if not isinstance(body, dict) or not body:
        raise HTTPException(400, "Leerer oder ungültiger Request-Body")
    settings_store.update(body)
    return JSONResponse({"ok": True})


@router.post("/api/settings/sender-mailboxes/refresh")
async def api_refresh_sender_mailboxes(user: str = Depends(_require_admin)):
    import asyncio
    import exo_mailboxes
    try:
        await asyncio.to_thread(exo_mailboxes.list_mailboxes, True)  # force refresh
    except Exception as exc:
        raise HTTPException(500, str(exc))
    return JSONResponse({"ok": True, "mailboxes": exo_mailboxes.as_sender_list()})


@router.post("/api/settings/notification-mailbox/create-shared")
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


@router.get("/settings", response_class=HTMLResponse)
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
            "s": settings_store.public_view(),
            "active": "settings",
            "saved": request.query_params.get("saved"),
            "gateway_name": _gateway_name(),
            "sender_mailboxes": sender_mailboxes,
        },
    )


@router.get("/settings/signature", response_class=HTMLResponse)
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
            "s": settings_store.public_view(),
            "active": "settings-signature",
            "saved": request.query_params.get("saved"),
            "gateway_name": _gateway_name(),
            "sender_mailboxes": sender_mailboxes,
            "custom_var_entra_fields": graph_client.CUSTOM_VAR_ENTRA_FIELDS,
        },
    )


@router.get("/settings/smime", response_class=HTMLResponse)
async def settings_smime_page(request: Request, user: str = Depends(_require_admin)):
    return templates.TemplateResponse(
        request=request, name="settings_smime.html",
        context={
            "s": settings_store.public_view(),
            "active": "settings-smime",
            "saved": request.query_params.get("saved"),
            "gateway_name": _gateway_name(),
        },
    )


@router.get("/settings/connect", response_class=HTMLResponse)
async def settings_connect_page(request: Request, user: str = Depends(_require_admin)):
    import hub_client
    return templates.TemplateResponse(
        request=request, name="settings_connect.html",
        context={
            "s": settings_store.public_view(),
            "active": "settings-connect",
            "gateway_name": _gateway_name(),
            "hub_registered": hub_client.is_registered(),
            "hub_cert_registered": hub_client.cert_is_registered(),
        },
    )


@router.get("/settings/update")
async def settings_update_redirect(user: str = Depends(_require_admin)):
    # Update-Tab wurde mit Backup zusammengelegt
    return RedirectResponse("/backup", status_code=308)


@router.post("/settings")
async def settings_save(request: Request, user: str = Depends(_require_admin)):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Ungültige JSON-Daten")
    clean = {k: v for k, v in data.items() if k in settings_store.DEFAULTS}
    settings_store.update(clean)
    log.info("Settings updated by %s: %s", user, list(clean.keys()))
    return JSONResponse({"ok": True})
