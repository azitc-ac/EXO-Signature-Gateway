"""Routen der Einstellungen — Seiten und Speichern.

Sechstes Modul der Aufteilung von `app.py`. Muster siehe `addin.py`.

⚠️ ZUM PFADFILTER: `/settings/smime` gehört HIERHER, nicht ins S/MIME-Modul.
Der Filter dort greift auf `/smime`, `/smime/…` und `/api/smime…` — die
Einstellungsseite für S/MIME faellt also korrekt heraus und liegt bei den
uebrigen Einstellungsseiten, wo sie hingehoert. Ein schlichtes `"smime" in
pfad` haette sie auseinandergerissen.

⚠️ `settings_store.public_view()`, NIEMALS `get_all()`
------------------------------------------------------
Die Seiten geben den Einstellungsstand an eine Vorlage. `public_view()`
maskiert dabei die als geheim deklarierten Schluessel (`SECRET_KEYS`);
`get_all()` gaebe sie im Klartext ins HTML. Geheimnisfelder bekommen im
Formular ausserdem `placeholder` statt `value` — kaeme die Maskierung
zurueck, wuerde sie das echte Kennwort ueberschreiben. Beides steht als
verbindliche Regel in `CLAUDE.md`.

ZUM SPEICHERN
-------------
`POST /settings` (`settings_save`) ist der EINE allgemeine Schreibweg — für
das Formular wie für einzelne Felder (`savePartial()` in common.js). Er
verlangt die Verwaltungsrolle; `tests/test_wachen.py` führt ihn namentlich
unter den besonders folgenreichen Adressen. Bis 2026-10-21 gab es daneben
`POST /api/settings/partial` mit derselben Prüfung; zwei Sofort-Schalter
riefen ihn an savePartial vorbei auf. Nach deren Umstellung hatte er keinen
Aufrufer mehr und ist entfallen.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import graph_client
import gruppen
import settings_store

from webui.deps import (
    templates, log, _gateway_name, _check_auth, _require_admin,
)

router = APIRouter()


def _loop_header_konsequenzen(clean: dict) -> None:
    """Ändert sich `LOOP_HEADER`, läuft die EXO-Transportregel hinterher.

    Die Regeln prüfen den Header-Namen, den der letzte Setup-Lauf geschrieben
    hat; ändert man das Setting, setzt das Gateway auf dem Graph-/SMTP-Weg zwar
    den neuen Namen, die Regeln nehmen aber weiter den alten aus — Loop-Ausnahme
    greift nicht mehr. Wir markieren die betroffenen Setup-Schritte deshalb als
    offen (Connector UND S/MIME-Regeln, beide prüfen den Header), damit Wizard
    und Abnahme zum Neu-Ausführen auffordern, statt Gateway und Regeln still
    auseinanderlaufen zu lassen."""
    if "LOOP_HEADER" not in clean:
        return
    neu = (clean.get("LOOP_HEADER") or "").strip() or "X-Sig-Applied"
    alt = (settings_store.get("LOOP_HEADER") or "").strip() or "X-Sig-Applied"
    if neu == alt:
        return
    offen = {}
    if settings_store.get("EXO_CONNECTOR_CREATED"):
        offen["EXO_CONNECTOR_CREATED"] = False
    if settings_store.get("SMIME_RULES_CREATED"):
        offen["SMIME_RULES_CREATED"] = False
    if offen:
        settings_store.update(offen)
        log.warning(
            "LOOP_HEADER %r→%r: %s auf offen gesetzt — die betroffenen "
            "Transportregeln müssen neu geschrieben werden.",
            alt, neu, ", ".join(sorted(offen)))


@router.get("/api/settings/template-policies")
async def api_get_template_policies(_=Depends(_require_admin)):
    return JSONResponse(settings_store.get("TEMPLATE_POLICIES") or {"sig": "default"})


@router.get("/api/settings/internal-groups")
async def api_get_internal_groups(_=Depends(_require_admin)):
    # Inklusive der berechneten Gruppe „Alle Postfächer" (gruppen.py) — die
    # Oberfläche zeigt sie schreibgeschützt.
    return JSONResponse(gruppen.interne_gruppen())


@router.post("/api/settings/internal-groups/save")
async def api_save_internal_groups(request: Request, _=Depends(_require_admin)):
    data = await request.json()
    groups = data.get("groups")
    if not isinstance(groups, dict):
        raise HTTPException(400, "groups must be a dict")
    # Berechnete Gruppen („Alle Postfächer") nie persistieren — ihre Mitglieder
    # entstehen bei jedem Zugriff neu. Ihre Variablen dürfen aber gespeichert werden.
    alle_namen = set(groups) | {gruppen.ALLE}
    groups = gruppen.nur_gespeicherte(groups)
    aenderung: dict = {"INTERNAL_GROUPS": groups}
    # Gruppen-Variablen (optional) im selben Speichervorgang: {Gruppe: {var: wert}}.
    # Nur Gruppen behalten, die es auch gibt — sonst bleiben Werte verwaister
    # Gruppen ewig liegen.
    group_vars = data.get("group_vars")
    if isinstance(group_vars, dict):
        bereinigt = {g: v for g, v in group_vars.items()
                     if g in alle_namen and isinstance(v, dict) and v}
        aenderung["GROUP_VARS"] = bereinigt
    settings_store.update(aenderung)
    return JSONResponse({"ok": True})


@router.get("/api/settings/custom-policies")
async def api_get_custom_policies(_=Depends(_require_admin)):
    return JSONResponse(settings_store.get("CUSTOM_POLICIES") or [])


@router.post("/api/settings/custom-policies/save")
async def api_save_custom_policies(request: Request, _=Depends(_require_admin)):
    data = await request.json()
    policies = data.get("policies")
    if not isinstance(policies, list):
        raise HTTPException(400, "policies must be a list")
    settings_store.update({"CUSTOM_POLICIES": policies})
    return JSONResponse({"ok": True})


@router.post("/api/settings/sender-mailboxes/refresh")
async def api_refresh_sender_mailboxes(user: str = Depends(_require_admin)):
    import asyncio
    import exo_mailboxes
    try:
        await asyncio.to_thread(exo_mailboxes.list_mailboxes, True)  # force refresh
    except Exception as exc:
        raise HTTPException(500, str(exc))
    return JSONResponse({"ok": True, "mailboxes": exo_mailboxes.as_sender_list()})


@router.post("/api/settings/notification-mailbox/create-shared")
async def api_create_notification_shared_mailbox(user: str = Depends(_require_admin)):
    import asyncio
    import setup_wizard
    import exo_mailboxes
    result = await asyncio.to_thread(setup_wizard.run_create_notification_mailbox)
    if not result.get("ok"):
        raise HTTPException(500, result.get("output") or "Anlage fehlgeschlagen")
    try:
        await asyncio.to_thread(exo_mailboxes.list_mailboxes, True)
    except Exception:
        pass
    return JSONResponse({"ok": True, "email": result.get("email", ""), "mailboxes": exo_mailboxes.as_sender_list()})


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user: str = Depends(_require_admin)):
    import asyncio
    import exo_mailboxes
    try:
        await asyncio.to_thread(exo_mailboxes.list_mailboxes)
        sender_mailboxes = exo_mailboxes.as_sender_list()
    except Exception:
        sender_mailboxes = []
    return templates.TemplateResponse(
        request=request, name="settings.html",
        context={
            "s": settings_store.public_view(),
            "active": "settings",
            "saved": request.query_params.get("saved"),
            "gateway_name": _gateway_name(),
            "sender_mailboxes": sender_mailboxes,
        },
    )


@router.get("/settings/signature", response_class=HTMLResponse)
async def settings_signature_page(request: Request, user: str = Depends(_require_admin)):
    import asyncio
    import exo_mailboxes
    try:
        await asyncio.to_thread(exo_mailboxes.list_mailboxes)
        sender_mailboxes = exo_mailboxes.as_sender_list()
    except Exception:
        sender_mailboxes = []
    return templates.TemplateResponse(
        request=request, name="settings_signature.html",
        context={
            "s": settings_store.public_view(),
            "active": "settings-signature",
            "saved": request.query_params.get("saved"),
            "gateway_name": _gateway_name(),
            "sender_mailboxes": sender_mailboxes,
            "custom_var_entra_fields": graph_client.CUSTOM_VAR_ENTRA_FIELDS,
            "regel_stand": _regel_stand(),
        },
    )


def _regel_stand() -> dict:
    """Zuletzt gelesene Empfängerbedingung der Signatur-Regel (regel_bedingung.py)
    — ohne Exchange-Anmeldung beim Seitenaufruf; frisch prüft der Knopf."""
    import waechter_state
    st = waechter_state.lesen()
    if not st.get("rule_checked") or "rule_sent_to_scope" not in st:
        return {}
    return {"sent_to_scope": st.get("rule_sent_to_scope") or "", "am": st["rule_checked"]}


@router.get("/settings/smime", response_class=HTMLResponse)
async def settings_smime_page(request: Request, user: str = Depends(_require_admin)):
    # Für die automatische Bestellung: nur Bezugswege, die ohne Zutun des
    # Postfachinhabers bestellen können — dieselbe Bedingung wie beim
    # Sammelvorgang, damit die Auswahl nicht auseinanderläuft.
    import ca_backends
    wege = [b for b in await ca_backends.list_backends_aktuell()
            if b.get("ready") and b.get("auto")]
    return templates.TemplateResponse(
        request=request, name="settings_smime.html",
        context={
            "enroll_wege": wege,
            "s": settings_store.public_view(),
            "active": "settings-smime",
            "saved": request.query_params.get("saved"),
            "gateway_name": _gateway_name(),
        },
    )


@router.get("/settings/connect", response_class=HTMLResponse)
async def settings_connect_page(request: Request, user: str = Depends(_require_admin)):
    import hub_client
    return templates.TemplateResponse(
        request=request, name="settings_connect.html",
        context={
            "s": settings_store.public_view(),
            "active": "settings-connect",
            "gateway_name": _gateway_name(),
            "hub_registered": hub_client.is_registered(),
            "hub_cert_registered": hub_client.cert_is_registered(),
        },
    )


@router.get("/settings/update")
async def settings_update_redirect(user: str = Depends(_require_admin)):
    # Update-Tab wurde mit Backup zusammengelegt
    return RedirectResponse("/backup", status_code=308)


@router.post("/settings")
async def settings_save(request: Request, user: str = Depends(_require_admin)):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Ungültige JSON-Daten")
    clean, unbekannt = settings_store.nur_bekannte(data)
    if unbekannt:
        log.warning("/settings: unbekannte Schlüssel verworfen: %s", unbekannt)
    _loop_header_konsequenzen(clean)
    settings_store.update(clean)
    log.info("Settings updated by %s: %s", user, list(clean.keys()))
    return JSONResponse({"ok": True})


# ── Separates SMTP-Listener-Zertifikat (Erweitert) ───────────────────────────
@router.get("/api/smtp-cert")
async def api_smtp_cert_info(_=Depends(_require_admin)):
    import smtp_cert
    return JSONResponse(smtp_cert.info())


@router.post("/api/smtp-cert")
async def api_smtp_cert_save(pfx: UploadFile = File(...), password: str = Form(""),
                            user: str = Depends(_require_admin)):
    """Separates SMTP-Listener-Zert aus einer hochgeladenen PFX/PKCS#12-Datei
    (Zert + Schlüssel, optional Kette). Passwort optional."""
    import smtp_cert
    data = await pfx.read()
    if not data:
        return JSONResponse({"ok": False, "error": "Keine PFX-Datei empfangen."},
                            status_code=400)
    import asyncio
    try:
        # speichern_pfx vervollständigt die Kette per AIA (Netz-I/O) → Thread,
        # damit der Event-Loop nicht blockiert.
        await asyncio.to_thread(smtp_cert.speichern_pfx, data, password)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    log.info("Separates SMTP-Listener-Zert (PFX) gesetzt durch %s", user)
    return JSONResponse({"ok": True, "info": smtp_cert.info(), "restart_needed": True})


@router.post("/api/smtp-cert/remove")
async def api_smtp_cert_remove(user: str = Depends(_require_admin)):
    import smtp_cert
    weg = smtp_cert.entfernen()
    if weg:
        log.info("Separates SMTP-Listener-Zert entfernt durch %s", user)
    return JSONResponse({"ok": True, "entfernt": weg, "restart_needed": weg})
