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
from webui.deps import log, _require_admin, _hash_password, _verify_password

router = APIRouter()

_MAX_BODY = 1024
_GUID = re.compile(r"^[0-9a-fA-F-]{36}$")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def zustand() -> dict:
    """Aktueller Wächter-Zustand — {} wenn noch keiner da ist."""
    return waechter_state.lesen()


@router.post("/api/watchdog/heartbeat")
async def watchdog_heartbeat(request: Request):
    """Der Wächter meldet sich. Token im Kopffeld `X-Watchdog-Token`."""
    stored = settings_store.get("WATCHDOG_TOKEN_HASH") or ""
    token = request.headers.get("X-Watchdog-Token") or ""
    # Falscher/fehlender Token → 401 ohne jedes Detail.
    if not stored or not token or not _verify_password(token, stored):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    raw = await request.body()
    if len(raw) > _MAX_BODY:
        return JSONResponse({"detail": "body too large"}, status_code=413)
    try:
        payload = json.loads(raw or b"{}")
    except Exception:                                       # noqa: BLE001
        payload = {}
    waechter_state.merge(
        last_seen=_now(),
        bypass_active=bool(payload.get("bypass_active")),
        fails=int(payload.get("fails") or 0),
        oks=int(payload.get("oks") or 0),
        healthy=bool(payload.get("healthy")),
        # Leben ≠ handlungsfähig: meldet der Wächter einen EXO-Fehler, läuft er
        # zwar, kann aber die Regel nicht schalten — das gehört sichtbar gemacht.
        exo_error=str(payload.get("exo_error") or "")[:300],
    )
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
    """Für die Oberfläche: zuletzt gesehen, Bypass-Zustand, Regelzustand."""
    st = zustand()
    return JSONResponse({
        "enabled": settings_store.get("WATCHDOG_ENABLED") is True,
        "kind": settings_store.get("WATCHDOG_KIND") or "",
        "last_seen": st.get("last_seen") or "",
        "bypass_active": bool(st.get("bypass_active")),
        "rule_state": st.get("rule_state") or "unbekannt",   # von der EXO-Prüfung (Folgeschritt)
        "token_set": bool(settings_store.get("WATCHDOG_TOKEN_HASH")),
        "exo_error": st.get("exo_error") or "",              # Wächter lebt, aber EXO-Zugriff fehlt
        "hinweise": _config_hinweise(),
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
        if enabled:
            k = updates.get("WATCHDOG_KIND", settings_store.get("WATCHDOG_KIND") or "")
            if k not in ("azure", "cron"):
                return JSONResponse({"ok": False, "detail": "Erst eine Variante wählen."},
                                    status_code=400)
            if not settings_store.get("WATCHDOG_TOKEN_HASH"):
                return JSONResponse({"ok": False, "detail": "Erst ein Heartbeat-Token erzeugen."},
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


@router.post("/api/watchdog/token/rotate")
async def watchdog_token_rotate(user: str = Depends(_require_admin)):
    """Neues Heartbeat-Token erzeugen — Klartext wird EINMALIG zurückgegeben,
    gespeichert wird nur der PBKDF2-Hash."""
    token = secrets.token_urlsafe(32)
    settings_store.update({"WATCHDOG_TOKEN_HASH": _hash_password(token)})
    log.info("Watchdog-Token rotiert von %s", user)
    return JSONResponse({"ok": True, "token": token})


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
        else:
            log.warning("Watchdog-Rollenzuweisung fehlgeschlagen rc=%d: %s", proc.returncode, out[:300])
        zeilen = [ln.strip() for ln in out.splitlines()
                  if ln.strip().startswith("[OK]") or "error" in ln.lower() or "fehler" in ln.lower()]
        return JSONResponse({"ok": ok, "output": "\n".join(zeilen[-6:]) or out[-400:]})
    except Exception as exc:                                   # noqa: BLE001
        log.error("Watchdog-Rollenzuweisung Fehler: %s", exc)
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=500)
