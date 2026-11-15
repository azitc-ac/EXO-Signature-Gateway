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
import notbetrieb_entwuerfe
import notbetrieb_faktor
import notbetrieb_lesen
import notbetrieb_rechte
import settings_store
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


def inhalte_erlaubt(benutzer: str) -> bool:
    """So kann der Zugang vorab auf jedem Gerät eingerichtet werden, ohne dass
    dort jemand mitliest — Regel in `notbetrieb.inhalte_erlaubt`."""
    return notbetrieb.inhalte_erlaubt(benutzer)


def _leser(benutzer: str = Depends(_benutzer)) -> str:
    """Wache für alle INHALTE des Webmails (Mail, Kalender, Kontakte, Senden,
    Adresshilfe) — EINE Stelle, damit keine Route den Bereitschaftsmodus vergisst."""
    if not inhalte_erlaubt(benutzer):
        raise HTTPException(403, "Bereit — der Notfall ist nicht ausgerufen.")
    return benutzer


def _sender(benutzer: str = Depends(_leser)) -> str:
    """Wache fürs SENDEN — strenger als Lesen (notbetrieb.senden_erlaubt)."""
    if not notbetrieb.senden_erlaubt(benutzer):
        raise HTTPException(403, "Senden geht hier nur im Notfall — im Normalbetrieb nimm Outlook.")
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
                                      context={"gateway_name": _gateway_name(), "einrichten": False,
                                               "eingerichtet": notbetrieb_anmeldung.eingerichtet(),
                                               "frisch_min": FRISCH_S // 60})


@router.get("/notfall/app.webmanifest")
async def notfall_manifest():
    """Homescreen-App („Zum Home-Bildschirm“, Intune-Web-Link): eigenes
    Fenster ohne Browserleiste, Start und Geltungsbereich nur /notfall."""
    if not notbetrieb.funktion_aktiv():
        raise HTTPException(404)
    return JSONResponse({
        "name": f"Notfall-Mail {_gateway_name()}", "short_name": "Notfall-Mail",
        "start_url": "/notfall", "scope": "/notfall", "display": "standalone",
        "background_color": "#f1f5f9", "theme_color": "#0f172a", "lang": "de",
        "icons": [{"src": "/static/favicon.svg", "sizes": "any", "type": "image/svg+xml"},
                  {"src": "/static/symbol/192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/static/apple-touch-icon.png", "sizes": "180x180", "type": "image/png"}],
    }, media_type="application/manifest+json")


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
    postfach = erg["postfach"]
    # Zweiter Faktor: erst das Kennwort, dann der Code (sonst verriete die
    # Antwort ohne Kennwort, wer einen Faktor hat). Das Kennwort ist hier
    # geprüft; der Vorgang trägt das Postfach für 5 Minuten.
    if notbetrieb_faktor.eingerichtet(postfach):
        st = notbetrieb_faktor.stand(postfach)
        rp = notbetrieb_faktor.passkey_rp(_seiten_host(request))
        return JSONResponse({"ok": True, "faktor": True, "totp": st["totp"],
                             "passkey": any(p["rp"] == rp for p in st["passkeys"]),
                             "vorgang": _vorgang_signierer().dumps({"b": postfach})})
    if notbetrieb_faktor.pflicht() and not (notbetrieb.ausgerufen() and notbetrieb_faktor.im_notfall_ohne()):
        log.info("Notbetrieb-Webmail: %s ohne zweiten Faktor abgewiesen (von %s)", postfach, quelle)
        return JSONResponse({"ok": False, "einrichten": True,
                             "error": "Für den Notfall-Zugang brauchst du einen zweiten Faktor. Richte ihn "
                                      "einmalig ein — mit deiner Microsoft-Anmeldung, solange sie geht."},
                            status_code=403)
    return _anmeldung_fertig(postfach, erg.get("benutzer"), quelle, "ohne Faktor")


def _seiten_host(request: Request) -> str:
    """Hostname, unter dem die Seite im Browser offen ist (rp_id der Passkeys).
    Aus dem Origin-Kopf, den der Browser bei jedem POST setzt; sonst Host.
    Fälschen bringt nichts: Der Browser signiert nur für seine echte Seite,
    und notbetrieb_faktor prüft die Herkunft aus clientDataJSON."""
    from urllib.parse import urlsplit
    herkunft = request.headers.get("origin") or ""
    return (urlsplit(herkunft).hostname if herkunft else request.url.hostname) or ""


def _vorgang_postfach(body: dict) -> str | None:
    try:
        return str(_vorgang_signierer().loads(str(body.get("vorgang") or ""), max_age=300).get("b") or "") or None
    except (BadSignature, SignatureExpired):
        return None


_VORGANG_ABGELAUFEN = {"ok": False, "error": "Die Anmeldung ist abgelaufen — bitte erneut mit Kennwort.", "neu": True}


def _vorgang_signierer() -> URLSafeTimedSerializer:
    import sso
    return URLSafeTimedSerializer(sso._get_secret(), salt="notbetrieb-faktor-vorgang")


def _anmeldung_fertig(postfach: str, anmeldename, quelle: str, wie: str) -> JSONResponse:
    antwort = JSONResponse({"ok": True})
    antwort.set_cookie(COOKIE, _signierer().dumps({"b": postfach}), max_age=TTL_S,
                       path="/notfall", httponly=True, secure=True, samesite="strict")
    log.info("Notbetrieb-Webmail: Anmeldung %s → %s von %s (%s)", anmeldename, postfach, quelle, wie)
    return antwort


@router.post("/notfall/anmelden/faktor")
async def notfall_anmelden_faktor(request: Request):
    """Zweiter Schritt: Code aus der Authenticator-App oder ein Wiederherstellungscode."""
    if not notbetrieb.funktion_aktiv():
        raise HTTPException(404)
    try:
        body = await request.json()
    except Exception:                                               # noqa: BLE001
        raise HTTPException(400, "Ungültige Anfrage")
    postfach = _vorgang_postfach(body)
    if not postfach:
        return JSONResponse(_VORGANG_ABGELAUFEN, status_code=401)
    quelle = request.client.host if request.client else ""
    ok, meldung = await asyncio.to_thread(notbetrieb_faktor.pruefen, postfach, str(body.get("code") or ""), quelle)
    if not ok:
        return JSONResponse({"ok": False, "error": meldung}, status_code=401)
    return _anmeldung_fertig(postfach, postfach, quelle, "mit Code")


@router.post("/notfall/anmelden/passkey/optionen")
async def notfall_anmelden_passkey_optionen(request: Request):
    """Zweiter Schritt mit Passkey, Teil 1: Herausforderung (gilt einmal)."""
    if not notbetrieb.funktion_aktiv():
        raise HTTPException(404)
    try:
        body = await request.json()
    except Exception:                                               # noqa: BLE001
        raise HTTPException(400, "Ungültige Anfrage")
    postfach = _vorgang_postfach(body)
    if not postfach:
        return JSONResponse(_VORGANG_ABGELAUFEN, status_code=401)
    optionen = await asyncio.to_thread(notbetrieb_faktor.passkey_anmelden_optionen, postfach, _seiten_host(request))
    if not optionen:
        return JSONResponse({"ok": False, "error": "Unter dieser Adresse ist kein Passkey eingerichtet — "
                                                   "nimm den Code."}, status_code=404)
    return JSONResponse({"ok": True, "optionen": optionen})


@router.post("/notfall/anmelden/passkey")
async def notfall_anmelden_passkey(request: Request):
    """Zweiter Schritt mit Passkey, Teil 2: signierte Antwort des Browsers."""
    if not notbetrieb.funktion_aktiv():
        raise HTTPException(404)
    try:
        body = await request.json()
    except Exception:                                               # noqa: BLE001
        raise HTTPException(400, "Ungültige Anfrage")
    postfach = _vorgang_postfach(body)
    if not postfach:
        return JSONResponse(_VORGANG_ABGELAUFEN, status_code=401)
    quelle = request.client.host if request.client else ""
    antwort = body.get("antwort") if isinstance(body.get("antwort"), dict) else {}
    ok, meldung = await asyncio.to_thread(notbetrieb_faktor.passkey_pruefen, postfach, antwort, quelle)
    if not ok:
        return JSONResponse({"ok": False, "error": meldung}, status_code=401)
    return _anmeldung_fertig(postfach, postfach, quelle, "mit Passkey")


# ── Zweiten Faktor einrichten (nach frischer Entra-Anmeldung) ───────────────

FRISCH_S = 15 * 60


EINRICHTEN_COOKIE = "nb_einrichten"


def _einrichten_signierer() -> URLSafeTimedSerializer:
    import sso
    return URLSafeTimedSerializer(sso._get_secret(), salt="notbetrieb-faktor-einrichten")


def einrichten_bescheinigen(upn: str):
    """Nach der vollständig geprüften Entra-Anmeldung (anmeldung.py,
    /auth/id-callback): Bescheinigung für 15 Minuten, nur /notfall. Keine
    Sitzung des Gateways — der Nachweis berechtigt zu nichts anderem."""
    from fastapi.responses import RedirectResponse
    antwort = RedirectResponse("/notfall/einrichten", status_code=302)
    if notbetrieb.funktion_aktiv() and upn:
        antwort.set_cookie(EINRICHTEN_COOKIE, _einrichten_signierer().dumps({"u": upn.strip().lower()}),
                           max_age=FRISCH_S, path="/notfall", httponly=True, secure=True, samesite="strict")
        log.info("Notbetrieb-Faktor: Entra-Nachweis für %s (Einrichten)", upn)
    return antwort


def _entra_postfach(request: Request) -> str:
    """Postfach aus einer FRISCHEN Entra-Anmeldung (Bescheinigung, höchstens
    15 Minuten alt). Frisch, damit ein länger liegendes Cookie keinen Faktor
    einrichten kann."""
    if not notbetrieb.funktion_aktiv():
        raise HTTPException(404)
    try:
        daten = _einrichten_signierer().loads(request.cookies.get(EINRICHTEN_COOKIE) or "", max_age=FRISCH_S)
    except (BadSignature, SignatureExpired):
        raise HTTPException(401, "Bitte mit Microsoft anmelden.")
    postfach = notbetrieb.postfach_fuer(str((daten or {}).get("u") or ""))
    if not postfach:
        raise HTTPException(403, "Zu diesem Konto gibt es kein Postfach im Notbetrieb.")
    return postfach


@router.get("/notfall/einrichten", response_class=HTMLResponse)
async def notfall_einrichten_seite(request: Request):
    if not notbetrieb.funktion_aktiv():
        raise HTTPException(404)
    return templates.TemplateResponse(request=request, name="notfall.html",
                                      context={"gateway_name": _gateway_name(), "einrichten": True,
                                               "eingerichtet": notbetrieb_anmeldung.eingerichtet(),
                                               "frisch_min": FRISCH_S // 60})


@router.get("/notfall/api/faktor")
async def notfall_faktor_stand(postfach: str = Depends(_entra_postfach)):
    return JSONResponse({"ok": True, "postfach": postfach, **notbetrieb_faktor.stand(postfach)})


@router.post("/notfall/api/faktor/beginnen")
async def notfall_faktor_beginnen(postfach: str = Depends(_entra_postfach)):
    return JSONResponse({"ok": True, **await asyncio.to_thread(notbetrieb_faktor.totp_beginnen, postfach)})


@router.post("/notfall/api/faktor/bestaetigen")
async def notfall_faktor_bestaetigen(request: Request, postfach: str = Depends(_entra_postfach)):
    try:
        body = await request.json()
    except Exception:                                               # noqa: BLE001
        raise HTTPException(400, "Ungültige Anfrage")
    try:
        codes = await asyncio.to_thread(notbetrieb_faktor.totp_bestaetigen, postfach, str(body.get("code") or ""))
    except notbetrieb_faktor.FaktorFehler as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "codes": codes})


@router.post("/notfall/api/faktor/passkey/beginnen")
async def notfall_passkey_beginnen(request: Request, postfach: str = Depends(_entra_postfach)):
    try:
        optionen = await asyncio.to_thread(notbetrieb_faktor.passkey_beginnen, postfach, _seiten_host(request))
    except notbetrieb_faktor.FaktorFehler as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "optionen": optionen})


