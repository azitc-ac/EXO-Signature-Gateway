"""Self-Service: Postfach-Nutzer verwalten ihre EIGENE Abwesenheit und ihre
eigene Standard-Signaturvorlage — ohne Admin-Zugang.

Anlass: Der native Outlook-OOF-Dialog rendert die Vorschau kaputt (WordMail-
Spaltenkollaps), obwohl das Ergebnis gut ist — irreführend. Diese Seite zeigt
die KORREKTE Vorschau (dieselbe Pipeline wie /preview) und erlaubt An/Aus,
Zeitraum und Vorlagenwahl.

⚠️ SICHERHEIT: Jede Aktion bezieht sich ausschliesslich auf die Adresse aus der
Sitzung (`_require_self` → Token-Identität). Ein `email`-Parameter wird NIE
akzeptiert — sonst könnte Nutzer A das Postfach von Nutzer B verändern.

⚠️ FREIGABE: Schreibende Aktionen nur, wenn der Betreiber `SELF_SERVICE_ENABLED`
gesetzt hat. Ist es aus, ist die Seite reine Anzeige/Vorschau.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

import settings_store
import signature_engine
import mailbox_match
import graph_client
import abwesenheit
from webui.deps import templates, log, _gateway_name, _require_self

router = APIRouter()

_NO_STORE = "no-store, no-cache, must-revalidate, max-age=0"


def _freigeschaltet() -> bool:
    return settings_store.get("SELF_SERVICE_ENABLED") is True


def _darf_vorlagen_waehlen(email: str) -> bool:
    """Darf dieser Nutzer seine OOF-/Signaturvorlage selbst WÄHLEN? Standard: NEIN.

    Freischaltbar pro Postfach (`MAILBOX_CONFIG[...]["self_templates"]=true`) ODER
    pro interner Gruppe (`SELF_TEMPLATE_GROUPS`). An/Aus der Abwesenheit und der
    Zeitraum sind davon unabhängig — nur die Vorlagenwahl ist gated. Server-seitig
    durchgesetzt, nie dem Client geglaubt."""
    mb_all = settings_store.get("MAILBOX_CONFIG") or {}
    sender_cfg = mailbox_match.match_sender(mb_all, email)
    if sender_cfg.get("self_templates") is True:
        return True
    gruppen = settings_store.get("SELF_TEMPLATE_GROUPS") or []
    if gruppen:
        key = mailbox_match.match_sender_key(mb_all, email)
        intern = settings_store.get("INTERNAL_GROUPS") or {}
        for g in gruppen:
            if key and key in (intern.get(g) or []):
                return True
    return False


def _effektive_vorlagen(email: str) -> tuple[str, str]:
    """(oof_template, sig_template), die dem Postfach aktuell ZUGEWIESEN sind — für
    Vorbelegung und Vorschau, wenn der Nutzer nicht selbst wählen darf."""
    import policies as _pol
    mb_all = settings_store.get("MAILBOX_CONFIG") or {}
    sender_cfg = mailbox_match.match_sender(mb_all, email)
    oof = abwesenheit.oof_vorlage_fuer(email, mb_all, sender_cfg)
    pol, use_pol = _pol.resolve_policies(email, mb_all, sender_cfg)
    sig = (pol.get("sig") or "default") if use_pol else (sender_cfg.get("template") or "default")
    return oof, (sig or "default")


def _ankuendigung_ins_eintrag(eintrag: dict, ann: dict) -> None:
    """Ankündigungs-Einstellungen aus dem Self-Service in den Postfach-Eintrag
    schreiben — server-seitig validiert, dem Client nie geglaubt. `ann` ist das
    `announce`-Objekt aus dem Body (bzw. den Vorschau-Parametern)."""
    eintrag["oof_announce"] = (ann.get("an") is not False)
    eintrag["oof_announce_mode"] = "tage" if ann.get("mode") == "tage" else "anzahl"
    try:
        x = int(ann.get("x") or 3)
    except (TypeError, ValueError):
        x = 3
    eintrag["oof_announce_x"] = min(50, max(1, x))
    eintrag["oof_announce_privat"] = (ann.get("privat") is not False)
    eintrag["oof_announce_extern"] = bool(ann.get("extern"))


def _postfach(email: str) -> tuple[str, dict]:
    """(config_key, sender_cfg) für die EIGENE Adresse. 403, wenn das Postfach im
    Gateway nicht verwaltet wird (Self-Service gilt nur für aktivierte Postfächer)."""
    cfg = settings_store.get("MAILBOX_CONFIG") or {}
    key = mailbox_match.match_sender_key(cfg, email)
    if not key or key not in cfg:
        raise HTTPException(403, "Für dieses Postfach ist kein Self-Service verfügbar.")
    return key, dict(cfg[key])


def _als_utc(datum: str, uhrzeit: str) -> dict:
    """Ein lokales Datum (YYYY-MM-DD) + Uhrzeit → Graph-dateTimeTimeZone in UTC.

    Die <input type=date> liefern nur ein Datum; ein Zeitraum meint den GANZEN Tag,
    also lokal 00:00 bis 23:59. „Lokal" = die Anzeige-Zeitzone (LOG_TIMEZONE,
    Vorgabe Europe/Berlin) — dieselbe, in der zeitraum_text die Daten zeigt. Wir
    rechnen die lokale Wand-Uhrzeit in einen eindeutigen UTC-Zeitpunkt um und senden
    timeZone=UTC (Graph-sicher; Windows-/IANA-Namen sind beim Schreiben heikel)."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(settings_store.get("LOG_TIMEZONE") or "UTC")
    except Exception:                                              # noqa: BLE001
        tz = timezone.utc
    lokal = datetime.fromisoformat(f"{datum}T{uhrzeit}").replace(tzinfo=tz)
    utc = lokal.astimezone(timezone.utc)
    return {"dateTime": utc.strftime("%Y-%m-%dT%H:%M:%S.0000000"), "timeZone": "UTC"}


