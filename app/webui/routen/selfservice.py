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


def _synth_setting(status: str, start: str, ende: str) -> dict:
    """Ein automaticRepliesSetting-ähnliches dict aus den Nutzereingaben bauen,
    damit zeitraum_text()/start_ende_text() denselben Text liefern wie im Betrieb.
    `start`/`ende` sind ISO-Datumsangaben (YYYY-MM-DD) aus den <input type=date>.

    Ein Zeitraum deckt die ganzen Tage ab: lokal `start` 00:00 bis `ende` 23:59.
    (Date-only-UI; wer in Outlook stundengenau plant, verliert das beim Speichern
    über /self — bewusste Vereinfachung.)"""
    s: dict = {"status": status}
    if status == "scheduled":
        if start:
            s["scheduledStartDateTime"] = _als_utc(start, "00:00:00")
        if ende:
            s["scheduledEndDateTime"] = _als_utc(ende, "23:59:59")
    return s


async def _render_oof_fuer(email: str, oof_tpl: str, status: str,
                           start: str, ende: str) -> tuple[str, str]:
    """(html, txt) der Abwesenheit für die eigene Adresse — wie im Betrieb."""
    if not oof_tpl:
        return "", ""
    user_data = await graph_client.get_user(email)
    synth = _synth_setting(status or "scheduled", start, ende)
    _ab, _bis = abwesenheit.start_ende_text(synth)
    return abwesenheit.render_oof(user_data, oof_tpl, abwesenheit.zeitraum_text(synth), _ab, _bis)


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
        "ooo": {"status": "disabled", "start": "", "ende": ""},
        "zugriff": True,
    }
    # Aktuellen OOF-Status aus Exchange lesen (nur Anzeige — kein Schreibzugriff).
    try:
        token = await graph_client._acquire_token_async()
        if token:
            status_lese, setting = await abwesenheit._get_setting(email, token)
            if status_lese == "ok" and setting:
                daten["ooo"]["status"] = setting.get("status", "disabled")
                ab, bis = _iso_aus_setting(setting)
                daten["ooo"]["start"], daten["ooo"]["ende"] = ab, bis
            elif status_lese == "kein_zugriff":
                daten["zugriff"] = False
    except Exception as exc:                                       # noqa: BLE001
        log.warning("self_context: OOF-Status lesen fehlgeschlagen für %s: %s", email, exc)
    return JSONResponse(daten)


def _iso_aus_setting(setting: dict) -> tuple[str, str]:
    """(start, ende) als YYYY-MM-DD aus dem Setting (für <input type=date>)."""
    def iso(d):
        dt = abwesenheit._lokal_datum(d)
        return f"{dt:%Y-%m-%d}" if dt else ""
    return iso(setting.get("scheduledStartDateTime")), iso(setting.get("scheduledEndDateTime"))


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
                       email: str = Depends(_require_self)):
    """Korrekte, VOLLSTÄNDIGE Vorschau fürs eigene Postfach: OOF (über der Signatur)
    + Signatur + Banner + Disclaimer (wie die echte Mail)."""
    oof_html, oof_txt = await _render_oof_fuer(email, oof, status, start, ende)
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
    if status not in ("disabled", "alwaysEnabled", "scheduled"):
        raise HTTPException(400, "Ungültiger Status")

    # 1) Vorlagenwahl NUR, wenn für dieses Postfach freigeschaltet (Standard: nein).
    #    ⚠️ Server-seitig durchgesetzt — die Felder aus dem Body werden sonst
    #    ignoriert, egal was der Client schickt. Darf der Nutzer nicht wählen, bleibt
    #    MAILBOX_CONFIG unangetastet und es gilt die zugewiesene Vorlage.
    if _darf_vorlagen_waehlen(email):
        oof_tpl = (body.get("oof_template") or "").strip()
        sig_tpl = (body.get("sig_template") or "").strip()
        key, _cfg = _postfach(email)
        voll = settings_store.get("MAILBOX_CONFIG") or {}
        eintrag = dict(voll.get(key, {}))
        eintrag["use_policy"] = False
        if oof_tpl:
            eintrag["oof_template"] = oof_tpl
        else:
            eintrag.pop("oof_template", None)
        if sig_tpl and sig_tpl != "default":
            eintrag["template"] = sig_tpl
        elif sig_tpl == "default":
            eintrag.pop("template", None)
        voll[key] = eintrag
        settings_store.update({"MAILBOX_CONFIG": voll})
    else:
        # Keine Wahl erlaubt → zugewiesene OOF-Vorlage nehmen, Config NICHT ändern.
        oof_tpl, _sig = _effektive_vorlagen(email)

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
    synth = _synth_setting(status, start, ende)
    html, _txt = await _render_oof_fuer(email, oof_tpl, status, start, ende)
    try:
        await abwesenheit._patch_setting(
            email, token, setting, html,
            status=status,
            start=synth.get("scheduledStartDateTime"),
            ende=synth.get("scheduledEndDateTime"),
        )
    except Exception as exc:                                       # noqa: BLE001
        log.warning("self_save: OOF-PATCH fehlgeschlagen für %s: %s", email, exc)
        raise HTTPException(502, "Abwesenheit konnte bei Exchange nicht gesetzt werden.")
    log.info("Self-Service: %s hat Abwesenheit (status=%s) + Vorlagen gesetzt", email, status)
    return JSONResponse({"ok": True})