@router.post("/notfall/api/faktor/passkey/bestaetigen")
async def notfall_passkey_bestaetigen(request: Request, postfach: str = Depends(_entra_postfach)):
    try:
        body = await request.json()
    except Exception:                                               # noqa: BLE001
        raise HTTPException(400, "Ungültige Anfrage")
    antwort = body.get("antwort") if isinstance(body.get("antwort"), dict) else {}
    try:
        codes = await asyncio.to_thread(notbetrieb_faktor.passkey_bestaetigen, postfach, antwort,
                                        str(body.get("name") or ""))
    except notbetrieb_faktor.FaktorFehler as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "codes": codes})


@router.post("/notfall/api/faktor/passkey/entfernen")
async def notfall_passkey_entfernen(request: Request, postfach: str = Depends(_entra_postfach)):
    try:
        body = await request.json()
    except Exception:                                               # noqa: BLE001
        raise HTTPException(400, "Ungültige Anfrage")
    if not await asyncio.to_thread(notbetrieb_faktor.passkey_entfernen, postfach, str(body.get("id") or "")):
        return JSONResponse({"ok": False, "error": "Diesen Passkey gibt es nicht."}, status_code=404)
    return JSONResponse({"ok": True})


@router.post("/notfall/api/faktor/codes")
async def notfall_faktor_codes(postfach: str = Depends(_entra_postfach)):
    try:
        codes = await asyncio.to_thread(notbetrieb_faktor.codes_erneuern, postfach)
    except notbetrieb_faktor.FaktorFehler as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "codes": codes})