def _synth_setting(status: str, start: str, ende: str,
                   start_zeit: str = "", ende_zeit: str = "") -> dict:
    """Ein automaticRepliesSetting-ähnliches dict aus den Nutzereingaben bauen,
    damit zeitraum_text()/start_ende_text() denselben Text liefern wie im Betrieb.
    `start`/`ende` sind ISO-Datumsangaben (YYYY-MM-DD).

    Ohne Uhrzeiten (ganztägig) gilt lokal `start` 00:00 bis `ende` 23:59. Mit
    `start_zeit`/`ende_zeit` (HH:MM) wird stundengenau geplant (Checkbox „ganztägig"
    aus)."""
    s: dict = {"status": status}
    if status == "scheduled":
        if start:
            s["scheduledStartDateTime"] = _als_utc(start, f"{start_zeit}:00" if start_zeit else "00:00:00")
        if ende:
            s["scheduledEndDateTime"] = _als_utc(ende, f"{ende_zeit}:00" if ende_zeit else "23:59:59")
    return s


async def _render_oof_fuer(email: str, oof_tpl: str, status: str, start: str, ende: str,
                           start_zeit: str = "", ende_zeit: str = "",
                           token: str | None = None,
                           announce: dict | None = None) -> tuple[str, str, str]:
    """(html_intern, html_extern, txt) der Abwesenheit für die eigene Adresse — wie
    im Betrieb (inkl. Ankündigung künftiger Abwesenheiten, falls die Vorlage sie
    nutzt; die Ankündigung steht ggf. nur im internen Text). Ohne `token` wird keine
    Ankündigung gelesen (reine Vorschau ohne Kalenderzugriff). `announce`
    überschreibt die gespeicherten Ankündigungs-Felder für die Live-Vorschau."""
    if not oof_tpl:
        return "", "", ""
    user_data = await graph_client.get_user(email)
    synth = _synth_setting(status or "scheduled", start, ende, start_zeit, ende_zeit)
    _ab, _bis = abwesenheit.start_ende_text(synth)
    z = abwesenheit.zeitraum_text(synth)
    _key, cfg = _postfach(email)
    if announce is not None:
        cfg = dict(cfg)
        _ankuendigung_ins_eintrag(cfg, announce)
    html, html_extern = await abwesenheit.render_intern_extern(
        user_data, email, token, oof_tpl, cfg, abwesenheit._state(), z, _ab, _bis)
    _h, txt = abwesenheit.render_oof(user_data, oof_tpl, z, _ab, _bis)
    return html, html_extern, txt


