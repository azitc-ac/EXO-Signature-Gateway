"""Notbetrieb „Exchange-Schatten“: Stand, Abgleich, Rückspielen, Löschen.

Eingeschaltet wird im Assistenten (Abschnitt „Modus & Funktionen“, Schalter
NOTBETRIEB_AKTIV); bedient auf der eigenen Seite /notbetrieb. Die Logik liegt
in `notbetrieb.py`; diese Endpunkte reichen nur durch. Die Graph-Aufrufe dort sind synchron und laufen
deshalb in einem Thread, damit die Oberfläche bedienbar bleibt.
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import domain_routing
import graph_client
import gruppen
import aussenadresse
import notbetrieb
import notbetrieb_eingang
import notbetrieb_anmeldung
import notbetrieb_faktor
import notbetrieb_rechte
import settings_store
from webui.deps import templates, log, _gateway_name, _require_admin

router = APIRouter()


def edge_richtlinie(webmail_url: str) -> str:
    """Wert der Edge-Richtlinie WebAppInstallForceList (GPO/Intune): installiert
    die Notfall-Mail still als App mit eigenem Fenster. Felder nach
    learn.microsoft.com „WebAppInstallForceList“ (Edge ≥ 80): `url` Pflicht,
    `default_launch_container` „window“, `create_desktop_shortcut`."""
    return json.dumps([{"url": webmail_url, "default_launch_container": "window",
                        "create_desktop_shortcut": True}])


async def teams_notfall_stand() -> dict | None:
    """Teams-App „Notfall-Mail“: Fassung des Pakets und — wenn lesbar — die im
    Teams-Katalog (wie die Self-Service-App auf der Einrichtung). Ohne
    Außenadresse kein Paket: Der Reiter bräuchte eine erreichbare Adresse."""
    basis = aussenadresse.basis()
    if not basis:
        return None
    from webui.routen.addin import _notfall_teams_manifest, _versionstupel
    try:
        m = _notfall_teams_manifest(basis)
        stand = {"version": m["version"],
                 "seit": (settings_store.get("_TEAMS_NOTFALL_STAND") or {}).get("seit", ""),
                 "katalog": await graph_client.teams_katalog(m["id"])}
        v = stand["katalog"].get("version")
        stand["katalog"]["aktuell"] = bool(v) and _versionstupel(v) >= _versionstupel(m["version"])
        return stand
    except Exception as exc:                                   # noqa: BLE001
        log.warning("Notbetrieb: Teams-App „Notfall-Mail“ nicht ermittelt: %s", exc)
        return None


@router.get("/notbetrieb/notfall-teams-app.zip")
async def notfall_teams_app(_user: str = Depends(_require_admin)):
    """Paket der Teams-App „Notfall-Mail“ für Integrierte Apps / Teams Admin Center."""
    basis = aussenadresse.basis()
    if not basis:
        raise HTTPException(409, "Keine Außenadresse eingestellt — der Reiter bräuchte eine erreichbare Adresse.")
    from webui.routen.addin import notfall_teams_paket
    paket, m = notfall_teams_paket(basis)
    return Response(content=paket, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="notfall-mail-{m["version"]}.zip"',
                             "Cache-Control": "no-store"})


@router.get("/notbetrieb", response_class=HTMLResponse)
async def notbetrieb_page(request: Request, _user: str = Depends(_require_admin)):
    # Aus UND keine Kopie → kein versteckter Zugang über die URL; zum Schalter
    # führen. Liegt noch eine Kopie, bleibt die Seite für Zurückspielen/Löschen.
    if not notbetrieb.sichtbar():
        return RedirectResponse("/setup#step-mode", status_code=303)
    regeln = settings_store.get("NOTBETRIEB_REGELN") or []
    webmail_url = aussenadresse.basis() + "/notfall"
    # Kopie und Faktor-Datei tragen Sperren: im Thread, nicht in der Event-Loop.
    postfaecher = await asyncio.to_thread(notbetrieb._postfaecher)
    faktor_abdeckung = await asyncio.to_thread(notbetrieb_faktor.abdeckung)
    return templates.TemplateResponse(
        request=request, name="notbetrieb.html",
        context={
            "active": "notbetrieb",
            "aktiv": notbetrieb.funktion_aktiv(),
            "ausgerufen": notbetrieb.ausgerufen(),
            "probegruppe": settings_store.get("NOTBETRIEB_PROBEGRUPPE") or [],
            "lesen_alle": notbetrieb.lesen_alle(),
            "senden_alle": settings_store.get("NOTBETRIEB_SENDEN_ALLE") is True,
            "webmail_url": webmail_url,
            "edge_richtlinie": edge_richtlinie(webmail_url),
            "teams_notfall": await teams_notfall_stand(),
            "faktor_abdeckung": faktor_abdeckung,
            "zweitfaktor": {"pflicht": notbetrieb_faktor.pflicht(), "notfall_ohne": notbetrieb_faktor.im_notfall_ohne()},
            "imap_host": aussenadresse.ehlo_hostname() or "",
            "eingang": {"greylist": notbetrieb_eingang.greylisting_an(), "max_empf": notbetrieb_eingang.max_empfaenger(),
                        "max_mb": notbetrieb_eingang.max_bytes() // (1024 * 1024),
                        "je_stunde": notbetrieb_eingang.je_stunde()},
            "gateway_name": _gateway_name(),
            "fenster": notbetrieb.standard_fenster(),
            "regeln": regeln if isinstance(regeln, list) else [],
            "gruppen": sorted(gruppen.interne_gruppen()),
            "postfaecher": [upn for _key, upn in postfaecher],
            "max_tage": notbetrieb.MAX_TAGE,
            "mess_tage": notbetrieb.MESS_TAGE,
            "ldap": notbetrieb_anmeldung.einstellungen(),
            "umfang": notbetrieb.umfang(),
            "onprem": await asyncio.to_thread(notbetrieb.onprem_stand),
            "onprem_ziel": str(settings_store.get("NOTBETRIEB_ONPREM_ZIEL") or ""),
            "routing_ziele": sorted(domain_routing.ziele()),
        },
    )


@router.get("/api/notbetrieb/bereitschaft")
async def api_notbetrieb_bereitschaft(_user: str = Depends(_require_admin)):
    """Bereitschaft samt Netzprüfungen (Domänencontroller, MX) — auf Abruf,
    nicht bei jedem Seitenaufbau."""
    import notbetrieb_bereitschaft
    erg = await asyncio.to_thread(notbetrieb_bereitschaft.pruefen, True)
    erg["mx"] = await asyncio.to_thread(notbetrieb_bereitschaft.mx_stand)
    return JSONResponse({"ok": True, **erg})


@router.post("/api/notbetrieb/onprem-probe")
async def api_notbetrieb_onprem_probe(user: str = Depends(_require_admin)):
    """Weg zum On-Prem-Ziel prüfen: Verbindung bis RCPT, ohne DATA."""
    erg = await asyncio.to_thread(notbetrieb.onprem_probe)
    log.info("Notbetrieb: On-Prem-Probe durch %s: %s", user, erg)
    return JSONResponse({**erg, "stand": await asyncio.to_thread(notbetrieb.onprem_stand)})


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


@router.get("/api/notbetrieb/faktoren")
async def api_notbetrieb_faktoren(_user: str = Depends(_require_admin)):
    """Wer hat einen zweiten Faktor (ohne Geheimnisse)."""
    return JSONResponse({"ok": True, "items": await asyncio.to_thread(notbetrieb_faktor.uebersicht)})


@router.post("/api/notbetrieb/faktoren/zuruecksetzen")
async def api_notbetrieb_faktor_zuruecksetzen(request: Request, user: str = Depends(_require_admin)):
    try:
        postfach = str((await request.json()).get("postfach") or "").strip().lower()
    except Exception:                                               # noqa: BLE001
        raise HTTPException(400, "Ungültige Anfrage")
    if not await asyncio.to_thread(notbetrieb_faktor.zuruecksetzen, postfach, user):
        return JSONResponse({"ok": False, "error": "Für dieses Postfach ist kein zweiter Faktor eingerichtet."},
                            status_code=404)
    return JSONResponse({"ok": True})


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
