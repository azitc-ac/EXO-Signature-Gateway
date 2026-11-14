"""Notbetrieb „Exchange-Schatten“: Stand, Abgleich, Rückspielen, Löschen.

Bedient wird das aus dem Assistenten (Abschnitt „Modus & Funktionen“), wo auch
der Schalter NOTBETRIEB_AKTIV steht. Die Logik liegt in `notbetrieb.py`; diese
Endpunkte reichen nur durch. Die Graph-Aufrufe dort sind synchron und laufen
deshalb in einem Thread, damit die Oberfläche bedienbar bleibt.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

import notbetrieb
from webui.deps import log, _require_admin

router = APIRouter()


@router.get("/api/notbetrieb/status")
async def api_notbetrieb_status(_user: str = Depends(_require_admin)):
    return JSONResponse({"ok": True, **await asyncio.to_thread(notbetrieb.status)})


@router.post("/api/notbetrieb/abgleich")
async def api_notbetrieb_abgleich(user: str = Depends(_require_admin)):
    if not notbetrieb.funktion_aktiv():
        return JSONResponse({"ok": False, "error": "Der Notbetrieb ist ausgeschaltet."},
                            status_code=409)
    ergebnis = await asyncio.to_thread(notbetrieb.abgleich_alle)
    log.info("Notbetrieb: Abgleich angestoßen durch %s: %s", user, ergebnis)
    return JSONResponse({"ok": True, **ergebnis})


@router.post("/api/notbetrieb/zurueckspielen")
async def api_notbetrieb_zurueckspielen(user: str = Depends(_require_admin)):
    # Bewusst auch bei ausgeschaltetem Notbetrieb: angenommene Post muss nach
    # Exchange kommen können, egal wie der Schalter danach steht.
    ergebnis = await asyncio.to_thread(notbetrieb.zurueckspielen)
    log.info("Notbetrieb: Rückspielen angestoßen durch %s: %s", user, ergebnis)
    return JSONResponse({"ok": not ergebnis.get("fehler") and not ergebnis.get("kein_token"),
                         **ergebnis})


@router.post("/api/notbetrieb/loeschen")
async def api_notbetrieb_loeschen(user: str = Depends(_require_admin)):
    ergebnis = await asyncio.to_thread(notbetrieb.schatten_loeschen)
    log.info("Notbetrieb: Schatten löschen durch %s: %s", user, ergebnis)
    return JSONResponse(ergebnis, status_code=200 if ergebnis.get("ok") else 409)
