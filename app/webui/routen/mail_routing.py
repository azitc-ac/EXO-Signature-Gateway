"""Einstellungen → Mail-Routing — Seite und Direktversand-Schnittstelle.

Die Einrichtung des Routings (Weiterleitungsziele, Routen je Domäne/Tenant,
Direktversand, Hybrid-Koexistenz) stand bis v1.12.17 als Karten auf der
SMTP-Relay-Seite und war nur sichtbar, wenn das Relay aktiv UND der
Anzeige-Schalter „Domänen-Routing“ gesetzt war. Wer den Direktversand für
etwas anderes brauchte, kam an die Einrichtung nicht heran. Jetzt eine
eigene Einstellungsseite, unabhängig vom Relay.

Die Routing-Endpunkte unter /api/relay/* bleiben in `relay.py` (Pfade
unverändert); hier stehen die Seite und /api/direkt/*.

⚠️ Alles hinter `_require_admin` — wer hier schreibt, entscheidet, wohin Post
das Unternehmen verlässt.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

import dkim_signatur
import direktversand
import settings_store

from webui.deps import templates, log, _gateway_name, _require_admin

router = APIRouter()


@router.get("/settings/routing", response_class=HTMLResponse)
async def settings_routing_page(request: Request, user: str = Depends(_require_admin)):
    return templates.TemplateResponse(
        request=request, name="settings_routing.html",
        context={"active": "settings-routing", "gateway_name": _gateway_name(),
                 "s": settings_store.public_view()},
    )


# ── Direktversand (Postfix-Container) ────────────────────────────────────────

@router.post("/api/direkt/einstellungen")
async def api_direkt_einstellungen(request: Request, user: str = Depends(_require_admin)):
    """Betriebsart, HELO, Smarthost und Hauptschalter des Direktversands.
    Einschalten per MX verlangt einen bestandenen Port-25-Test (in
    `direktversand.speichern`, nicht nur hier) — blockierend, daher im Thread."""
    import asyncio
    daten = await request.json()
    try:
        e = await asyncio.to_thread(direktversand.speichern, daten)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    log.info("Direktversand durch %s gespeichert: aktiv=%s modus=%s",
             user, e.get("aktiv"), e.get("modus"))
    return JSONResponse({"ok": True, "direkt": e})


@router.post("/api/direkt/port25-test")
async def api_direkt_port25_test(user: str = Depends(_require_admin)):
    """Ist ausgehender Port 25 offen? Nur Verbindung + Begrüßung, keine Mail."""
    import asyncio
    return JSONResponse(await asyncio.to_thread(direktversand.port25_test))


@router.post("/api/direkt/dkim")
async def api_direkt_dkim(request: Request, user: str = Depends(_require_admin)):
    """DKIM-Schlüssel für eine Domäne erzeugen (neuer Selektor je Aufruf)."""
    daten = await request.json()
    try:
        eintrag = dkim_signatur.schluessel_erzeugen(daten.get("domain") or "",
                                                    daten.get("selektor") or "")
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    log.info("DKIM-Schlüssel für %s (Selektor %s) durch %s erzeugt",
             eintrag.get("domain"), eintrag.get("selektor"), user)
    return JSONResponse({"ok": True, "eintrag": eintrag})


@router.post("/api/direkt/dkim/loeschen")
async def api_direkt_dkim_loeschen(request: Request, user: str = Depends(_require_admin)):
    daten = await request.json()
    weg = dkim_signatur.entfernen(daten.get("domain") or "")
    if weg:
        log.info("DKIM für %s durch %s entfernt", daten.get("domain"), user)
    return JSONResponse({"ok": weg})


@router.get("/api/direkt/dns-pruefung")
async def api_direkt_dns_pruefung(domain: str = "", user: str = Depends(_require_admin)):
    """SPF, DKIM-Eintrag und PTR so, wie ein Empfänger sie sieht."""
    import asyncio
    return JSONResponse({"ok": True, **await asyncio.to_thread(direktversand.dns_pruefen, domain)})


# ── Tägliche Probe des Direktversands ────────────────────────────────────────

def _probe_antwort(st: dict) -> dict:
    import direktversand_probe
    ampel, text = direktversand_probe.bewertung(st)
    return {"ok": True, "ampel": ampel, "text": text, "stand": st,
            "postfach": direktversand_probe.postfach(), "aktiv": direktversand.aktiv()}


@router.get("/api/direkt/probe")
async def api_direkt_probe_stand(user: str = Depends(_require_admin)):
    """Stand der letzten Probe. Prüft dabei die Ankunft einer gesendeten Probe
    (Graph, blockierend — im Thread), damit die Seite nicht auf den Takt wartet."""
    import asyncio
    import direktversand_probe
    st = direktversand_probe.stand()
    if direktversand.aktiv() and direktversand_probe.postfach():
        try:
            st = await asyncio.to_thread(direktversand_probe.pruefen)
        except Exception as exc:                                    # noqa: BLE001
            log.warning("Direktversand-Probe: Prüfung fehlgeschlagen: %s", exc)
    return JSONResponse(_probe_antwort(st))


@router.post("/api/direkt/probe")
async def api_direkt_probe_senden(user: str = Depends(_require_admin)):
    """Probe sofort senden. 409 ohne eingeschalteten Direktversand, 400 ohne Postfach."""
    import asyncio
    import direktversand_probe
    if not direktversand.aktiv():
        return JSONResponse({"ok": False, "error": "Selbst zustellen ist nicht eingeschaltet."},
                            status_code=409)
    if not direktversand_probe.postfach():
        return JSONResponse({"ok": False, "error": "Kein Probe-Postfach eingetragen."},
                            status_code=400)
    st = await asyncio.to_thread(direktversand_probe.senden)
    log.info("Direktversand-Probe durch %s gestartet: %s", user, st.get("zustand"))
    return JSONResponse(_probe_antwort(st))