# ── Seite ──────────────────────────────────────────────────────────────────────

@router.get("/self", response_class=HTMLResponse)
async def self_page(request: Request):
    # Die SEITE ist öffentlich (wie das Add-in-Compose): WebView-/Browser-Navigation
    # kann keinen Auth-Header mitgeben. Die Identität klärt das JS über die
    # API-Endpunkte (Cookie im Browser bzw. X-Addin-Session im Add-in); ohne
    # gültige Sitzung liefern die APIs 401 und die Seite leitet zur Anmeldung.
    resp = templates.TemplateResponse(
        request=request, name="self.html",
        context={"gateway_name": _gateway_name()},
    )
    resp.headers["Cache-Control"] = _NO_STORE   # in Outlook-WebView nie cachen
    return resp


# ── Daten ──────────────────────────────────────────────────────────────────────

@router.get("/api/self/context")
async def self_context(email: str = Depends(_require_self)):
    """Aktueller Stand + Auswahlmöglichkeiten für das EIGENE Postfach."""
    key, cfg = _postfach(email)
    by_kind = signature_engine.templates_nach_art()
    darf = _darf_vorlagen_waehlen(email)
    oof_eff, sig_eff = _effektive_vorlagen(email)
    ank = abwesenheit._ankuendigung_einstellungen(cfg)
    daten = {
        "email": email,
        "freigeschaltet": _freigeschaltet(),
        "darf_vorlagen": darf,          # darf der Nutzer die Vorlagen selbst wählen?
        "oof_template": oof_eff,         # aktuell zugewiesen/gewählt (Vorbelegung)
        "sig_template": sig_eff,
        # Auswahllisten nur, wenn die Wahl freigeschaltet ist — sonst gibt es nichts
        # zu wählen (Standard), und die Oberfläche blendet die Dropdowns aus.
        "oof_templates": by_kind.get("oof", []) if darf else [],
        "sig_templates": by_kind.get("signatur", ["default"]) if darf else [],
        "ooo": {"status": "disabled", "start": "", "ende": "",
                "start_zeit": "09:00", "ende_zeit": "17:00", "ganztaegig": True},
        # Ankündigung künftiger Abwesenheiten: nur relevant, wenn die zugewiesene
        # oof-Vorlage die Variable nutzt (sonst wirkt die Einstellung nicht).
        "announce_supported": abwesenheit._vorlage_nutzt_ankuendigung(oof_eff),
        "announce": {"an": ank["an"], "mode": ank["mode"], "x": ank["x"],
                     "privat": ank["privat"], "extern": ank["extern"]},
        "zugriff": True,
    }
    # Aktuellen OOF-Status aus Exchange lesen (nur Anzeige — kein Schreibzugriff).
    try:
        token = await graph_client._acquire_token_async()
        if token:
            status_lese, setting = await abwesenheit._get_setting(email, token)
            if status_lese == "ok" and setting:
                daten["ooo"]["status"] = setting.get("status", "disabled")
                sd, sz = _datum_zeit(setting.get("scheduledStartDateTime"))
                ed, ez = _datum_zeit(setting.get("scheduledEndDateTime"))
                daten["ooo"]["start"], daten["ooo"]["ende"] = sd, ed
                # Ganztägig, wenn lokal 00:00 bis 23:59 — sonst die echten Uhrzeiten.
                ganz = sz in ("", "00:00") and ez in ("", "23:59")
                daten["ooo"]["ganztaegig"] = ganz
                if not ganz:
                    daten["ooo"]["start_zeit"] = sz or "09:00"
                    daten["ooo"]["ende_zeit"] = ez or "17:00"
            elif status_lese == "kein_zugriff":
                daten["zugriff"] = False
    except Exception as exc:                                       # noqa: BLE001
        log.warning("self_context: OOF-Status lesen fehlgeschlagen für %s: %s", email, exc)
    return JSONResponse(daten)


