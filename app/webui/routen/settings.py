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
async def api_save_internal_groups(request: Request, user=Depends(_require_admin)):
    import dynamische_gruppen as _dg
    data = await request.json()
    groups = data.get("groups")
    if not isinstance(groups, dict):
        raise HTTPException(400, "groups must be a dict")
    # Berechnete Gruppen („Alle Postfächer") nie persistieren — ihre Mitglieder
    # entstehen bei jedem Zugriff neu. Ihre Variablen dürfen aber gespeichert werden.
    alle_namen = set(groups) | {gruppen.ALLE}
    groups = gruppen.nur_gespeicherte(groups)
    # Dynamische Gruppen: Definition je Name prüfen (400 bei unbekanntem Feld/
    # Vergleich, Tiefe > 2 …). Fehlt „dynamic“ ganz, bleiben die bisherigen
    # Definitionen der weiter bestehenden Gruppen — ein älterer Aufrufer löscht
    # so keine Regel.
    dyn_roh = data.get("dynamic")
    if dyn_roh is None:
        dyn_roh = {n: d for n, d in (settings_store.get("DYNAMIC_GROUPS") or {}).items()
                   if n in groups}
    if not isinstance(dyn_roh, dict):
        raise HTTPException(400, "dynamic must be a dict")
    dyn: dict = {}
    for name, definition in dyn_roh.items():
        if name not in groups:
            continue                     # verwaist oder „Alle Postfächer“ — nicht dynamisch
        try:
            dyn[name] = _dg.definition_pruefen(definition)
        except _dg.RegelFehler as exc:
            raise HTTPException(400, f"Gruppe „{name}“: {exc}")
    for name in dyn:
        groups[name] = []                # Mitglieder werden berechnet; Eintrag hält die Reihenfolge
    vorher = settings_store.get("DYNAMIC_GROUPS") or {}
    aenderung: dict = {"INTERNAL_GROUPS": groups, "DYNAMIC_GROUPS": dyn}
    # Gruppen-Variablen (optional) im selben Speichervorgang: {Gruppe: {var: wert}}.
    # Nur Gruppen behalten, die es auch gibt — sonst bleiben Werte verwaister
    # Gruppen ewig liegen.
    group_vars = data.get("group_vars")
    if isinstance(group_vars, dict):
        bereinigt = {g: v for g, v in group_vars.items()
                     if g in alle_namen and isinstance(v, dict) and v}
        aenderung["GROUP_VARS"] = bereinigt
    if dyn != vorher:
        wer = user if isinstance(user, str) else "Verwaltung"
        _dg.grund_vormerken(f"Regel gespeichert von {wer}")
    settings_store.update(aenderung)
    # Gleich neu rechnen: Änderungen der Mitglieder stehen sofort im Audit, mit
    # dem Grund von oben — nicht erst bei der nächsten Mail.
    gruppen.interne_gruppen()
    return JSONResponse({"ok": True})


@router.get("/api/gruppen/dynamisch")
async def api_dynamische_gruppen(_=Depends(_require_admin)):
    """Definitionen der dynamischen Gruppen und Stand des Entra-Schnappschusses."""
    import dynamische_gruppen as _dg
    namen = gruppen.dynamische_namen()
    dyn = settings_store.get("DYNAMIC_GROUPS") or {}
    return JSONResponse({"ok": True, "definitionen": {n: dyn[n] for n in namen},
                         "status": _dg.status()})


@router.post("/api/gruppen/dynamisch/vorschau")
async def api_dynamische_vorschau(request: Request, _=Depends(_require_admin)):
    """Noch nicht gespeicherte Regel gegen den Schnappschuss auswerten — ändert nichts."""
    import dynamische_gruppen as _dg
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(400, "Objekt erwartet")
    try:
        return JSONResponse(_dg.vorschau(str(data.get("name") or ""), data.get("definition"),
                                         str(data.get("filter") or "")))
    except _dg.RegelFehler as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/gruppen/dynamisch/einlesen")
async def api_dynamische_einlesen(user=Depends(_require_admin)):
    """Knopf „Entra neu einlesen“. Scheitert der Abruf, bleibt der alte Stand."""
    import dynamische_gruppen as _dg
    return JSONResponse(await _dg.einlesen(user if isinstance(user, str) else "Verwaltung"))


@router.get("/api/settings/custom-policies")
async def api_get_custom_policies(_=Depends(_require_admin)):
    # Immer im aktuellen Format — auch wenn eine zurückgespielte Sicherung
    # noch das Altformat trägt (policies.regeln).
    import policies as _pol
    return JSONResponse(_pol.regeln(settings_store.get("CUSTOM_POLICIES") or []))