@router.post("/notfall/abmelden")
async def notfall_abmelden():
    antwort = JSONResponse({"ok": True})
    antwort.delete_cookie(COOKIE, path="/notfall")
    return antwort


@router.get("/notfall/api/ich")
async def notfall_ich(benutzer: str = Depends(_benutzer)):
    if not inhalte_erlaubt(benutzer):
        # Bereitschaftsmodus: Anmeldung klappt, Inhalte erst im Notfall.
        letzter = (notbetrieb.status().get("letzter_lauf") or {}).get("ts")
        return JSONResponse({"ok": True, "benutzer": benutzer, "bereit_modus": True,
                             "letzter_abgleich": letzter, "zugriff": [], "absender": []})
    zugriff = [p for p in await asyncio.to_thread(notbetrieb_rechte.zugriff, benutzer) if p["voll"]]
    sendeweg = notbetrieb.sendeweg(benutzer)
    senden = sendeweg is not None
    return JSONResponse({"ok": True, "benutzer": benutzer, "bereit_modus": False, "zugriff": zugriff,
                         "senden": senden, "sendeweg": sendeweg, "vorschau": None if notbetrieb.ausgerufen() else
                         await asyncio.to_thread(_vorschau, [p["postfach"] for p in zugriff]),
                         "absender": await asyncio.to_thread(notbetrieb_senden.absender, benutzer) if senden else []})


