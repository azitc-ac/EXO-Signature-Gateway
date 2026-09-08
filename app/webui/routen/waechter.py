"""Bypass-Wächter: Heartbeat/Status/Token für den externen Failover-Wächter.

Der Wächter (Azure Function oder cron-Host) prüft `/health` und schaltet bei
Ausfall die Signatur-Transportregel ab. Diese Endpunkte geben ihm einen
Heartbeat-Kanal (er meldet Zustand) und der Verwaltung eine Sicht darauf.

Der wechselnde Zustand (zuletzt gesehen, Bypass, Zähler) liegt in
`data/watchdog_state.json` — NICHT in settings.json, das sonst im Minutentakt
samt Geheimnissen neu geschrieben würde. In settings.json steht nur der
Token-Hash (Geheimnis) und die Konfiguration.
"""
from __future__ import annotations

import json
import re
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

import config
import settings_store
import waechter_state
import waechter_register
from webui.deps import log, _require_admin, _hash_password, _verify_password, _get_session_user

router = APIRouter()

_MAX_BODY = 1024
_GUID = re.compile(r"^[0-9a-fA-F-]{36}$")

# Zustand der laufenden Function-Provisionierung (nur einer gleichzeitig).
_deploy: dict = {"running": False, "step": "", "ok": None, "msg": "",
                 "principal_id": "", "host": "", "app": "", "watcher_id": ""}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def zustand() -> dict:
    """Aktueller Wächter-Zustand — {} wenn noch keiner da ist."""
    return waechter_state.lesen()


@router.post("/api/watchdog/heartbeat")
async def watchdog_heartbeat(request: Request):
    """Ein Wächter meldet sich. Token im Kopffeld `X-Watchdog-Token`, optional
    `X-Watchdog-Id` (neue Wächter). Legacy-Wächter ohne Id werden per Token-Scan
    über das Register zugeordnet — so überlebt der bestehende Prod-Wächter den
    Umbau ohne Änderung."""
    token = request.headers.get("X-Watchdog-Token") or ""
    wid_hdr = (request.headers.get("X-Watchdog-Id") or "").strip()
    # Falscher/fehlender Token oder unbekannter Wächter → 401 ohne jedes Detail.
    wid = waechter_register.zuordnen(token, wid_hdr or None, _verify_password)
    if not wid:
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    raw = await request.body()
    if len(raw) > _MAX_BODY:
        return JSONResponse({"detail": "body too large"}, status_code=413)
    try:
        payload = json.loads(raw or b"{}")
    except Exception:                                       # noqa: BLE001
        payload = {}
    waechter_register.heartbeat_aktualisieren(wid, {
        "last_seen": _now(),
        "bypass_active": bool(payload.get("bypass_active")),
        "fails": int(payload.get("fails") or 0),
        "oks": int(payload.get("oks") or 0),
        "healthy": bool(payload.get("healthy")),
        # Leben ≠ handlungsfähig: meldet der Wächter einen EXO-Fehler, läuft er
        # zwar, kann aber die Regel nicht schalten — das gehört sichtbar gemacht.
        "exo_error": str(payload.get("exo_error") or "")[:300],
    })
    return JSONResponse({"ok": True})


def _config_hinweise() -> dict:
    """Werte, die beide Wächter-Varianten brauchen — zum Kopieren in der Oberfläche."""
    import waechter_regel
    basis = (settings_store.get("ADDIN_BASE_URL") or "").strip().rstrip("/")
    return {
        "health_url": (basis + "/health") if basis else "",
        "organization": (settings_store.get("TENANT_DOMAIN") or "").strip(),
        "sig_rule_name": waechter_regel.regelname(),
    }


@router.get("/api/watchdog/status")
async def watchdog_status(user: str = Depends(_require_admin)):
    """Für die Oberfläche: Liste der Wächter (je zuletzt gesehen/Zustand), der
    globale Regelzustand und die Kopierwerte. Zusätzlich Aggregat-Felder für die
    bisherige Einzelanzeige (bis die Liste in der Oberfläche steht)."""
    st = zustand()                                            # globaler Regelzustand
    watchers = waechter_register.liste()
    last_seen = max((w.get("last_seen") or "" for w in watchers), default="")
    any_bypass = any(w.get("bypass_active") for w in watchers) or bool(st.get("bypass_active"))
    exo_err = next((w.get("exo_error") for w in watchers if w.get("exo_error")), "") \
        or (st.get("exo_error") or "")
    kind = watchers[0]["kind"] if watchers else (settings_store.get("WATCHDOG_KIND") or "")
    return JSONResponse({
        "enabled": settings_store.get("WATCHDOG_ENABLED") is True,
        "rule_state": st.get("rule_state") or "unbekannt",   # von der EXO-Prüfung des Schedulers
        "watchers": watchers,
        "count": len(watchers),
        "hinweise": _config_hinweise(),
        # ── Aggregat (Rückwärtskompatibilität der alten Einzelanzeige) ──
        "kind": kind,
        "last_seen": last_seen,
        "bypass_active": any_bypass,
        "token_set": len(watchers) > 0 or bool(settings_store.get("WATCHDOG_TOKEN_HASH")),
        "exo_error": exo_err,
    })


