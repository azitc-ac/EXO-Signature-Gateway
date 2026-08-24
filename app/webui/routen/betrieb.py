"""Laufender Betrieb: Zustand, Wartung, zurückgehaltene Post, Protokolle.

Was hier steht, betrifft die Maschine und nicht die Fachlichkeit: Ist der
Dienst gesund? Läuft der Wartungsmodus? Was liegt zurückgehalten? Was steht im
Protokoll? Alles davon verlangt die Verwaltungsrolle — bis auf `/health`, das
der Container selbst abfragt, bevor es überhaupt eine Anmeldung gibt.

Aus `app/webui/app.py` herausgelöst (21.08.2026). Reines Umsortieren — der
Inhalt der Funktionen ist unverändert; die Routen-Momentaufnahme in
`tests/test_routes.py` belegt, dass dieselbe Oberfläche herauskommt.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, StreamingResponse)

import asyncio
import os
import queue as _queue_mod
import sys
import threading

import config
import held_mails as _held_mails_mod
import settings_store

from webui.deps import (
    templates, log, _gateway_name, _check_auth, _require_admin,
    _LOG_BUFFER, _LOG_SUBSCRIBERS, _LOG_SUBSCRIBERS_LOCK,
    _make_log_token, _check_log_token,
)

router = APIRouter()


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


@router.get("/health")
async def health():
    return JSONResponse({"status": "ok", "service": "exo-signature-service"})

@router.get("/api/health/mailboxes")
async def api_health_mailboxes(_=Depends(_require_admin)):
    """Return current cached MAILBOX_HEALTH data."""
    return settings_store.get("MAILBOX_HEALTH") or {}

@router.post("/api/health/mailboxes")
async def api_health_run(_=Depends(_require_admin)):
    """Run health checks for all configured mailboxes and return results."""
    import health_check
    results = await health_check.run_all_checks()
    return {"ok": True, "results": results}

@router.get("/api/health/audit-log")
async def api_health_audit_log(_=Depends(_require_admin)):
    """Return GATEWAY_AUDIT_LOG entries."""
    return settings_store.get("GATEWAY_AUDIT_LOG") or []

@router.get("/api/maintenance/mails")
async def api_held_mails_list(_: str = Depends(_require_admin)):
    return JSONResponse({
        "maintenance_mode": bool(settings_store.get("MAINTENANCE_MODE")),
        "mails": _held_mails_mod.list_all(),
    })

@router.get("/api/maintenance/mails/{mail_id}/preview", response_class=HTMLResponse)
async def api_held_mail_preview(mail_id: str, _: str = Depends(_require_admin)):
    html = _held_mails_mod.get_preview_html(mail_id)
    if html is None:
        raise HTTPException(404, "Mail not found")
    return HTMLResponse(html or "<em>(kein HTML-Inhalt)</em>")

@router.delete("/api/maintenance/mails/{mail_id}")
async def api_held_mail_delete(mail_id: str, _: str = Depends(_require_admin)):
    if not _held_mails_mod.delete(mail_id):
        raise HTTPException(404, "Mail not found")
    return JSONResponse({"ok": True})

@router.post("/api/maintenance/mails/{mail_id}/release")
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

@router.post("/api/maintenance/mode")
async def api_set_maintenance_mode(request: Request, _: str = Depends(_require_admin)):
    body = await request.json()
    enabled = bool(body.get("enabled", False))
    settings_store.update({"MAINTENANCE_MODE": enabled})
    return JSONResponse({"ok": True, "maintenance_mode": enabled})

@router.post("/api/restart")
async def api_restart(user: str = Depends(_require_admin)):
    log.info("Service restart requested by %s", user)

    def _do_restart():
        import time
        time.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=_do_restart, daemon=True).start()
    return JSONResponse({"ok": True})

@router.get("/log", response_class=HTMLResponse)
async def log_page(request: Request, user: str = Depends(_require_admin)):
    return templates.TemplateResponse(
        request=request, name="log.html",
        context={"active": "log", "stream_token": _make_log_token(),
                 "gateway_name": _gateway_name()},
    )

@router.get("/log/stream")
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

@router.get("/api/logs/search")
async def api_logs_search(q: str = "", time_from: str = "", time_to: str = "",
                          user: str = Depends(_require_admin)):
    if not q and not (time_from or time_to):
        raise HTTPException(400, "Suchbegriff oder Zeitraum fehlt")
    import log_manager
    results = log_manager.search(q, max_lines=500,
                                 time_from=time_from, time_to=time_to)
    return JSONResponse({"results": results, "count": len(results)})


@router.get("/api/audit/events")
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


@router.get("/api/abnahme")
async def api_abnahme(_=Depends(_require_admin)):
    """Ist diese Installation betriebsbereit? Punkt für Punkt.

    Bewusst eine EIGENE Sicht neben `setup_wizard.verify_*` (Einzelschritte der
    Einrichtung) und `health_check` (je Postfach): Wer alle Häkchen im
    Assistenten hat, weiss damit noch nicht, ob Post durchläuft.

    Gedacht auch zum Abfragen von aussen — der halbautomatische Aufbau einer
    Gateway-VM soll am Ende diesen Endpunkt fragen, statt dass jemand Bildschirme
    vergleicht. Siehe `abnahme.py` für die Punkte und für das, was sie
    ausdrücklich NICHT abdecken.
    """
    import abnahme
    return JSONResponse(abnahme.bericht())