def _vorschau(postfaecher: list[str]) -> dict:
    """Für den Hinweis im Normalbetrieb: Stand der Kopie und je Postfach, wie
    viele Tage Post sie hält — damit niemand die Kopie für das Postfach hält."""
    stand = (notbetrieb.status().get("letzter_lauf") or {}).get("ts")
    tage = {p["postfach"].lower(): p["mail"] for p in notbetrieb.postfach_uebersicht()}
    return {"stand": stand, "tage": {pf: tage.get(pf.lower()) for pf in postfaecher}}


@router.get("/notfall/api/ordner")
async def notfall_ordner(postfach: str, benutzer: str = Depends(_leser)):
    pf = _postfach(benutzer, postfach)
    return JSONResponse({"ok": True, "ordner": await asyncio.to_thread(notbetrieb_lesen.ordner, pf)})


@router.get("/notfall/api/kalender")
async def notfall_kalender(postfach: str, von: str, bis: str, benutzer: str = Depends(_leser)):
    """Termine (nur lesend). Private Termine eines fremden Postfachs nur als Zeitblock."""
    import notbetrieb_pim
    pf = _postfach(benutzer, postfach)
    eigen = any(p["postfach"] == pf and p["eigen"] for p in await asyncio.to_thread(notbetrieb_rechte.zugriff, benutzer))
    return JSONResponse({"ok": True, "termine": await asyncio.to_thread(
        notbetrieb_pim.termine, pf, von[:25], bis[:25], eigen)})