@router.post("/api/watchdog/config")
async def watchdog_config(request: Request, user: str = Depends(_require_admin)):
    """Variante wählen (azure|cron) und den Wächter scharf-/stellen. Aktivieren geht
    nur mit gewählter Variante UND gesetztem Token — sonst liefe ein Wächter ins Leere."""
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:                                          # noqa: BLE001
        body = {}
    updates: dict = {}

    kind = body.get("kind")
    if kind is not None:
        if kind not in ("", "azure", "cron"):
            return JSONResponse({"ok": False, "detail": "Variante muss 'azure' oder 'cron' sein."},
                                status_code=400)
        updates["WATCHDOG_KIND"] = kind

    enabled = body.get("enabled")
    if enabled is not None:
        enabled = bool(enabled)
        # Scharfstellen nur, wenn es überhaupt einen Wächter gibt — sonst liefe
        # der Regelzustands-Check ins Leere. (Legacy-Token zählt als Wächter.)
        if enabled and not (waechter_register.anzahl() > 0
                            or settings_store.get("WATCHDOG_TOKEN_HASH")):
            return JSONResponse({"ok": False, "detail": "Erst einen Wächter hinzufügen."},
                                status_code=400)
        updates["WATCHDOG_ENABLED"] = enabled

    if updates:
        settings_store.update(updates)
        log.info("Watchdog-Konfiguration geändert von %s: %s", user, sorted(updates))
    return JSONResponse({
        "ok": True,
        "kind": settings_store.get("WATCHDOG_KIND") or "",
        "enabled": settings_store.get("WATCHDOG_ENABLED") is True,
    })


@router.post("/api/watchdog/grant-role")
async def watchdog_grant_role(request: Request, user: str = Depends(_require_admin)):
    """Weist der Wächter-Identität die EXO-SCHREIB-Rolle „Transport Rules" zu — der
    einzige Teil, den das Gateway selbst erledigen kann (per Auth-Zertifikat).
    App-Rolle `Exchange.ManageAsApp` und Entra „Global Reader" bleiben Azure-/
    Directory-Admin (die Oberfläche zeigt die Befehle)."""
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:                                          # noqa: BLE001
        body = {}
    # Mit watcher_id kommen AppId + Objekt-ID aus dem Register (keine freien
    # Felder); watchdog_app_id/-object_id bleiben für den manuellen/cron-Weg.
    wid = str(body.get("watcher_id") or "").strip()
    if wid:
        e = waechter_register.holen(wid)
        az = (e or {}).get("azure") or {}
        app_id = str(az.get("app_id") or "").strip()
        obj_id = str(az.get("principal_id") or "").strip()
        if not app_id:
            return JSONResponse({"ok": False, "detail": "AppId noch nicht aufgelöst — erst "
                                "‚Berechtigungen erteilen (Azure-Login)‘ ausführen."}, status_code=400)
    else:
        app_id = str(body.get("watchdog_app_id") or "").strip()
        obj_id = str(body.get("watchdog_object_id") or "").strip()
    if not _GUID.match(app_id) or not _GUID.match(obj_id):
        return JSONResponse({"ok": False, "detail": "AppId und Objekt-ID müssen GUIDs sein."},
                            status_code=400)
    script = Path("/app/scripts/grant_watchdog_role.ps1")
    cert = Path(config.DATA_DIR) / "auth.pfx"
    if not script.exists() or not cert.exists():
        return JSONResponse({"ok": False, "detail": "Skript oder Auth-Zertifikat fehlt."},
                            status_code=500)
    gw_app = config.CLIENT_ID or settings_store.get("CLIENT_ID") or ""
    org = settings_store.get("TENANT_DOMAIN") or ""
    if not gw_app or not org:
        return JSONResponse({"ok": False, "detail": "Gateway unvollständig (CLIENT_ID/TENANT_DOMAIN)."},
                            status_code=400)
    cmd = ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(script),
           "-AppId", gw_app, "-Organization", org, "-CertPath", str(cert),
           "-WatchdogAppId", app_id, "-WatchdogObjectId", obj_id]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        out = (proc.stdout + "\n" + proc.stderr).strip()
        ok = proc.returncode == 0 and "GRANT-ROLE-OK" in proc.stdout
        if ok:
            log.info("Watchdog-Rolle 'Transport Rules' zugewiesen von %s (MI %s)", user, app_id)
            if wid:
                waechter_register.merke_azure(wid, grants_done=True)
        else:
            log.warning("Watchdog-Rollenzuweisung fehlgeschlagen rc=%d: %s", proc.returncode, out[:300])
        zeilen = [ln.strip() for ln in out.splitlines()
                  if ln.strip().startswith("[OK]") or "error" in ln.lower() or "fehler" in ln.lower()]
        return JSONResponse({"ok": ok, "output": "\n".join(zeilen[-6:]) or out[-400:]})
    except Exception as exc:                                   # noqa: BLE001
        log.error("Watchdog-Rollenzuweisung Fehler: %s", exc)
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=500)


