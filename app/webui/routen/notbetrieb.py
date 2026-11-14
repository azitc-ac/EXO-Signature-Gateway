"""Notbetrieb „Exchange-Schatten“: Stand, Abgleich, Rückspielen, Löschen.

Eingeschaltet wird im Assistenten (Abschnitt „Modus & Funktionen“, Schalter
NOTBETRIEB_AKTIV); bedient auf der eigenen Seite /notbetrieb. Die Logik liegt
in `notbetrieb.py`; diese Endpunkte reichen nur durch. Die Graph-Aufrufe dort sind synchron und laufen
deshalb in einem Thread, damit die Oberfläche bedienbar bleibt.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import gruppen
import notbetrieb
import notbetrieb_eingang
import notbetrieb_anmeldung
import notbetrieb_rechte
import settings_store
from webui.deps import templates, log, _gateway_name, _require_admin

router = APIRouter()


@router.get("/notbetrieb", response_class=HTMLResponse)
async def notbetrieb_page(request: Request, _user: str = Depends(_require_admin)):
    # Aus UND keine Kopie → kein versteckter Zugang über die URL; zum Schalter
    # führen. Liegt noch eine Kopie, bleibt die Seite für Zurückspielen/Löschen.
    if not notbetrieb.sichtbar():
        return RedirectResponse("/setup#step-mode", status_code=303)
    regeln = settings_store.get("NOTBETRIEB_REGELN") or []
    return templates.TemplateResponse(
        request=request, name="notbetrieb.html",
        context={
            "active": "notbetrieb",
            "aktiv": notbetrieb.funktion_aktiv(),
            "ausgerufen": notbetrieb.ausgerufen(),
            "eingang": {"greylist": notbetrieb_eingang.greylisting_an(), "max_empf": notbetrieb_eingang.max_empfaenger(),
                        "max_mb": notbetrieb_eingang.max_bytes() // (1024 * 1024),
                        "je_stunde": notbetrieb_eingang.je_stunde()},
            "gateway_name": _gateway_name(),
            "fenster": notbetrieb.standard_fenster(),
            "regeln": regeln if isinstance(regeln, list) else [],
            "gruppen": sorted(gruppen.interne_gruppen()),
            "postfaecher": [upn for _key, upn in notbetrieb._postfaecher()],
            "max_tage": notbetrieb.MAX_TAGE,
            "mess_tage": notbetrieb.MESS_TAGE,
            "ldap": notbetrieb_anmeldung.einstellungen(),
            "umfang": notbetrieb.umfang(),
        },
    )


@router.get("/api/notbetrieb/postfaecher")
async def api_notbetrieb_postfaecher(_user: str = Depends(_require_admin)):
    return JSONResponse({"ok": True, "items": await asyncio.to_thread(notbetrieb.postfach_uebersicht)})


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


@router.post("/api/notbetrieb/platz-messen")
async def api_notbetrieb_platz_messen(user: str = Depends(_require_admin)):
    """Messung der Post der letzten Tage live aus Exchange anstoßen. Läuft im
    Hintergrund (bei vielen Postfächern Minuten); Stand über GET derselben Adresse."""
    if notbetrieb.messlauf()["laeuft"]:
        return JSONResponse({"ok": True, "laeuft": True, **notbetrieb.messlauf()})
    log.info("Notbetrieb: Platzmessung angestoßen durch %s", user)
    asyncio.get_running_loop().run_in_executor(None, notbetrieb.platz_messen)
    return JSONResponse({"ok": True, "laeuft": True})


@router.get("/api/notbetrieb/platz-messen")
async def api_notbetrieb_platz_messen_stand(_user: str = Depends(_require_admin)):
    return JSONResponse({"ok": True, **notbetrieb.messlauf()})


@router.post("/api/notbetrieb/platz-rechnen")
async def api_notbetrieb_platz_rechnen(request: Request, _user: str = Depends(_require_admin)):
    """Was wäre, wenn: Bedarf für die übergebene (noch ungespeicherte) Einstellung."""
    try:
        body = await request.json()
    except Exception:                                               # noqa: BLE001
        body = {}
    regeln = body.get("regeln") if isinstance(body.get("regeln"), list) else None
    std = body.get("fenster") if isinstance(body.get("fenster"), dict) else None
    return JSONResponse(await asyncio.to_thread(notbetrieb.platz_rechnen, regeln, std))


@router.get("/api/notbetrieb/rechte")
async def api_notbetrieb_rechte(_user: str = Depends(_require_admin)):
    """Abbild der Postfachrechte: Stand und je Postfach wer darf was."""
    return JSONResponse({"ok": True, "stand": await asyncio.to_thread(notbetrieb_rechte.stand),
                         "items": await asyncio.to_thread(notbetrieb_rechte.uebersicht)})


@router.post("/api/notbetrieb/rechte")
async def api_notbetrieb_rechte_abgleichen(user: str = Depends(_require_admin)):
    """Rechte jetzt aus Exchange lesen (EXO-PowerShell, bei vielen Postfächern Minuten)."""
    if not notbetrieb.funktion_aktiv():
        return JSONResponse({"ok": False, "error": "Der Notbetrieb ist ausgeschaltet."}, status_code=409)
    ergebnis = await asyncio.to_thread(notbetrieb_rechte.abgleichen)
    log.info("Notbetrieb: Rechte-Abgleich durch %s: %s", user, ergebnis)
    if not ergebnis.get("ok"):
        ergebnis["error"] = "Abruf aus Exchange fehlgeschlagen — der bisherige Stand bleibt."
    return JSONResponse(ergebnis, status_code=200 if ergebnis.get("ok") else 502)


@router.get("/api/notbetrieb/zugriff")
async def api_notbetrieb_zugriff(wer: str = "", _user: str = Depends(_require_admin)):
    """Was sieht <wer> im Notbetrieb? Dieselbe Auflösung, die das Webmail nutzt."""
    return JSONResponse({"ok": True, "items": await asyncio.to_thread(notbetrieb_rechte.zugriff, wer)})


@router.post("/api/notbetrieb/anmeldung-test")
async def api_notbetrieb_anmeldung_test(request: Request, user: str = Depends(_require_admin)):
    """Notbetrieb-Anmeldung mit eingegebenen Zugangsdaten durchspielen — dieselbe
    Prüfung wie im Webmail. Nichts davon wird gespeichert."""
    try:
        body = await request.json()
    except Exception:                                               # noqa: BLE001
        body = {}
    quelle = request.client.host if request.client else ""
    ergebnis = await asyncio.to_thread(notbetrieb_anmeldung.anmelden, str(body.get("benutzer") or ""),
                                       str(body.get("passwort") or ""), quelle)
    log.info("Notbetrieb: Anmeldetest durch %s für %s: %s", user, body.get("benutzer"),
             "ok" if ergebnis.get("ok") else ergebnis.get("error"))
    return JSONResponse(ergebnis)