@router.get("/notfall/api/kontakte")
async def notfall_kontakte(postfach: str, benutzer: str = Depends(_leser)):
    import notbetrieb_pim
    pf = _postfach(benutzer, postfach)
    return JSONResponse({"ok": True, "kontakte": await asyncio.to_thread(notbetrieb_pim.kontakte, pf),
                         "recht_fehlt": await asyncio.to_thread(notbetrieb_pim.kontakte_recht_fehlt)})


@router.get("/notfall/api/adressen")
async def notfall_adressen(q: str = "", benutzer: str = Depends(_leser)):
    """Adresshilfe beim Verfassen: eigene Kontakte, dann die globale Adressliste."""
    import notbetrieb_pim
    return JSONResponse({"ok": True, "treffer": await asyncio.to_thread(
        notbetrieb_pim.adressen_suchen, benutzer, q[:100])})


@router.get("/notfall/api/liste")
async def notfall_liste(postfach: str, ordner: str = "", suche: str = "", seite: int = 0,
                        benutzer: str = Depends(_leser)):
    pf = _postfach(benutzer, postfach)
    return JSONResponse({"ok": True, **await asyncio.to_thread(notbetrieb_lesen.liste, pf, ordner,
                                                               suche[:200], seite)})


@router.get("/notfall/api/mail")
async def notfall_mail(postfach: str, id: str, benutzer: str = Depends(_leser)):
    pf = _postfach(benutzer, postfach)
    m = await asyncio.to_thread(notbetrieb_lesen.mail, pf, id)
    if not m:
        raise HTTPException(404)
    await asyncio.to_thread(notbetrieb_lesen.als_gelesen, benutzer, pf, id)
    return JSONResponse({"ok": True, **m})


@router.get("/notfall/inhalt")
async def notfall_inhalt(postfach: str, id: str, benutzer: str = Depends(_leser)):
    """HTML der Mail — NUR mit durchgesetzter CSP (notbetrieb_lesen.INHALT_CSP)."""
    pf = _postfach(benutzer, postfach)
    inhalt = await asyncio.to_thread(notbetrieb_lesen.inhalt_html, pf, id)
    if inhalt is None:
        raise HTTPException(404)
    return HTMLResponse(inhalt, headers={"Content-Security-Policy": notbetrieb_lesen.INHALT_CSP,
                                         "X-Frame-Options": "SAMEORIGIN",
                                         "Cache-Control": "no-store"})