def _datum_zeit(d: dict | None) -> tuple[str, str]:
    """(YYYY-MM-DD, HH:MM) in lokaler Anzeigezeit aus einer Graph-dateTimeTimeZone."""
    dt = abwesenheit._lokal_datum(d)
    return (f"{dt:%Y-%m-%d}", f"{dt:%H:%M}") if dt else ("", "")


async def _banner_disclaimer_html(email: str) -> tuple[str, str]:
    """(banner_html, disclaimer_html), die das Postfach TATSÄCHLICH bekäme — wie im
    Betrieb aufgelöst (Richtlinie bzw. Postfach-eigene Felder). Nicht nutzerwählbar;
    gehört aber in die Vorschau, damit sie vollständig ist."""
    import policies as _pol
    mb_all = settings_store.get("MAILBOX_CONFIG") or {}
    sender_cfg = mailbox_match.match_sender(mb_all, email)
    pol, use_pol = _pol.resolve_policies(email, mb_all, sender_cfg)
    banner = ((pol.get("banner") or "") if use_pol else sender_cfg.get("banner_template", "")).strip()
    disclaimer = ((pol.get("disclaimer") or "") if use_pol else sender_cfg.get("disclaimer_template", "")).strip()
    if not banner and not disclaimer:
        return "", ""
    user_data = await graph_client.get_user(email)
    b = signature_engine.render(user_data, template_name=banner)[0] if banner else ""
    d = signature_engine.render(user_data, template_name=disclaimer)[0] if disclaimer else ""
    return b, d


@router.get("/api/self/preview")
async def self_preview(oof: str = "", sig: str = "", status: str = "scheduled",
                       start: str = "", ende: str = "",
                       start_zeit: str = "", ende_zeit: str = "",
                       ank_an: str = "1", ank_mode: str = "anzahl", ank_x: str = "3",
                       ank_privat: str = "1", ank_extern: str = "0",
                       email: str = Depends(_require_self)):
    """Korrekte, VOLLSTÄNDIGE Vorschau fürs eigene Postfach: OOF (über der Signatur)
    + Signatur + Banner + Disclaimer (wie die echte Mail). Die ank_*-Parameter
    spiegeln die noch ungespeicherten Ankündigungs-Einstellungen in die Vorschau."""
    # Token (best effort), damit die Vorschau die Ankündigung mitzeigt, falls die
    # Vorlage sie nutzt. Ohne Token zeigt die Vorschau den Text ohne Ankündigung.
    token = await graph_client._acquire_token_async()
    announce = {"an": ank_an != "0", "mode": ank_mode, "x": ank_x,
                "privat": ank_privat != "0", "extern": ank_extern == "1"}
    oof_html, _oof_extern, oof_txt = await _render_oof_fuer(
        email, oof, status, start, ende, start_zeit, ende_zeit, token, announce)
    sig_html, sig_txt = "", ""
    if sig:
        user_data = await graph_client.get_user(email)
        sig_html, sig_txt = signature_engine.render(user_data, template_name=sig)
    banner_html, disclaimer_html = await _banner_disclaimer_html(email)
    return JSONResponse({"oof_html": oof_html, "oof_txt": oof_txt,
                         "sig_html": sig_html, "sig_txt": sig_txt,
                         "banner_html": banner_html, "disclaimer_html": disclaimer_html})


# ── Speichern ──────────────────────────────────────────────────────────────────