@router.post("/api/settings/custom-policies/save")
async def api_save_custom_policies(request: Request, _=Depends(_require_admin)):
    """Gruppen-Regeln speichern — normalisiert über `policies.regeln`: nur
    bekannte Slots, Werte als Zeichenkette (`addin` Liste oder "*"), `fest` nur
    für Wahl-Slots, die die Regel auch festlegt. Unbrauchbare Regeln entfallen.
    Die Reihenfolge bleibt (first-match je Slot) — hier wird nichts
    zusammengefasst."""
    import policies as _pol
    data = await request.json()
    roh = data.get("policies") if isinstance(data, dict) else None
    if not isinstance(roh, list):
        raise HTTPException(400, "policies must be a list")
    rg = _pol.regeln(roh)
    if len(rg) != len(roh):
        log.warning("Gruppen-Regeln: %d von %d unbrauchbar, verworfen",
                    len(roh) - len(rg), len(roh))
    settings_store.update({"CUSTOM_POLICIES": rg})
    return JSONResponse({"ok": True, "regeln": rg})


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
    import ews_bereich
    import owa_signatur
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
            # Hybrid-Option wirkt nur im Rückweg smtp (exo_mailboxes.hybrid_aktiv)
            "reinject_mode": settings_store.reinject_mode(),
            "owa_sig": owa_signatur.uebersicht(),
            "ews_bereich": ews_bereich.uebersicht(),
            "ews_bereich_befehl": ews_bereich.BESTAND_BEFEHL,
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
            "loop_marker": __import__("loop_detector").marker_wert(),
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
    if "SMTP_PROXY_QUELLEN" in clean:
        import smtp_proxy
        try:
            clean["SMTP_PROXY_QUELLEN"] = smtp_proxy.normalisieren(clean["SMTP_PROXY_QUELLEN"])
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if "HOSTS_EINTRAEGE" in clean:
        import namensaufloesung
        try:
            clean["HOSTS_EINTRAEGE"] = namensaufloesung.normalisieren(str(clean["HOSTS_EINTRAEGE"] or ""))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    fehler = _ews_onprem_pruefen(clean)
    if fehler:
        return JSONResponse({"ok": False, "error": fehler}, status_code=400)
    import health_check
    ews_neu = health_check.ews_geaendert(clean, settings_store.get)
    settings_store.update(clean)
    if ews_neu:
        # Status der Hybrid-Nutzer (Abwesenheit, Postfach lesen) hängt an der
        # Anbindung — neu prüfen statt „entfällt“ von vorher stehen zu lassen.
        health_check.nach_ews_aenderung()
    if "HYBRID_POSTFAECHER" in clean or "REINJECT_MODE" in clean:
        # Das PowerShell-Skript der Postfachliste hängt an beiden (Get-MailUser
        # nur bei wirksamer Hybrid-Option) — der nächste Abruf fragt EXO frisch.
        import exo_mailboxes
        exo_mailboxes.invalidate_cache()
    if "LOG_LEVEL" in clean:
        import log_manager
        log_manager.stufe_anwenden()    # wirkt sofort, nicht erst nach dem Neustart
    import aufgaben
    aufgaben.vergessen()        # der Sammelpunkt soll die Änderung gleich zeigen
    log.info("Settings updated by %s: %s", user, list(clean.keys()))
    return JSONResponse({"ok": True})


def _ews_onprem_pruefen(clean: dict) -> str:
    """Anbindung an Exchange lokal: Adresse/IP prüfen, Werte trimmen. Fehlertext oder ""."""
    import ews_onprem
    for k in ("EWS_ONPREM_URL", "EWS_ONPREM_KONTO", "EWS_ONPREM_VERBINDEN", "EWS_ONPREM_KENNWORT"):
        if k not in clean:
            continue
        v = clean[k] if isinstance(clean[k], str) else ""
        if any(c in v for c in "\r\n"):
            return "Kein Zeilenumbruch in den Feldern der Anbindung an Exchange lokal"
        clean[k] = v if k == "EWS_ONPREM_KENNWORT" else v.strip()
    if clean.get("EWS_ONPREM_URL"):
        f = ews_onprem.url_pruefen(clean["EWS_ONPREM_URL"])
        if f:
            return f
    if clean.get("EWS_ONPREM_VERBINDEN"):
        f = ews_onprem.verbinden_pruefen(clean["EWS_ONPREM_VERBINDEN"])
        if f:
            return f
    import ews_bereich
    for k in ("EWS_LDAP_SERVER", "EWS_LDAP_BASIS", "EWS_BEREICH_OU", "EWS_BEREICH_GRUPPE"):
        if k not in clean:
            continue
        v = clean[k].strip() if isinstance(clean[k], str) else ""
        clean[k] = v
        if not v:
            continue
        if k == "EWS_LDAP_SERVER":
            try:
                ews_bereich.ldap_server_pruefen(v)
            except ews_bereich.BereichFehler as exc:
                return exc.detail
        elif k == "EWS_BEREICH_GRUPPE":
            f = ews_bereich.name_pruefen(v, "Gruppenname")
            if f:
                return f
        else:
            f = ews_bereich.dn_pruefen(v, "Basis-DN" if k == "EWS_LDAP_BASIS" else "Ablage (OU)")
            if f:
                return f
    if "EWS_ONPREM_CA" in clean:
        # Mehrzeilig (PEM) — darum nicht in der Zeilenumbruch-Prüfung oben.
        # Gespeichert wird die geprüfte, neu kodierte Fassung.
        roh = clean["EWS_ONPREM_CA"] if isinstance(clean["EWS_ONPREM_CA"], str) else ""
        pem, f = ews_onprem.ca_pruefen(roh)
        if f:
            return f
        clean["EWS_ONPREM_CA"] = pem
    return ""


