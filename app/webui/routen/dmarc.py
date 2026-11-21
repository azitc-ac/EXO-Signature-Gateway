"""Einstellungen → DMARC — Auswertung der Sammelberichte (dmarc_berichte.py).

Das Ziel (DMARC_POSTFACH) speichert die Seite wie jede Einstellung über
`savePartial()` → POST /settings. Hier stehen nur die Seite, der Stand und
„Jetzt abrufen“. Alles hinter `_require_admin`: Die Berichte nennen die
Versand-IPs des Unternehmens.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

import settings_store

from webui.deps import templates, log, _gateway_name, _require_admin

router = APIRouter()


@router.get("/settings/dmarc", response_class=HTMLResponse)
async def settings_dmarc_page(request: Request, user: str = Depends(_require_admin)):
    return templates.TemplateResponse(
        request=request, name="dmarc.html",
        context={"active": "settings-dmarc", "gateway_name": _gateway_name(),
                 "s": settings_store.public_view()},
    )


@router.get("/api/dmarc")
async def api_dmarc_stand(aufloesen: int = 0, user: str = Depends(_require_admin)):
    """Übersicht. `aufloesen=1` (nach dem Speichern des Ziels): Postfach oder
    Gruppe neu bestimmen — davon hängt ab, ob Group-Conversation.Read.All nötig ist."""
    import dmarc_berichte
    if aufloesen and dmarc_berichte.adresse():
        z = await asyncio.to_thread(dmarc_berichte.ziel_aufloesen)
        if z.get("art") == "postfach" and dmarc_berichte.eingrenzung_aktiv():
            # Postfach in die Gruppe „App Scope“ — sonst sperrt die Eingrenzung.
            import setup_wizard
            await asyncio.to_thread(setup_wizard.app_scope_nachziehen)
    daten = await asyncio.to_thread(dmarc_berichte.uebersicht)
    ampel, text = dmarc_berichte.bewertung()
    return JSONResponse({"ok": True, "ampel": ampel, "text": text, **daten})


@router.post("/api/dmarc/abruf")
async def api_dmarc_abruf(user: str = Depends(_require_admin)):
    """Berichte sofort abrufen (sonst alle dmarc_berichte.TAKT_H Stunden)."""
    import dmarc_berichte
    if not dmarc_berichte.adresse():
        return JSONResponse({"ok": False, "error": "Kein Postfach und keine Gruppe eingetragen."},
                            status_code=400)
    abruf = await asyncio.to_thread(dmarc_berichte.abrufen)
    log.info("DMARC-Abruf durch %s: %s", user, abruf.get("meldung") or f"{abruf.get('neu')} neue Berichte")
    if not abruf.get("ok"):
        return JSONResponse({"ok": False, "error": abruf.get("meldung") or "Abruf gescheitert",
                             "abruf": abruf}, status_code=502)
    return JSONResponse({"ok": True, "abruf": abruf})
