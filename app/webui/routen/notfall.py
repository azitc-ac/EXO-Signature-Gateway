"""Notbetrieb-Webmail unter /notfall — für alle Mitarbeiter, nicht nur Admins.

Anmeldung per LDAP am lokalen AD (notbetrieb_anmeldung), danach ein eigenes,
signiertes Sitzungs-Cookie (`nb_sitzung`, nur für /notfall, SameSite=Strict).
Es trägt NUR das eigene Postfach; was jemand sehen darf, wird bei JEDEM Abruf
neu aus dem Rechte-Abbild bestimmt (notbetrieb_lesen.darf_lesen).

Schreibende Aufrufe sind JSON-POSTs: Ein fremdes Formular kann keinen
JSON-Rumpf ohne CORS-Vorabfrage senden, und das Cookie geht wegen
SameSite=Strict nicht mit — das ersetzt hier ein eigenes CSRF-Token.
"""
from __future__ import annotations

import asyncio
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import notbetrieb
import notbetrieb_anmeldung
import notbetrieb_lesen
import notbetrieb_rechte
import notbetrieb_senden
from webui.deps import templates, log, _gateway_name

router = APIRouter()

COOKIE = "nb_sitzung"
TTL_S = 12 * 60 * 60


def _signierer() -> URLSafeTimedSerializer:
    import sso
    return URLSafeTimedSerializer(sso._get_secret(), salt="notbetrieb-webmail")


def _benutzer(request: Request) -> str:
    """Angemeldetes Postfach oder 401. Bei ausgeschaltetem Notbetrieb 404 —
    das Webmail existiert dann nicht."""
    if not notbetrieb.funktion_aktiv():
        raise HTTPException(404)
    roh = request.cookies.get(COOKIE) or ""
    try:
        daten = _signierer().loads(roh, max_age=TTL_S)
    except (BadSignature, SignatureExpired):
        raise HTTPException(401, "Nicht angemeldet.")
    benutzer = str((daten or {}).get("b") or "")
    if not benutzer:
        raise HTTPException(401, "Nicht angemeldet.")
    return benutzer


def _postfach(benutzer: str, postfach: str) -> str:
    postfach = (postfach or "").strip().lower()
    if not notbetrieb_lesen.darf_lesen(benutzer, postfach):
        raise HTTPException(403, "Kein Zugriff auf dieses Postfach.")
    return postfach


@router.get("/notfall", response_class=HTMLResponse)
async def notfall_seite(request: Request):
    if not notbetrieb.funktion_aktiv():
        raise HTTPException(404)
    return templates.TemplateResponse(request=request, name="notfall.html",
                                      context={"gateway_name": _gateway_name(),
                                               "eingerichtet": notbetrieb_anmeldung.eingerichtet()})


@router.post("/notfall/anmelden")
async def notfall_anmelden(request: Request):
    if not notbetrieb.funktion_aktiv():
        raise HTTPException(404)
    try:
        body = await request.json()
    except Exception:                                               # noqa: BLE001
        raise HTTPException(400, "Ungültige Anfrage")
    quelle = request.client.host if request.client else ""
    erg = await asyncio.to_thread(notbetrieb_anmeldung.anmelden, str(body.get("benutzer") or ""),
                                  str(body.get("passwort") or ""), quelle)
    if not erg.get("ok"):
        return JSONResponse({"ok": False, "error": erg.get("error"), "warten_s": erg.get("warten_s")},
                            status_code=401)
    antwort = JSONResponse({"ok": True})
    antwort.set_cookie(COOKIE, _signierer().dumps({"b": erg["postfach"]}), max_age=TTL_S,
                       path="/notfall", httponly=True, secure=True, samesite="strict")
    log.info("Notbetrieb-Webmail: Anmeldung %s → %s von %s", erg.get("benutzer"), erg["postfach"], quelle)
    return antwort


@router.post("/notfall/abmelden")
async def notfall_abmelden():
    antwort = JSONResponse({"ok": True})
    antwort.delete_cookie(COOKIE, path="/notfall")
    return antwort


@router.get("/notfall/api/ich")
async def notfall_ich(benutzer: str = Depends(_benutzer)):
    return JSONResponse({"ok": True, "benutzer": benutzer,
                         "zugriff": [p for p in await asyncio.to_thread(notbetrieb_rechte.zugriff, benutzer)
                                     if p["voll"]],
                         "absender": await asyncio.to_thread(notbetrieb_senden.absender, benutzer)})


@router.get("/notfall/api/ordner")
async def notfall_ordner(postfach: str, benutzer: str = Depends(_benutzer)):
    pf = _postfach(benutzer, postfach)
    return JSONResponse({"ok": True, "ordner": await asyncio.to_thread(notbetrieb_lesen.ordner, pf)})


@router.get("/notfall/api/kalender")
async def notfall_kalender(postfach: str, von: str, bis: str, benutzer: str = Depends(_benutzer)):
    """Termine (nur lesend). Private Termine eines fremden Postfachs nur als Zeitblock."""
    import notbetrieb_pim
    pf = _postfach(benutzer, postfach)
    eigen = any(p["postfach"] == pf and p["eigen"] for p in await asyncio.to_thread(notbetrieb_rechte.zugriff, benutzer))
    return JSONResponse({"ok": True, "termine": await asyncio.to_thread(
        notbetrieb_pim.termine, pf, von[:25], bis[:25], eigen)})


