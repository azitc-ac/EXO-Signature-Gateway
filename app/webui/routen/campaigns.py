"""Routen der Banner-Kampagnen — Seite + Verwaltung.

Eine Kampagne zeigt ein Banner zeitfenster-/gruppengesteuert (banner_campaigns.py).
Die Zuweisung ist ein Marketing-Vorgang; ab Stufe 2 gehört sie der Rolle
`kampagnen-manager`. In dieser Stufe noch `_require_admin` — die Wache wird
gemeinsam mit der Rolle auf `_require_kampagnen` umgestellt.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

import banner_campaigns
import settings_store
import signature_engine

from webui.deps import templates, log, _gateway_name, _require_kampagnen

router = APIRouter()


def _ansicht() -> dict:
    now = datetime.now(timezone.utc)
    camps = [{**c, "status": banner_campaigns.status(c, now)}
             for c in banner_campaigns.liste()]
    return {
        "campaigns": camps,
        "banners": signature_engine.list_templates("signatur"),
        "groups": sorted((settings_store.get("INTERNAL_GROUPS") or {}).keys()),
    }


@router.get("/kampagnen", response_class=HTMLResponse)
async def kampagnen_page(request: Request, user: str = Depends(_require_kampagnen)):
    return templates.TemplateResponse(
        request=request, name="kampagnen.html",
        context={"active": "kampagnen", "gateway_name": _gateway_name()},
    )


@router.get("/api/campaigns")
async def api_campaigns(user: str = Depends(_require_kampagnen)):
    return JSONResponse({"ok": True, **_ansicht()})


@router.post("/api/campaigns")
async def api_campaigns_save(request: Request, user: str = Depends(_require_kampagnen)):
    daten = await request.json()
    try:
        rec = banner_campaigns.speichern(daten)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    log.info("Banner-Kampagne %s (%s) durch %s gespeichert",
             rec["id"], rec["name"], user)
    return JSONResponse({"ok": True, "campaign": rec})


@router.post("/api/campaigns/delete")
async def api_campaigns_delete(request: Request, user: str = Depends(_require_kampagnen)):
    daten = await request.json()
    weg = banner_campaigns.loeschen(daten.get("id") or "")
    if weg:
        log.info("Banner-Kampagne %s durch %s entfernt", daten.get("id"), user)
    return JSONResponse({"ok": weg})
