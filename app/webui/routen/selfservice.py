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


def _postfach(email: str) -> tuple[str, dict]:
    """(config_key, sender_cfg) für die EIGENE Adresse. 403, wenn das Postfach im
    Gateway nicht verwaltet wird (Self-Service gilt nur für aktivierte Postfächer)."""
    cfg = settings_store.get("MAILBOX_CONFIG") or {}
    key = mailbox_match.match_sender_key(cfg, email)
    if not key or key not in cfg:
        raise HTTPException(403, "Für dieses Postfach ist kein Self-Service verfügbar.")
    return key, dict(cfg[key])


def _synth_setting(status: str, start: str, ende: str) -> dict:
    """Ein automaticRepliesSetting-ähnliches dict aus den Nutzereingaben bauen,
    damit zeitraum_text()/start_ende_text() denselben Text liefern wie im Betrieb.
    `start`/`ende` sind ISO-Datumsangaben (YYYY-MM-DD) aus den <input type=date>."""
    s: dict = {"status": status}
    if status == "scheduled":
        if start:
            s["scheduledStartDateTime"] = {"dateTime": f"{start}T00:00:00.0000000", "timeZone": "UTC"}
        if ende:
            s["scheduledEndDateTime"] = {"dateTime": f"{ende}T00:00:00.0000000", "timeZone": "UTC"}
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
    daten = {
        "email": email,
        "freigeschaltet": _freigeschaltet(),
        "use_policy": cfg.get("use_policy", True),
        "oof_template": cfg.get("oof_template", ""),
        "sig_template": cfg.get("template", "default"),
        "oof_templates": by_kind.get("oof", []),
        "sig_templates": by_kind.get("signatur", ["default"]),
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


@router.get("/api/self/preview")
async def self_preview(oof: str = "", sig: str = "", status: str = "scheduled",
                       start: str = "", ende: str = "",
                       email: str = Depends(_require_self)):
    """Korrekte Vorschau für das EIGENE Postfach — OOF (über der Signatur) + Signatur."""
    oof_html, oof_txt = await _render_oof_fuer(email, oof, status, start, ende)
    sig_html, sig_txt = "", ""
    if sig:
        user_data = await graph_client.get_user(email)
        sig_html, sig_txt = signature_engine.render(user_data, template_name=sig)
    return JSONResponse({"oof_html": oof_html, "oof_txt": oof_txt,
                         "sig_html": sig_html, "sig_txt": sig_txt})


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
    oof_tpl = (body.get("oof_template") or "").strip()
    sig_tpl = (body.get("sig_template") or "").strip()
    status = (body.get("oof_status") or "disabled").strip()
    start = (body.get("oof_start") or "").strip()
    ende = (body.get("oof_ende") or "").strip()
    if status not in ("disabled", "alwaysEnabled", "scheduled"):
        raise HTTPException(400, "Ungültiger Status")

    # 1) Eigene Vorlagenwahl in MAILBOX_CONFIG (use_policy→false: Nutzer übernimmt).
    key, cfg_eintrag = _postfach(email)
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