# ── Function per ARM anlegen (KV-Stil, delegierter ARM-Token) ─────────────────

def _health_base_url() -> str:
    base = (settings_store.get("ADDIN_BASE_URL") or "").strip().rstrip("/")
    if not base:
        host = (settings_store.get("PUBLIC_HOSTNAME") or "").strip()
        base = f"https://{host}" if host else ""
    return base


async def _run_deploy(upn: str, sub: str, rg: str, loc: str, app_name: str, create_rg: bool) -> None:
    """Hintergrund-Provisionierung: RG → Storage → Function+MI → Code → Settings.
    Aktualisiert _deploy. Läuft im Hauptprozess (settings_store.update ist hier sicher)."""
    import re as _re
    import keyvault
    import waechter_regel
    import watchdog_deploy as wd

    def _step(s):
        _deploy["step"] = s
        log.info("Watchdog-Deploy: %s", s)

    try:
        token = keyvault.get_user_arm_token(upn)
        if not token:
            _deploy.update(running=False, ok=False, msg="Kein Azure-Zugriff — erst per Azure-Login.")
            return
        storage = (_re.sub(r"[^a-z0-9]", "", app_name.lower())[:16] + secrets.token_hex(4))[:24]
        _deploy["app"] = app_name

        if create_rg:
            _step("Resource Group")
            ok, msg = await wd.ensure_resource_group(sub, rg, loc, token)
            if not ok:
                _deploy.update(running=False, ok=False, msg=msg); return

        # Frische Subscriptions haben Microsoft.Storage/Microsoft.Web nicht
        # registriert (ARM 409) — einmalig pro Abo nachholen, sonst scheitert
        # das Anlegen von Storage/Function.
        _step("Ressourcenanbieter")
        ok, msg = await wd.ensure_providers_registered(sub, token)
        if not ok:
            _deploy.update(running=False, ok=False, msg=msg); return

        _step("Storage-Konto")
        ok, msg, conn = await wd.create_storage_account(sub, rg, storage, loc, token)
        if not ok:
            _deploy.update(running=False, ok=False, msg=msg); return

        _step("Function-App")
        ok, msg, info = await wd.create_function_app(sub, rg, app_name, loc, conn, "7.6", token)
        if not ok:
            _deploy.update(running=False, ok=False, msg=msg); return
        _deploy["principal_id"] = info.get("principalId", "")
        _deploy["host"] = info.get("defaultHostName", "")

        _step("Code hochladen")
        ok, msg = await wd.deploy_code(sub, rg, app_name, token)
        if not ok:
            _deploy.update(running=False, ok=False, msg=msg); return

        _step("Einstellungen")
        # Eigenes Token je Wächter (NICHT mehr das globale rotieren — sonst sperrt
        # ein zweiter Installer den ersten Wächter aus). Registrierung mit den
        # Rückbau-Metadaten, damit das Gateway später GENAU das löschen kann.
        token_klar = secrets.token_urlsafe(32)
        wid = waechter_register.neue_id()
        waechter_register.registrieren(
            id=wid,
            name=app_name,                                   # kurz, wie in Azure — Details (RG/Region) zeigt die Liste
            kind="azure",
            token_hash=_hash_password(token_klar),
            azure={
                "subscription": sub, "resource_group": rg, "app_name": app_name,
                "location": loc, "storage": storage, "plan": f"{app_name}-plan",
                "created_rg": bool(create_rg),
                "principal_id": _deploy.get("principal_id", ""), "app_id": "",
            },
        )
        _deploy["watcher_id"] = wid
        settings_store.update({"WATCHDOG_KIND": "azure"})     # nur Anzeige; Aktivieren bleibt manuell
        health = _health_base_url()
        app_settings = {
            "GATEWAY_HEALTH_URL": (health + "/health") if health else "",
            "EXO_ORGANIZATION": settings_store.get("TENANT_DOMAIN") or "",
            "SIG_RULE_NAME": waechter_regel.regelname(),
            "FAIL_THRESHOLD": "3",
            "WATCHDOG_TOKEN": token_klar,
            "WATCHDOG_ID": wid,
        }
        ok, msg = await wd.set_app_settings(sub, rg, app_name, app_settings, token)
        if not ok:
            _deploy.update(running=False, ok=False, msg=msg); return

        _deploy.update(running=False, ok=True, step="Fertig",
                       msg="Function angelegt und als Wächter registriert. "
                           "Als Nächstes die Berechtigungen erteilen (unten).")
    except Exception as exc:                                    # noqa: BLE001
        log.error("Watchdog-Deploy-Fehler: %s", exc)
        _deploy.update(running=False, ok=False, msg=str(exc))


