"""Routen des sicheren Nachrichtenportals.

Zweites Modul der Aufteilung von `app.py`. Muster siehe `addin.py`:

    router = APIRouter()          hier, NICHT `app`
    from webui.deps import …      nur das Fundament, nie `webui.app`
    app.py:  ROUTENMODULE = [addin, portal]

ZUM ZUGANG
----------
Die Portal-Adressen sind ABSICHTLICH ohne Anmeldung an der Oberfläche: Sie
richten sich an externe Empfänger, die kein Konto haben. Der Zugang haengt
allein am Merkmal in der Adresse und — sofern eingeschaltet — an einer
Einmalkennzahl per Mail (`_portal_check_access`).

Die Verwaltungsadressen darunter (`/api/portal/admin/…`) verlangen sehr wohl
die Verwaltungsrolle. Beim Verschieben ist das die Stelle, an der ein Fehler
teuer waere: eine Adresse, die ihr `Depends(_require_admin)` verliert, gibt
Fremden die Liste aller Nachrichten preis. `tests/test_seiten.py` prueft
deshalb, dass ohne Anmeldung keine Route etwas herausgibt.
"""
from __future__ import annotations

import json as _json_mod
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response

import config
import portal_store
import settings_store

from webui.deps import (
    templates, log, _gateway_name, _check_auth, _require_admin,
)

router = APIRouter()


# ── Secure Message Portal ────────────────────────────────────────────────────

@router.get("/portal/logo")
async def portal_logo():
    """Öffentliches Branding-Logo — referenziert von Portal-Seite und Mails."""
    import portal_store
    logo = portal_store.get_logo()
    if not logo:
        raise HTTPException(status_code=404)
    data, ctype = logo
    return Response(content=data, media_type=ctype,
                    headers={"Cache-Control": "public, max-age=3600"})


@router.post("/api/portal/branding/logo")
async def portal_upload_logo(file: UploadFile = File(...), user: str = Depends(_require_admin)):
    """Admin: Branding-Logo hochladen (PNG/JPEG/GIF, max. 512 KB)."""
    import portal_store
    ctype = (file.content_type or "").lower()
    if ctype not in portal_store.LOGO_ALLOWED_TYPES:
        raise HTTPException(400, "Nur PNG, JPEG oder GIF erlaubt")
    data = await file.read()
    if len(data) > portal_store.LOGO_MAX_BYTES:
        raise HTTPException(400, "Logo zu groß (max. 512 KB)")
    if not data:
        raise HTTPException(400, "Leere Datei")
    portal_store.save_logo(data, ctype)
    log.info("Portal branding logo uploaded by %s (%d bytes, %s)", user, len(data), ctype)
    return JSONResponse({"ok": True})


@router.delete("/api/portal/branding/logo")
async def portal_delete_logo(user: str = Depends(_require_admin)):
    import portal_store
    portal_store.delete_logo()
    log.info("Portal branding logo removed by %s", user)
    return JSONResponse({"ok": True})


@router.get("/portal/{token}", response_class=HTMLResponse)
async def portal_page(request: Request, token: str):
    """Public portal page — no auth required. Token is the access credential."""
    import portal_store
    return templates.TemplateResponse(
        request=request, name="portal.html",
        context={"token": token,
                 "brand_name": (settings_store.get("PORTAL_BRAND_NAME") or "").strip(),
                 "has_logo": portal_store.has_logo()})


def _portal_otp_required() -> bool:
    return settings_store.get("SECURE_PORTAL_OTP") is not False


def _mask_email(addr: str) -> str:
    """papazar73@gmail.com → pa*******@gmail.com (Hinweis, wohin der Code geht)."""
    local, _, domain = (addr or "").partition("@")
    if len(local) <= 2:
        return f"{local[:1]}*@{domain}"
    return f"{local[:2]}{'*' * (len(local) - 2)}@{domain}"


def _portal_check_access(token: str, request: Request) -> None:
    """Raise 401 mit otp_required, wenn OTP aktiv und keine gültige Freischaltung."""
    import portal_store
    if not _portal_otp_required():
        return
    access = request.headers.get("X-Portal-Access") or ""
    if not portal_store.check_access(token, access):
        msg = portal_store.get_message(token)
        raise HTTPException(
            status_code=401,
            detail={"otp_required": True,
                    "recipient_hint": _mask_email(msg["recipient_email"]) if msg else ""},
        )


@router.post("/api/portal/otp/{token}")
async def portal_request_otp(token: str):
    """Zugangscode anfordern — wird an das Empfänger-Postfach gesendet."""
    import asyncio as _aio
    import portal_store
    msg = portal_store.get_message(token)
    if not msg or portal_store.is_expired(msg):
        raise HTTPException(status_code=404)
    code = portal_store.generate_otp(token)
    if not code:
        raise HTTPException(status_code=429,
                            detail="Bitte warten Sie eine Minute, bevor Sie einen neuen Code anfordern.")
    import notification as _notif
    _msg_copy = dict(msg)
    ok = await _aio.get_event_loop().run_in_executor(
        None, lambda: _notif.send_portal_otp(_msg_copy, code)
    )
    if not ok:
        raise HTTPException(status_code=502, detail="Code-Versand fehlgeschlagen")
    return JSONResponse({"ok": True, "recipient_hint": _mask_email(msg["recipient_email"])})