@router.get("/notfall/anhang")
async def notfall_anhang(postfach: str, id: str, nr: int, benutzer: str = Depends(_leser)):
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
async def notfall_vorlage(postfach: str, id: str, art: str, benutzer: str = Depends(_leser)):
    """Vorbelegung für Antworten / Allen antworten / Weiterleiten."""
    pf = _postfach(benutzer, postfach)
    try:
        return JSONResponse({"ok": True, **await asyncio.to_thread(notbetrieb_senden.vorlage, benutzer, pf, id, art)})
    except notbetrieb_senden.SendeFehler as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)


_MAX_RUMPF = 22 * 1024 * 1024      # 15 MB Anhänge, base64 (×4/3) plus Text


@router.post("/notfall/api/senden")
async def notfall_senden(request: Request, benutzer: str = Depends(_sender)):
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
    if body.get("entwurf"):
        await asyncio.to_thread(notbetrieb_entwuerfe.loeschen, benutzer, str(body["entwurf"]))
    return JSONResponse(erg)


# ── Löschen und Verschieben (in der Kopie, zurück nach Exchange) ────────────

@router.post("/notfall/api/verschieben")
async def notfall_verschieben(request: Request, benutzer: str = Depends(_sender)):
    """Ziel: Ordner-Kennung des Postfachs oder „deleteditems“ (Löschen).
    Nur wer im Webmail arbeitet (_sender) — in der reinen Vorschau ändert
    niemand sein echtes Postfach."""
    import notbetrieb_aenderungen
    try:
        body = await request.json()
    except Exception:                                               # noqa: BLE001
        raise HTTPException(400, "Ungültige Anfrage")
    pf = _postfach(benutzer, str(body.get("postfach") or ""))
    ziel = await asyncio.to_thread(notbetrieb_lesen.ordner_kennung, pf, str(body.get("ziel") or ""))
    if not ziel:
        return JSONResponse({"ok": False, "error": "Diesen Ordner gibt es nicht."}, status_code=400)
    if not await asyncio.to_thread(notbetrieb_aenderungen.merken, benutzer, pf, str(body.get("id") or ""),
                                   notbetrieb_aenderungen.VERSCHIEBEN, ziel):
        raise HTTPException(404)
    return JSONResponse({"ok": True})


# ── Entwürfe (automatisch gesichert, je angemeldetem Benutzer) ──────────────

@router.get("/notfall/api/entwuerfe")
async def notfall_entwuerfe(benutzer: str = Depends(_sender)):
    return JSONResponse({"ok": True, "entwuerfe": await asyncio.to_thread(notbetrieb_entwuerfe.liste, benutzer)})


@router.get("/notfall/api/entwurf")
async def notfall_entwurf(id: str, benutzer: str = Depends(_sender)):
    e = await asyncio.to_thread(notbetrieb_entwuerfe.laden, benutzer, id)
    if not e:
        raise HTTPException(404)
    return JSONResponse({"ok": True, **e})


@router.post("/notfall/api/entwurf")
async def notfall_entwurf_speichern(request: Request, benutzer: str = Depends(_sender)):
    try:
        body = await request.json()
    except Exception:                                               # noqa: BLE001
        raise HTTPException(400, "Ungültige Anfrage")
    try:
        return JSONResponse({"ok": True, **await asyncio.to_thread(
            notbetrieb_entwuerfe.speichern, benutzer, str(body.get("id") or ""), body)})
    except notbetrieb_entwuerfe.EntwurfFehler as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@router.post("/notfall/api/entwurf/loeschen")
async def notfall_entwurf_loeschen(request: Request, benutzer: str = Depends(_sender)):
    try:
        body = await request.json()
    except Exception:                                               # noqa: BLE001
        raise HTTPException(400, "Ungültige Anfrage")
    await asyncio.to_thread(notbetrieb_entwuerfe.loeschen, benutzer, str(body.get("id") or ""))
    return JSONResponse({"ok": True})