@router.post("/api/watchdog/deploy/start")
async def watchdog_deploy_start(request: Request, user: str = Depends(_require_admin)):
    """Startet die Function-Provisionierung im Hintergrund (dauert ~5–7 Min).
    Braucht einen delegierten ARM-Token (vorher „Azure-Login")."""
    import asyncio
    if _deploy.get("running"):
        return JSONResponse({"ok": False, "detail": "Es läuft bereits eine Provisionierung."},
                            status_code=409)
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:                                          # noqa: BLE001
        body = {}
    sub = str(body.get("subscription_id") or "").strip()
    rg = str(body.get("resource_group") or "").strip()
    loc = str(body.get("location") or "").strip()
    app_name = str(body.get("app_name") or "").strip()
    create_rg = bool(body.get("create_rg"))
    if not (sub and rg and loc and app_name):
        return JSONResponse({"ok": False, "detail": "subscription_id, resource_group, location, app_name nötig."},
                            status_code=400)
    if not _GUID.match(sub):
        return JSONResponse({"ok": False, "detail": "subscription_id ist keine gültige GUID."}, status_code=400)
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9-]{1,58}[a-zA-Z0-9]$", app_name):
        return JSONResponse({"ok": False, "detail": "app_name: 2–60 Zeichen, nur Buchstaben/Ziffern/Bindestrich, "
                                                    "nicht mit Bindestrich beginnend/endend."}, status_code=400)
    if not re.match(r"^[a-zA-Z0-9._()-]{1,90}$", rg):
        return JSONResponse({"ok": False, "detail": "resource_group: ungültiger Name."}, status_code=400)
    upn = _get_session_user(request) or ""
    if not keyvault_arm_ok(upn):
        return JSONResponse({"ok": False, "detail": "Kein Azure-Zugriff — erst per Azure-Login."},
                            status_code=400)
    _deploy.update(running=True, ok=None, step="Start", msg="",
                   principal_id="", host="", app=app_name, watcher_id="")
    asyncio.create_task(_run_deploy(upn, sub, rg, loc, app_name, create_rg))
    log.info("Watchdog-Deploy gestartet von %s: %s/%s (%s)", user, rg, app_name, loc)
    return JSONResponse({"ok": True, "started": True})


def keyvault_arm_ok(upn: str) -> bool:
    import keyvault
    return bool(upn) and bool(keyvault.get_user_arm_token(upn))


@router.get("/api/watchdog/deploy/status")
async def watchdog_deploy_status(user: str = Depends(_require_admin)):
    return JSONResponse({k: _deploy.get(k) for k in
                         ("running", "step", "ok", "msg", "principal_id", "host", "app", "watcher_id")})


# ── Rückbau: Wächter entfernen ────────────────────────────────────────────────