@router.post("/api/ews-onprem/test")
async def api_ews_onprem_test(request: Request, user: str = Depends(_require_admin)):
    """„Verbindung testen“: GetUserOofSettings auf eine Adresse — mit der
    GESPEICHERTEN Anbindung. Liest nur, ändert nichts."""
    import asyncio
    import ews_onprem
    try:
        body = await request.json()
    except Exception:
        body = {}
    adresse = (body.get("adresse") or "").strip() if isinstance(body, dict) else ""
    if not ews_onprem.konfiguriert():
        return JSONResponse({"ok": False, "code": "konfiguration",
                             "error": "Erst EWS-Adresse, Dienstkonto und Kennwort speichern."})
    # Der Knopf geht immer hinaus, auch während der Ausfallsperre (ews_onprem).
    erg = await asyncio.to_thread(ews_onprem.verbindung_testen, adresse, True)
    log.info("EWS-Verbindungstest durch %s für %s: %s", user, adresse,
             "ok" if erg["ok"] else erg["code"])
    if not erg["ok"]:
        erg["error"] = erg["detail"]
    return JSONResponse(erg)


@router.post("/api/ews-onprem/owa-signatur")
async def api_ews_onprem_owa_signatur(request: Request, user: str = Depends(_require_admin)):
    """„Jetzt übertragen“: Signatur der Hybrid-Nutzer in OWA (lokal) schreiben —
    auch bei unveränderter Prüfsumme. Mit `adresse` nur dieses Postfach (Ergebnis
    am Knopf), ohne alle im Hintergrund (Ergebnis im Protokoll und in der Karte)."""
    import owa_signatur
    try:
        body = await request.json()
    except Exception:
        body = {}
    adresse = (body.get("adresse") or "").strip() if isinstance(body, dict) else ""
    if not owa_signatur.aktiv():
        return JSONResponse({"ok": False, "error": "Erst die Anbindung einrichten und "
                                                   "„Signatur in OWA übertragen“ einschalten."})
    if adresse:
        erg = await owa_signatur.abgleich(erzwingen=True, nur=adresse)
        log.info("OWA-Signatur durch %s für %s: %s", user, adresse,
                 "übertragen" if erg.get(owa_signatur.GESETZT) else (erg.get("error") or "Fehler"))
        if erg.get("ok") and not erg.get(owa_signatur.GESETZT):
            f = (erg.get("fehler_liste") or [{}])[0]
            erg = {**erg, "ok": False,
                   "error": f.get("detail") or "Die Vorlage ergibt für dieses Postfach keine Signatur"}
        return JSONResponse(erg)
    gesamt = len(owa_signatur.ziele(settings_store.get("MAILBOX_CONFIG") or {}))
    owa_signatur.im_hintergrund()
    log.info("OWA-Signatur: Abgleich aller %d Hybrid-Nutzer durch %s gestartet", gesamt, user)
    return JSONResponse({"ok": True, "gestartet": True, "gesamt": gesamt})


# ── Zugriffsbereich des EWS-Dienstkontos (ews_bereich) ──────────────────────
# Admin-Anmeldedaten kommen NUR bei /einrichten herein, gehen nur an
# ews_bereich.einrichten und werden weder protokolliert noch gespeichert.

async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        body = {}
    return body if isinstance(body, dict) else {}


def _bereich_daten(body: dict) -> dict:
    """Formularwerte für den Plan; der erkannte Bestand kommt aus dem Stand,
    nicht aus der Anfrage."""
    import ews_bereich
    return {"konto_anlegen": bool(body.get("konto_anlegen")),
            "konto": str(body.get("konto") or "").strip(),
            "alt": ews_bereich.stand().get("bestand") or {},
            "alt_entfernen": bool(body.get("alt_entfernen"))}


