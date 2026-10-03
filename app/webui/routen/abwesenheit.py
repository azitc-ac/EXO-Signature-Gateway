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

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

import abwesenheit
import graph_client
import mailbox_match
import settings_store
from webui.deps import templates, log, _gateway_name, _require_admin

router = APIRouter()


def _postfach_key(email: str) -> tuple[str, dict]:
    """(config_key, sender_cfg) für ein VERWALTETES Postfach. 404 sonst.

    ⚠️ Anders als im Self-Service darf der Admin die Adresse frei wählen — sie wird
    hier aber hart gegen MAILBOX_CONFIG geprüft (nur aktivierte Postfächer)."""
    mb = settings_store.get("MAILBOX_CONFIG") or {}
    key = mailbox_match.match_sender_key(mb, email)
    if not key or key not in mb:
        raise HTTPException(404, "Kein im Gateway verwaltetes Postfach.")
    return key, dict(mb[key])


@router.get("/abwesenheit", response_class=HTMLResponse)
async def abwesenheit_page(request: Request, _user: str = Depends(_require_admin)):
    return templates.TemplateResponse(
        request=request, name="abwesenheit.html",
        context={
            "active": "abwesenheit",
            "gateway_name": _gateway_name(),
            "ooo_enabled": bool(settings_store.get("OOO_ENABLED")),
            # OOF-/Kalender-Betreiberschalter (von der Postfächer-Seite hierher gezogen).
            "ooo_calendar_auto": bool(settings_store.get("OOO_CALENDAR_AUTO")),
            "ooo_calendar_min_hours": int(settings_store.get("OOO_CALENDAR_MIN_HOURS") or 8),
            "ooo_calendar_refresh_hours": int(settings_store.get("OOO_CALENDAR_REFRESH_HOURS") or 6),
            "ooo_append_signature": bool(settings_store.get("OOO_APPEND_SIGNATURE")),
            "ooo_append_banner": bool(settings_store.get("OOO_APPEND_BANNER")),
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


# ── Zentrale Einstellung je Nutzer ────────────────────────────────────────────

@router.get("/api/abwesenheit/postfach")
async def api_abwesenheit_postfach(email: str, _user: str = Depends(_require_admin)):
    """Aktueller Abwesenheits-Stand eines Postfachs für den Admin-Editor."""
    email = (email or "").strip().lower()
    key, sender_cfg = _postfach_key(email)
    mb = settings_store.get("MAILBOX_CONFIG") or {}
    oof_eff = abwesenheit.oof_vorlage_fuer(email, mb, sender_cfg)
    ank = abwesenheit._ankuendigung_einstellungen(sender_cfg)
    daten = {
        "email": email,
        "oof_template": oof_eff,                      # zugewiesene Vorlage (nur Anzeige)
        "kalender": sender_cfg.get("ooo_calendar"),   # tri-state None/True/False
        "announce_supported": abwesenheit._vorlage_nutzt_ankuendigung(oof_eff),
        "announce": {"an": ank["an"], "mode": ank["mode"], "x": ank["x"],
                     "privat": ank["privat"], "extern": ank["extern"]},
        "ooo": {"status": "disabled", "start": "", "ende": "",
                "start_zeit": "09:00", "ende_zeit": "17:00", "ganztaegig": True},
        "zugriff": True,
    }
    token = await graph_client._acquire_token_async()
    if token:
        status_lese, setting = await abwesenheit._get_setting(email, token)
        if status_lese == "ok" and setting:
            daten["ooo"]["status"] = setting.get("status", "disabled")
            sd, sz = abwesenheit.datum_zeit(setting.get("scheduledStartDateTime"))
            ed, ez = abwesenheit.datum_zeit(setting.get("scheduledEndDateTime"))
            daten["ooo"]["start"], daten["ooo"]["ende"] = sd, ed
            ganz = sz in ("", "00:00") and ez in ("", "23:59")
            daten["ooo"]["ganztaegig"] = ganz
            if not ganz:
                daten["ooo"]["start_zeit"] = sz or "09:00"
                daten["ooo"]["ende_zeit"] = ez or "17:00"
        elif status_lese == "kein_zugriff":
            daten["zugriff"] = False
    else:
        daten["zugriff"] = False
    return JSONResponse(daten)


@router.post("/api/abwesenheit/postfach")
async def api_abwesenheit_postfach_setzen(request: Request, _user: str = Depends(_require_admin)):
    """Admin setzt für ein Postfach zentral: OOF-Status + Zeitraum, Kalender-Automatik
    und die Ankündigungs-Einstellungen. Die zugewiesene Vorlage bleibt unberührt
    (sie wird auf der Postfächer-Seite/über Richtlinien vergeben)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Ungültige Anfrage")
    email = (body.get("email") or "").strip().lower()
    key, _cfg0 = _postfach_key(email)
    status = (body.get("oof_status") or "disabled").strip()
    if status not in ("disabled", "alwaysEnabled", "scheduled"):
        raise HTTPException(400, "Ungültiger Status")
    start = (body.get("oof_start") or "").strip()
    ende = (body.get("oof_ende") or "").strip()
    ganztaegig = body.get("ganztaegig", True) is not False
    start_zeit = "" if ganztaegig else (body.get("start_zeit") or "").strip()
    ende_zeit = "" if ganztaegig else (body.get("ende_zeit") or "").strip()

    # 1) Konfig schreiben (Kalender-Automatik + Ankündigung) — EIN Schreibvorgang.
    voll = settings_store.get("MAILBOX_CONFIG") or {}
    eintrag = dict(voll.get(key, {}))
    cal = body.get("ooo_calendar")
    if cal in ("1", "0"):
        eintrag["ooo_calendar"] = (cal == "1")
    elif cal in ("", None):
        eintrag.pop("ooo_calendar", None)          # „Vorgabe" → kein Postfach-Wert
    abwesenheit.ankuendigung_ins_eintrag(eintrag, body.get("announce") or {})
    voll[key] = eintrag
    settings_store.update({"MAILBOX_CONFIG": voll})

    # 2) OOF bei Exchange setzen (Status + Zeitraum + korrekt gerenderter Text).
    token = await graph_client._acquire_token_async()
    if not token:
        raise HTTPException(503, "Kein Graph-Zugriff möglich.")
    status_lese, setting = await abwesenheit._get_setting(email, token)
    if status_lese == "kein_zugriff":
        raise HTTPException(403, "Kein Zugriff auf die Postfacheinstellungen (Consent fehlt).")
    if status_lese != "ok" or setting is None:
        raise HTTPException(502, "Postfacheinstellungen nicht lesbar.")

    synth = abwesenheit.synth_setting(status, start, ende, start_zeit, ende_zeit)
    tpl = abwesenheit.oof_vorlage_fuer(email, voll, eintrag)
    _ab, _bis = abwesenheit.start_ende_text(synth)
    if tpl:
        user_data = await graph_client.get_user(email)
        html, html_extern = await abwesenheit.render_intern_extern(
            user_data, email, token, tpl, eintrag, abwesenheit._state(),
            abwesenheit.zeitraum_text(synth), _ab, _bis)
    else:
        # Keine Vorlage zugewiesen → den vorhandenen Text NICHT leeren, nur schalten.
        html = setting.get("internalReplyMessage") or ""
        html_extern = setting.get("externalReplyMessage") or html
    try:
        await abwesenheit._patch_setting(
            email, token, setting, html, html_extern=html_extern, status=status,
            start=synth.get("scheduledStartDateTime"), ende=synth.get("scheduledEndDateTime"))
    except Exception as exc:                                       # noqa: BLE001
        log.warning("Admin-OOF-PATCH fehlgeschlagen für %s: %s", email, exc)
        raise HTTPException(502, "Abwesenheit konnte bei Exchange nicht gesetzt werden.")
    log.info("Admin hat Abwesenheit für %s gesetzt (status=%s)", email, status)
    return JSONResponse({"ok": True})