@router.post("/api/watchdog/remove")
async def watchdog_remove(request: Request, user: str = Depends(_require_admin)):
    """Wächter entfernen. Azure: das Gateway löscht per ARM GENAU das, was es
    angelegt hat (delegierter Login nötig). cron: aus dem Register nehmen und die
    Entfern-Befehle für den Zweithost zurückgeben (der fremde Host bleibt uns
    unerreichbar)."""
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:                                          # noqa: BLE001
        body = {}
    wid = str(body.get("id") or "").strip()
    eintrag = waechter_register.holen(wid)
    if not eintrag:
        return JSONResponse({"ok": False, "detail": "Wächter nicht gefunden."}, status_code=404)
    kind = eintrag.get("kind")

    if kind == "cron":
        waechter_register.entfernen(wid)
        log.info("Watchdog (cron) entfernt von %s: %s", user, wid)
        return JSONResponse({"ok": True, "kind": "cron",
            "hinweis": "Auf dem Wächter-Host ausführen, um ihn stillzulegen:",
            "befehle": ("sudo systemctl disable --now exo-watchdog.timer\n"
                        "sudo rm -f /etc/systemd/system/exo-watchdog.timer "
                        "/etc/systemd/system/exo-watchdog.service\n"
                        "sudo rm -rf /etc/exo-watchdog /opt/exo-watchdog\n"
                        "sudo systemctl daemon-reload")})

    # Azure: Ressourcen löschen, dann aus dem Register nehmen.
    az = eintrag.get("azure") or {}
    upn = _get_session_user(request) or ""
    if not keyvault_arm_ok(upn):
        return JSONResponse({"ok": False, "detail": "Kein Azure-Zugriff — erst per Azure-Login."},
                            status_code=400)
    if not az.get("subscription") or not az.get("resource_group") or not az.get("app_name"):
        # Kein vollständiger Rückbau-Datensatz (z.B. migrierter Legacy-Wächter):
        # nur aus dem Register nehmen, Azure-Ressourcen bleiben stehen.
        waechter_register.entfernen(wid)
        return JSONResponse({"ok": True, "kind": "azure", "nur_register": True,
                             "detail": "Aus dem Register genommen. Azure-Ressourcen "
                                       "waren nicht hinterlegt — dort ggf. manuell entfernen."})
    import keyvault
    import watchdog_deploy as wd
    tok = keyvault.get_user_arm_token(upn)
    ok, msg = await wd.delete_resources(
        az["subscription"], az["resource_group"], az["app_name"],
        az.get("storage", ""), az.get("plan", ""), bool(az.get("created_rg")), tok)
    if not ok:
        return JSONResponse({"ok": False, "detail": f"Azure-Löschen fehlgeschlagen: {msg}"},
                            status_code=502)
    waechter_register.entfernen(wid)
    log.info("Watchdog (azure) entfernt von %s: %s (%s)", user, wid, az.get("app_name"))
    return JSONResponse({"ok": True, "kind": "azure",
                         "detail": "Function und angelegte Ressourcen entfernt. Verwaiste "
                                   "Rollen-Zuweisungen der gelöschten Identität sind harmlos."})


# ── cron-Wächter: Bundle zum Copy-Paste erzeugen ──────────────────────────────

@router.post("/api/watchdog/cron/generate")
async def watchdog_cron_generate(request: Request, user: str = Depends(_require_admin)):
    """Registriert einen cron-Wächter (eigenes Token + Id) und gibt eine
    vorbefüllte `watchdog.env` zum Copy-Paste zurück. AppId/Zertifikat der
    Wächter-App trägt der Betreiber selbst nach (cert-basierte Anmeldung)."""
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:                                          # noqa: BLE001
        body = {}
    name = (str(body.get("name") or "").strip() or "cron-Wächter")[:80]
    token_klar = secrets.token_urlsafe(32)
    wid = waechter_register.neue_id()
    waechter_register.registrieren(id=wid, name=name, kind="cron",
                                   token_hash=_hash_password(token_klar))
    h = _config_hinweise()
    env_text = (
        "# /etc/exo-watchdog/watchdog.env  (chmod 600)\n"
        f"GATEWAY_HEALTH_URL={h['health_url']}\n"
        f"WATCHDOG_TOKEN={token_klar}\n"
        f"WATCHDOG_ID={wid}\n"
        f"EXO_ORGANIZATION={h['organization']}\n"
        "WATCHDOG_APP_ID=<AppId deiner Wächter-App>\n"
        "WATCHDOG_CERT_PATH=/etc/exo-watchdog/watchdog.pfx\n"
        f"SIG_RULE_NAME={h['sig_rule_name']}\n"
        "FAIL_THRESHOLD=3\n"
    )
    log.info("Watchdog (cron) registriert von %s: %s (%s)", user, wid, name)
    return JSONResponse({"ok": True, "id": wid, "token": token_klar, "env": env_text})
