"""Routen der Postfachverwaltung.

Fünftes Modul der Aufteilung von `app.py`. Muster siehe `addin.py`.

Nicht zusammenhängend: die Endpunkte liegen zwischen Z1462 und Z2084, die
Seite selbst ganz am Ende. Eingesammelt über den Syntaxbaum, nicht über einen
Zeilenbereich.

⚠️ ZUM PFADFILTER: `/api/health/mailboxes` gehört NICHT hierher, sondern zur
Gesundheitsprüfung. Der Filter greift auf `/mailboxes`, `/mailboxes/…` und
`/api/mailboxes…` — deshalb fällt die Health-Route korrekt heraus. Ein
schlichtes `"mailbox" in pfad` hätte sie mitgenommen.

ZU `api_save_mailboxes`
-----------------------
Der mit Abstand grösste Endpunkt hier, und der einzige mit Aussenwirkung: Er
schreibt `MAILBOX_CONFIG` UND aktualisiert auf Wunsch die Exchange-
Verteilerliste. Beides gehört zusammen — steht ein Postfach in der
Konfiguration, aber nicht in der Verteilerliste, erreicht seine Post das
Gateway nie; umgekehrt läuft sie durch, ohne verarbeitet zu werden.

Die Verteilerliste braucht nach einer Änderung **5–15 Minuten**, bis Exchange
sie überall kennt. Wer direkt danach prüft und nichts sieht, hat nicht
zwangsläufig einen Fehler gefunden.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

import config
import settings_store

from webui.deps import (
    templates, log, _gateway_name, _check_auth, _require_admin,
)

router = APIRouter()


@router.get("/api/mailboxes")
async def api_get_mailboxes(_=Depends(_check_auth)):
    """List all EXO mailboxes + their current MAILBOX_CONFIG + cached health status."""
    import asyncio
    import exo_mailboxes
    import mailbox_match
    raw = await asyncio.to_thread(exo_mailboxes.list_mailboxes)
    _type_map = {"UserMailbox": "user", "SharedMailbox": "shared",
                 "RoomMailbox": "room", "EquipmentMailbox": "equipment"}
    users = [{"email": m["primary"], "name": m.get("display_name") or m["primary"],
             "type": _type_map.get(m.get("type", ""), "shared")}
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
            "config_key": mailbox_match.match_sender_key(config_map, email) or email,
            "name": u["name"],
            "type": u.get("type", "user"),
            "sig": cfg.get("sig", False),
            "smime": cfg.get("smime", False),
            "template": cfg.get("template", "default"),
            "min_template": cfg.get("min_template", ""),
            "banner_template": cfg.get("banner_template", ""),
            "disclaimer_template": cfg.get("disclaimer_template", ""),
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
                "config_key": key,
                "name": cfg.get("display_name") or cemail,
                "type": "user",
                "sig": cfg.get("sig", False),
                "smime": cfg.get("smime", False),
                "template": cfg.get("template", "default"),
                "min_template": cfg.get("min_template", ""),
                "banner_template": cfg.get("banner_template", ""),
                "disclaimer_template": cfg.get("disclaimer_template", ""),
                "addin_templates": cfg.get("addin_templates", []),
                "use_policy": cfg.get("use_policy", True),
                "health_overall": h.get("overall"),
                "health_checked": h.get("last_checked"),
                "health_checks": h.get("checks", {}),
                "bookings_url": bookings_map.get(cemail, ""),
            })
    result.sort(key=lambda r: (r.get("name") or r["email"]).lower())
    return {"mailboxes": result}


@router.get("/api/mailboxes/migrate/preview")
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


@router.post("/api/mailboxes/migrate/apply")
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


@router.post("/api/mailboxes/save")
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
        min_template = (m.get("min_template") or "").strip()
        banner_template = (m.get("banner_template") or "").strip()
        disclaimer_template = (m.get("disclaimer_template") or "").strip()
        addin_tpl = m.get("addin_templates", [])
        use_policy = bool(m.get("use_policy", True))
        entry: dict = {"sig": sig, "smime": smime, "use_policy": use_policy}
        if template and template != "default":
            entry["template"] = template
        if min_template:
            entry["min_template"] = min_template
        if banner_template:
            entry["banner_template"] = banner_template
        if disclaimer_template:
            entry["disclaimer_template"] = disclaimer_template
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


@router.post("/api/mailboxes/fetch-bookings-urls")
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


@router.get("/mailboxes", response_class=HTMLResponse)
async def mailboxes_page(request: Request, user: str = Depends(_require_admin)):
    import signature_engine as _sig_engine
    templates_list = _sig_engine.list_templates()
    return templates.TemplateResponse(
        request=request, name="mailboxes.html",
        context={"active": "mailboxes", "templates_list": templates_list,
                 "gateway_name": _gateway_name(),
                 "addin_enabled": bool(settings_store.get("ADDIN_ENABLED"))},
    )