@router.post("/api/self/save")
async def self_save(request: Request, email: str = Depends(_require_self)):
    """Eigene Abwesenheit + Signaturvorlage setzen. Nur bei Freigabe."""
    if not _freigeschaltet():
        raise HTTPException(403, "Self-Service ist derzeit nicht freigeschaltet.")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Ungültige Anfrage")
    status = (body.get("oof_status") or "disabled").strip()
    start = (body.get("oof_start") or "").strip()
    ende = (body.get("oof_ende") or "").strip()
    # Uhrzeiten nur, wenn NICHT ganztägig (Checkbox) — sonst leer → ganzer Tag.
    ganztaegig = body.get("ganztaegig", True) is not False
    start_zeit = "" if ganztaegig else (body.get("start_zeit") or "").strip()
    ende_zeit = "" if ganztaegig else (body.get("ende_zeit") or "").strip()
    if status not in ("disabled", "alwaysEnabled", "scheduled"):
        raise HTTPException(400, "Ungültiger Status")

    # 1) Postfach-eigene Einstellungen schreiben — in EINEM Schreibvorgang:
    #    (a) Vorlagenwahl NUR, wenn für dieses Postfach freigeschaltet (Standard:
    #        nein; server-seitig durchgesetzt, egal was der Client schickt), und
    #    (b) die Ankündigungs-Einstellungen (an/aus, Umfang, privat, extern) — die
    #        sind unabhängig von der Vorlagenwahl, denn die Abwesenheit selbst darf
    #        der Nutzer ohnehin steuern.
    key, _cfg = _postfach(email)
    voll = settings_store.get("MAILBOX_CONFIG") or {}
    eintrag = dict(voll.get(key, {}))
    darf = _darf_vorlagen_waehlen(email)
    if darf:
        oof_tpl = (body.get("oof_template") or "").strip()
        sig_tpl = (body.get("sig_template") or "").strip()
        eintrag["use_policy"] = False
        if oof_tpl:
            eintrag["oof_template"] = oof_tpl
        else:
            eintrag.pop("oof_template", None)
        if sig_tpl and sig_tpl != "default":
            eintrag["template"] = sig_tpl
        elif sig_tpl == "default":
            eintrag.pop("template", None)
    else:
        # Keine Wahl erlaubt → zugewiesene OOF-Vorlage nehmen, nicht überschreiben.
        oof_tpl, _sig = _effektive_vorlagen(email)
    _ankuendigung_ins_eintrag(eintrag, body.get("announce") or {})
    voll[key] = eintrag
    settings_store.update({"MAILBOX_CONFIG": voll})

    # 2) Abwesenheit bei Exchange setzen (Status + Zeitraum + korrekt gerenderter
    #    Text), damit es sofort wirkt — nicht erst beim nächsten Poll.
    token = await graph_client._acquire_token_async()
    if not token:
        raise HTTPException(503, "Kein Graph-Zugriff möglich.")
    status_lese, setting = await abwesenheit._get_setting(email, token)
    if status_lese == "kein_zugriff":
        raise HTTPException(403, "Kein Zugriff auf die Postfacheinstellungen (Consent fehlt).")
    if status_lese != "ok" or setting is None:
        raise HTTPException(502, "Postfacheinstellungen nicht lesbar.")

    # On-demand: Kalender-Cache ZUERST auffrischen (nur wenn das Postfach überhaupt
    # Kalenderdaten braucht — Vorlagen-Nutzungs-Gate), damit die gleich gerenderte
    # Ankündigung schon die aktuellen Termine zeigt und der nächste Poll nichts
    # Veraltetes findet.
    try:
        _k, _cfg = _postfach(email)
        if abwesenheit._braucht_kalender(_cfg, oof_tpl):
            st = abwesenheit._state()
            await abwesenheit.kalender_fenster(email, token, st, force=True)
            abwesenheit._state_speichern(st)
    except Exception as exc:                                       # noqa: BLE001
        log.warning("self_save: Kalender-Cache-Refresh fehlgeschlagen für %s: %s", email, exc)

    synth = _synth_setting(status, start, ende, start_zeit, ende_zeit)
    html, html_extern, _txt = await _render_oof_fuer(
        email, oof_tpl, status, start, ende, start_zeit, ende_zeit, token)
    try:
        await abwesenheit._patch_setting(
            email, token, setting, html, html_extern=html_extern,
            status=status,
            start=synth.get("scheduledStartDateTime"),
            ende=synth.get("scheduledEndDateTime"),
        )
    except Exception as exc:                                       # noqa: BLE001
        log.warning("self_save: OOF-PATCH fehlgeschlagen für %s: %s", email, exc)
        raise HTTPException(502, "Abwesenheit konnte bei Exchange nicht gesetzt werden.")
    log.info("Self-Service: %s hat Abwesenheit (status=%s) + Vorlagen gesetzt", email, status)
    return JSONResponse({"ok": True})