@router.get("/notfall/api/kontakte")
async def notfall_kontakte(postfach: str, benutzer: str = Depends(_benutzer)):
    import notbetrieb_pim
    pf = _postfach(benutzer, postfach)
    return JSONResponse({"ok": True, "kontakte": await asyncio.to_thread(notbetrieb_pim.kontakte, pf),
                         "recht_fehlt": await asyncio.to_thread(notbetrieb_pim.kontakte_recht_fehlt)})


@router.get("/notfall/api/adressen")
async def notfall_adressen(q: str = "", benutzer: str = Depends(_benutzer)):
    """Adresshilfe beim Verfassen: eigene Kontakte, dann die globale Adressliste."""
    import notbetrieb_pim
    return JSONResponse({"ok": True, "treffer": await asyncio.to_thread(
        notbetrieb_pim.adressen_suchen, benutzer, q[:100])})


@router.get("/notfall/api/liste")
async def notfall_liste(postfach: str, ordner: str = "", suche: str = "", seite: int = 0,
                        benutzer: str = Depends(_benutzer)):
    pf = _postfach(benutzer, postfach)
    return JSONResponse({"ok": True, **await asyncio.to_thread(notbetrieb_lesen.liste, pf, ordner,
                                                               suche[:200], seite)})


@router.get("/notfall/api/mail")
async def notfall_mail(postfach: str, id: str, benutzer: str = Depends(_benutzer)):
    pf = _postfach(benutzer, postfach)
    m = await asyncio.to_thread(notbetrieb_lesen.mail, pf, id)
    if not m:
        raise HTTPException(404)
    await asyncio.to_thread(notbetrieb_lesen.als_gelesen, pf, id)
    return JSONResponse({"ok": True, **m})


@router.get("/notfall/inhalt")
async def notfall_inhalt(postfach: str, id: str, benutzer: str = Depends(_benutzer)):
    """HTML der Mail — NUR mit durchgesetzter CSP (notbetrieb_lesen.INHALT_CSP)."""
    pf = _postfach(benutzer, postfach)
    inhalt = await asyncio.to_thread(notbetrieb_lesen.inhalt_html, pf, id)
    if inhalt is None:
        raise HTTPException(404)
    return HTMLResponse(inhalt, headers={"Content-Security-Policy": notbetrieb_lesen.INHALT_CSP,
                                         "X-Frame-Options": "SAMEORIGIN",
                                         "Cache-Control": "no-store"})


@router.get("/notfall/anhang")
async def notfall_anhang(postfach: str, id: str, nr: int, benutzer: str = Depends(_benutzer)):
    pf = _postfach(benutzer, postfach)
    a = await asyncio.to_thread(notbetrieb_lesen.anhang, pf, id, nr)
    if not a:
        raise HTTPException(404)
    daten, name, _typ = a
    # Immer als Download: ein HTML- oder SVG-Anhang darf nicht im Kontext des
    # Gateways geöffnet werden.
    return Response(daten, media_type="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
                             "Cache-Control": "no-store"})


@router.get("/notfall/api/vorlage")
async def notfall_vorlage(postfach: str, id: str, art: str, benutzer: str = Depends(_benutzer)):
    """Vorbelegung für Antworten / Allen antworten / Weiterleiten."""
    pf = _postfach(benutzer, postfach)
    try:
        return JSONResponse({"ok": True, **await asyncio.to_thread(notbetrieb_senden.vorlage, benutzer, pf, id, art)})
    except notbetrieb_senden.SendeFehler as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)


_MAX_RUMPF = 22 * 1024 * 1024      # 15 MB Anhänge, base64 (×4/3) plus Text


@router.post("/notfall/api/senden")
async def notfall_senden(request: Request, benutzer: str = Depends(_benutzer)):
    import base64
    import binascii
    if int(request.headers.get("content-length") or 0) > _MAX_RUMPF:
        return JSONResponse({"ok": False, "error": "Nachricht zu groß."}, status_code=413)
    try:
        body = await request.json()
        anhaenge = [(str(a.get("name") or "anhang"), str(a.get("typ") or ""),
                     base64.b64decode(str(a.get("daten") or ""), validate=True))
                    for a in (body.get("anhaenge") or [])[:20]]
    except (ValueError, binascii.Error, AttributeError, TypeError):
        return JSONResponse({"ok": False, "error": "Ungültige Anfrage."}, status_code=400)
    try:
        # Im Ereignis-Takt, nicht in einem Thread: senden() ruft den Handler —
        # wie jede Mail vom Connector (dort läuft reinject ebenfalls synchron).
        erg = await notbetrieb_senden.senden(
            benutzer, str(body.get("postfach") or ""), str(body.get("an") or ""),
            str(body.get("cc") or ""), str(body.get("bcc") or ""), str(body.get("betreff") or "")[:500],
            str(body.get("text") or "")[:500000], anhaenge, str(body.get("bezug") or ""),
            str(body.get("art") or "neu"))
    except notbetrieb_senden.SendeFehler as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse(erg)