@router.get("/api/ews-bereich/stand")
async def api_ews_bereich_stand(_=Depends(_require_admin)):
    import ews_bereich
    return JSONResponse({"ok": True, **ews_bereich.uebersicht()})


@router.post("/api/ews-bereich/befehle")
async def api_ews_bereich_befehle(request: Request, _=Depends(_require_admin)):
    """Vorschau: dieselbe Schrittliste, die „Einrichten“ ausführt (ews_bereich.plan)."""
    import ews_bereich
    try:
        p = ews_bereich.parameter(_bereich_daten(await _json(request)))
    except ews_bereich.BereichFehler as exc:
        return JSONResponse({"ok": False, "error": exc.detail})
    schritte = ews_bereich.plan(p)
    return JSONResponse({"ok": True, "text": ews_bereich.befehle_text(schritte),
                         "schritte": [{"kennung": s.kennung, "art": s.art, "titel": s.titel}
                                      for s in schritte]})


@router.post("/api/ews-bereich/bestand")
async def api_ews_bereich_bestand(request: Request, user: str = Depends(_require_admin)):
    """Ausgabe des Bestandsbefehls auswerten und merken. Ändert nichts."""
    import ews_bereich
    import ews_onprem
    body = await _json(request)
    konto = str(body.get("konto") or "").strip() or ews_onprem._wert(ews_onprem.KEY_KONTO)
    try:
        b = ews_bereich.bestand_erkennen(str(body.get("ausgabe") or ""), konto,
                                         ews_bereich.stand().get("gruppe_dn", ""))
    except ews_bereich.BereichFehler as exc:
        return JSONResponse({"ok": False, "error": exc.detail})
    ews_bereich._stand_setzen(bestand=b)
    log.info("EWS-Bereich: Bestand ausgewertet durch %s — %d Zuweisungen für %s",
             user, len(b["zuweisungen"]), konto)
    return JSONResponse({"ok": True, **b})


@router.post("/api/ews-bereich/einrichten")
async def api_ews_bereich_einrichten(request: Request, user: str = Depends(_require_admin)):
    import asyncio
    import ews_bereich
    body = await _json(request)
    admin_konto = str(body.pop("admin_konto", "") or "").strip()
    admin_kennwort = str(body.pop("admin_kennwort", "") or "")
    erg = await asyncio.to_thread(ews_bereich.einrichten, _bereich_daten(body),
                                  admin_konto, admin_kennwort, user)
    admin_kennwort = ""                                   # nicht länger als nötig
    log.info("EWS-Bereich: Einrichtung durch %s — %s", user,
             "AD-Teil erledigt" if erg.get("ok") else erg.get("code"))
    _nach_kontowechsel(erg)
    import aufgaben
    aufgaben.vergessen()
    return JSONResponse(erg)


def _nach_kontowechsel(erg: dict) -> None:
    """Wurde das Dienstkonto wirksam (Ersteinrichtung oder bestandene Prüfung),
    gilt dasselbe wie beim Speichern der Anbindung über POST /settings: Hybrid-
    Nutzer neu prüfen, statt ein „OK“ des alten Kontos stehen zu lassen. Hier,
    im Event-Loop — ews_bereich läuft in einem Thread ohne Loop."""
    if erg.get("zugang_gewechselt"):
        import health_check
        health_check.nach_ews_aenderung()


@router.post("/api/ews-bereich/abgleichen")
async def api_ews_bereich_abgleichen(user: str = Depends(_require_admin)):
    """„Jetzt abgleichen“ — ändert die Mitglieder, auch mit ausgeschalteter Pflege."""
    import asyncio
    import ews_bereich
    erg = await asyncio.to_thread(ews_bereich.abgleichen, f"Knopf durch {user}", True)
    return JSONResponse(erg)


@router.post("/api/ews-bereich/pruefen")
async def api_ews_bereich_pruefen(user: str = Depends(_require_admin)):
    import asyncio
    import ews_bereich
    erg = await asyncio.to_thread(ews_bereich.pruefen, user)
    _nach_kontowechsel(erg)
    import aufgaben
    aufgaben.vergessen()
    return JSONResponse(erg)


@router.post("/api/ews-bereich/verwerfen")
async def api_ews_bereich_verwerfen(user: str = Depends(_require_admin)):
    """„Ausstehendes Konto verwerfen“ — das bisherige Dienstkonto bleibt wirksam."""
    import ews_bereich
    erg = ews_bereich.verwerfen(user)
    import aufgaben
    aufgaben.vergessen()
    return JSONResponse(erg)


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