@router.post("/api/portal/verify/{token}")
async def portal_verify_otp(token: str, body: dict):
    """Code prüfen; bei Erfolg 24h-Freischaltung für diesen Browser."""
    import portal_store
    msg = portal_store.get_message(token)
    if not msg or portal_store.is_expired(msg):
        raise HTTPException(status_code=404)
    access = portal_store.verify_otp(token, (body.get("code") or "").strip())
    if not access:
        raise HTTPException(status_code=403, detail="Code ungültig oder abgelaufen")
    return JSONResponse({"access": access})


@router.get("/api/portal/message/{token}")
async def portal_get_message(token: str, request: Request):
    """Return encrypted blob for client-side decryption. Token + ggf. OTP-Freischaltung."""
    import base64
    import portal_store
    msg = portal_store.get_message(token)
    if not msg or portal_store.is_expired(msg):
        raise HTTPException(status_code=404, detail="Nachricht nicht gefunden oder abgelaufen")
    _portal_check_access(token, request)
    blob = portal_store.get_blob(token)
    if not blob:
        raise HTTPException(status_code=404, detail="Nachricht nicht gefunden")
    return JSONResponse({
        "blob":         base64.b64encode(blob).decode(),
        "sender_name":  msg["sender_name"],
        "sender_email": msg["sender_email"],
        "subject":      msg["subject"],
        "expires_at":   msg["expires_at"],
        "read_at":      msg["read_at"],
        "replied_at":   msg["replied_at"],
        "replies":      portal_store.get_replies(token),
    })


@router.post("/api/portal/read/{token}")
async def portal_mark_read(token: str, request: Request):
    """Mark as read (first call only) and trigger read-receipt notification."""
    import asyncio as _aio
    import portal_store
    msg = portal_store.get_message(token)
    if not msg or portal_store.is_expired(msg):
        raise HTTPException(status_code=404)
    _portal_check_access(token, request)
    first_read = portal_store.mark_read(token)
    if first_read:
        import notification as _notif
        # Frisch lesen — msg wurde VOR mark_read geholt, read_at wäre sonst
        # None und die Lesebestätigung zeigte nur "gerade eben"
        _msg_copy = portal_store.get_message(token) or dict(msg)
        _aio.get_event_loop().run_in_executor(
            None, lambda: _notif.send_portal_read_receipt(_msg_copy)
        )
    return JSONResponse({"ok": True})


@router.get("/api/portal/admin/list")
async def portal_admin_list(_=Depends(_require_admin)):
    """Admin: aktive Portal-Nachrichten + Status für die S/MIME-Seite."""
    import portal_store
    msgs = portal_store.list_messages()
    fields = ("token", "sender_email", "recipient_email", "subject",
              "created_at", "expires_at", "read_at", "replied_at")
    return JSONResponse({
        "enabled": bool(settings_store.get("SECURE_PORTAL_ENABLED")),
        "retention_days": int(settings_store.get("SECURE_PORTAL_RETENTION_DAYS") or 14),
        "messages": [{k: m[k] for k in fields} for m in msgs],
    })


@router.delete("/api/portal/admin/{token}")
async def portal_admin_delete(token: str, user: str = Depends(_require_admin)):
    """Admin: Portal-Nachricht widerrufen (Link sofort ungültig)."""
    import portal_store
    ok = portal_store.delete_message(token)
    if ok:
        log.info("Portal message %s revoked by %s", token, user)
    return JSONResponse({"ok": ok})


@router.post("/api/portal/reply/{token}")
async def portal_reply(token: str, body: dict, request: Request):
    """Send a reply from the portal user to the original sender."""
    import asyncio as _aio
    import portal_store
    msg = portal_store.get_message(token)
    if not msg or portal_store.is_expired(msg):
        raise HTTPException(status_code=404)
    _portal_check_access(token, request)
    reply_text = (body.get("text") or "").strip()
    reply_name = (body.get("name") or "").strip()[:200]
    if not reply_text or len(reply_text) > 100_000:
        raise HTTPException(status_code=400, detail="Antworttext fehlt oder zu lang")

    # Anhänge: Graph sendMail hat ~4 MB Request-Limit → 3 MB Nutzdaten gesamt
    import base64 as _b64
    import os.path as _osp
    attachments = []
    total = 0
    for a in (body.get("attachments") or [])[:5]:
        name = _osp.basename((a.get("name") or "anhang").strip())[:120] or "anhang"
        data = a.get("data") or ""
        try:
            raw_len = len(_b64.b64decode(data, validate=True))
        except Exception:
            raise HTTPException(status_code=400, detail=f"Anhang {name}: ungültige Daten")
        total += raw_len
        if total > 3 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Anhänge zu groß (max. 3 MB gesamt)")
        attachments.append({"name": name, "type": (a.get("type") or "")[:100], "data": data})

    import notification as _notif
    _msg_copy = dict(msg)
    ok = await _aio.get_event_loop().run_in_executor(
        None, lambda: _notif.send_portal_reply(_msg_copy, reply_text, reply_name, attachments)
    )
    if ok:
        import portal_store as _ps
        _ps.mark_replied(token)
        # Antwort-Historie: clientseitig verschlüsselt (URL-Fragment-Key),
        # der Server speichert nur den Ciphertext. Limit 8 MB: enthält auch
        # die Anhänge (3 MB raw → doppelt base64 ≈ 5,5 MB), damit der
        # Empfänger sie später erneut herunterladen kann.
        cipher = (body.get("cipher") or "").strip()
        if cipher and len(cipher) <= 8_000_000:
            _ps.add_reply(token, cipher)
    return JSONResponse({"ok": ok})


