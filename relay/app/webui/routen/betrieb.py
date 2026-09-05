"""Betrieb: Erreichbarkeit, Übersicht, Protokoll, Mail-Protokoll."""
from __future__ import annotations

import asyncio
import json as _json
import queue as _queue_mod

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

import config
import exo_mailboxes
import mail_audit
import relay_hosts
import settings_store
import smtp_relay
import tls_cert

from webui.deps import (templates, _gateway_name, _require_admin, _make_log_token,
                        _check_log_token, _LOG_BUFFER, _LOG_SUBSCRIBERS, _LOG_SUBSCRIBERS_LOCK)

router = APIRouter()


@router.get("/health")
async def health():
    """Ohne Anmeldung — für Docker-HEALTHCHECK und den Online-Punkt der Leiste.
    Sagt, ob der Listener bedient; verrät sonst nichts."""
    import runtime_state
    c = runtime_state.smtp_controller
    smtp_ok = bool(c and getattr(c, "server", None))
    return JSONResponse({"ok": True, "smtp": smtp_ok, "version": config.VERSION},
                        status_code=200 if smtp_ok else 503)


@router.get("/", response_class=HTMLResponse)
async def uebersicht(request: Request, user: str = Depends(_require_admin)):
    return templates.TemplateResponse(
        request=request, name="uebersicht.html",
        context={"active": "uebersicht", "gateway_name": _gateway_name()})


@router.get("/api/uebersicht")
async def api_uebersicht(user: str = Depends(_require_admin)):
    geraete = relay_hosts.liste()
    adressen = exo_mailboxes.known_addresses()
    modus = settings_store.get("EXO_SUBMIT_MODE") or "smarthost"
    rueckweg_ok = bool((settings_store.get("EXO_SMARTHOST") or "").strip()) if modus == "smarthost" \
        else bool(settings_store.get("SUBMIT_USER") and settings_store.get("SUBMIT_PASSWORD"))
    return JSONResponse({
        "ok": True,
        "relay_an": bool(settings_store.get("SMTP_RELAY_ENABLED")),
        "lernmodus": smtp_relay.lernmodus_bis() is not None,
        "geraete": len(geraete),
        "gesperrt": sum(1 for g in geraete if g.get("gesperrt")),
        "klartext": sum(1 for g in geraete if g.get("tls") == "nein"),
        "abgewiesen": len(relay_hosts.abgewiesene()),
        "adressen": len(adressen),
        "exo": exo_mailboxes.zustand(),
        "rueckweg": {"modus": modus, "konfiguriert": rueckweg_ok,
                     "ziel": settings_store.get("EXO_SMARTHOST") if modus == "smarthost"
                     else settings_store.get("SUBMIT_HOST")},
        "tls": tls_cert.info(),
        "heute": mail_audit.zaehler_heute(),
        "ereignisse": mail_audit.query_events(limit=25),
        "smtp_port": config.SMTP_PORT,
    })


@router.get("/log", response_class=HTMLResponse)
async def log_page(request: Request, user: str = Depends(_require_admin)):
    return templates.TemplateResponse(
        request=request, name="log.html",
        context={"active": "log", "stream_token": _make_log_token(),
                 "gateway_name": _gateway_name()})


@router.get("/log/stream")
async def log_stream(request: Request, token: str = ""):
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

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/logs/search")
async def api_logs_search(q: str = "", time_from: str = "", time_to: str = "",
                          user: str = Depends(_require_admin)):
    if not q and not (time_from or time_to):
        raise HTTPException(400, "Suchbegriff oder Zeitraum fehlt")
    import log_manager
    results = log_manager.search(q, max_lines=500, time_from=time_from, time_to=time_to)
    return JSONResponse({"results": results, "count": len(results)})


@router.get("/api/audit/events")
async def api_audit_events(action: str | None = None, quelle: str | None = None,
                           limit: int = 200, offset: int = 0,
                           user: str = Depends(_require_admin)):
    return JSONResponse({"events": mail_audit.query_events(action=action, quelle=quelle,
                                                           limit=min(limit, 500), offset=offset)})
