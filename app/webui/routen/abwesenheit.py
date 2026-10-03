"""Admin-Dashboard „Abwesenheit": Übersicht, wer gerade abwesend ist, plus die
betreiberweiten Vorgaben für die Ankündigung künftiger Abwesenheiten.

Die Übersicht speist sich aus einem gecachten Status-Scan (abwesenheit.
status_uebersicht) — ein Graph-Lauf über alle aktivierten Postfächer, nebenläufig
und mit TTL, damit auch viele hundert Postfächer das Laden nicht sprengen. Die
per-Postfach-Konfiguration (zugewiesene Vorlage, Kalender-Automatik, Ankündigung)
wird aus MAILBOX_CONFIG angereichert, ohne weitere Graph-Aufrufe.

⚠️ Das eigentliche zentrale SETZEN pro Nutzer (OOF an/aus, Zeitraum, Vorlage …)
ist die nächste Stufe; hier steht zunächst die Übersicht + die Vorgaben.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

import abwesenheit
import mailbox_match
import settings_store
from webui.deps import templates, _gateway_name, _require_admin

router = APIRouter()


@router.get("/abwesenheit", response_class=HTMLResponse)
async def abwesenheit_page(request: Request, _user: str = Depends(_require_admin)):
    return templates.TemplateResponse(
        request=request, name="abwesenheit.html",
        context={
            "active": "abwesenheit",
            "gateway_name": _gateway_name(),
            "ooo_enabled": bool(settings_store.get("OOO_ENABLED")),
            # Betreiberweite Vorgaben für die Ankündigung (je Postfach übersteuerbar).
            "ann_mode": settings_store.get("OOO_ANNOUNCE_MODE") or "anzahl",
            "ann_x": int(settings_store.get("OOO_ANNOUNCE_X") or 3),
            "ann_privat": bool(settings_store.get("OOO_ANNOUNCE_PRIVAT")),
            "ann_extern": bool(settings_store.get("OOO_ANNOUNCE_EXTERN")),
        },
    )


@router.get("/api/abwesenheit/uebersicht")
async def api_abwesenheit_uebersicht(force: int = 0, _user: str = Depends(_require_admin)):
    """Status-Übersicht aller aktivierten Postfächer, angereichert um die
    per-Postfach-Konfiguration (Vorlage/Kalender/Ankündigung). `force=1` liest frisch."""
    daten = await abwesenheit.status_uebersicht(force=bool(force))
    mb = settings_store.get("MAILBOX_CONFIG") or {}
    items = []
    for it in daten.get("items", []):
        upn = it["upn"]
        sender_cfg = mailbox_match.match_sender(mb, upn)
        tpl = abwesenheit.oof_vorlage_fuer(upn, mb, sender_cfg)
        ank = abwesenheit._ankuendigung_einstellungen(sender_cfg)
        items.append({
            **it,
            "name": sender_cfg.get("display_name") or upn,
            "oof_template": tpl,
            # tri-state: None=Vorgabe, True=an, False=aus
            "kalender": sender_cfg.get("ooo_calendar"),
            # Ankündigung wirkt nur, wenn an UND die Vorlage die Variable nutzt.
            "ankuendigung": bool(ank["an"] and abwesenheit._vorlage_nutzt_ankuendigung(tpl)),
        })
    # Nach „gerade abwesend" zuerst, dann Name — die Übersicht soll die Abwesenden zeigen.
    items.sort(key=lambda i: (not i.get("abwesend"), (i.get("name") or "").lower()))
    return JSONResponse({
        "ts": daten.get("ts"),
        "gesamt": daten.get("gesamt", 0),
        "abwesend": daten.get("abwesend", 0),
        "kein_token": bool(daten.get("kein_token")),
        "ooo_enabled": bool(settings_store.get("OOO_ENABLED")),
        "items": items,
    })
