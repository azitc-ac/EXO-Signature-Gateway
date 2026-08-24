"""Systemzustand, Selbst-Aktualisierung und Unterstützungspaket.

Hier steht, was das Gateway über sich selbst weiss und wie es sich erneuert:
Fassung, Betriebsdaten, verfügbare Ausgaben, der Aktualisierungslauf samt
Zustand, sowie das Paket für den Fernzugriff durch die Betreuung.

⚠️ Der Aktualisierungslauf ersetzt den laufenden Dienst. Er gehört deshalb
sichtbar an einen Ort und nicht verstreut zwischen Fachlogik.

Aus `app/webui/app.py` herausgelöst (21.08.2026). Reines Umsortieren — der
Inhalt der Funktionen ist unverändert; die Routen-Momentaufnahme in
`tests/test_routes.py` belegt, dass dieselbe Oberfläche herauskommt.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, StreamingResponse)

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import config
import held_mails as _held_mails_mod
import settings_store

from webui.deps import (
    templates, log, _gateway_name, _check_auth, _require_admin, _LOG_BUFFER,
)

router = APIRouter()


@router.get("/api/system/info")
async def api_system_info(user: str = Depends(_require_admin)):
    import time as _time_mod
    import mail_audit as _audit_mod
    import handler as _handler_mod

    # Disk usage of /app/data
    data_path = Path("/app/data")
    try:
        du = shutil.disk_usage(str(data_path))
        disk_total_mb  = round(du.total / 1024 / 1024, 1)
        disk_used_mb   = round(du.used  / 1024 / 1024, 1)
        disk_free_mb   = round(du.free  / 1024 / 1024, 1)
        disk_pct       = round(du.used / du.total * 100, 1) if du.total else 0
    except Exception:
        disk_total_mb = disk_used_mb = disk_free_mb = disk_pct = None

    # SQLite DB size
    db_path = _audit_mod.DB_PATH
    try:
        db_size_kb = round(db_path.stat().st_size / 1024, 1)
    except Exception:
        db_size_kb = None

    # Log files total size
    logs_path = data_path / "logs"
    try:
        logs_size_kb = round(sum(f.stat().st_size for f in logs_path.iterdir() if f.is_file()) / 1024, 1)
    except Exception:
        logs_size_kb = None

    # Process RSS memory from /proc/self/status
    rss_mb = None
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                rss_mb = round(int(line.split()[1]) / 1024, 1)
                break
    except Exception:
        pass

    # Process uptime
    uptime_s = None
    try:
        pid_stat = Path("/proc/self/stat").read_text().split()
        # field 22 (0-indexed 21) = starttime in clock ticks
        clk_tck = os.sysconf("SC_CLK_TCK")
        uptime_total = float(Path("/proc/uptime").read_text().split()[0])
        proc_start_ticks = int(pid_stat[21])
        uptime_s = int(uptime_total - proc_start_ticks / clk_tck)
    except Exception:
        pass

    # In-flight mail count
    in_flight = _handler_mod._in_flight

    # Avg processing time last 24h
    since_24h = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # rewind 24h
    from datetime import timedelta
    since_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    avg_ms = _audit_mod.avg_processing_ms(since_24h)

    # Peak hour today
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    peak = _audit_mod.peak_hour(today_str)

    return {
        "disk_total_mb":    disk_total_mb,
        "disk_used_mb":     disk_used_mb,
        "disk_free_mb":     disk_free_mb,
        "disk_pct":         disk_pct,
        "db_size_kb":       db_size_kb,
        "logs_size_kb":     logs_size_kb,
        "rss_mb":           rss_mb,
        "uptime_s":         uptime_s,
        "in_flight":        in_flight,
        "avg_ms_24h":       avg_ms,
        "peak_hour":        peak[0] if peak else None,
        "peak_hour_cnt":    peak[1] if peak else None,
        "maintenance_mode": bool(settings_store.get("MAINTENANCE_MODE")),
        "held_mail_count":  _held_mails_mod.count(),
    }

@router.get("/api/system/mail-hourly")
async def api_mail_hourly(user: str = Depends(_require_admin)):
    """Stündliche Mail-Statistik für heute aus mail_audit.db."""
    import mail_audit as _audit_mod
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _audit_mod.get_mail_hourly(today)

@router.get("/api/system/log-tail")
async def api_log_tail(n: int = 150, user: str = Depends(_require_admin)):
    """Letzte N Zeilen aus dem In-Memory-Log-Buffer."""
    lines = list(_LOG_BUFFER)[-n:]
    return {"lines": lines}

@router.post("/api/system/restart-container")
async def api_restart_container(user: str = Depends(_require_admin)):
    """Trigger-Datei schreiben → Host-Watcher führt docker compose restart aus."""
    import updater
    result = updater.request_container_restart(user)
    if not result["ok"]:
        return JSONResponse(result, status_code=409)
    log.info("Container restart requested by %s", user)
    return JSONResponse(result)

@router.get("/api/system/update/check")
async def api_update_check(channel: str = "main", user: str = Depends(_require_admin)):
    """GitHub-Prüfung: gibt es eine neuere Version im gewählten Kanal?"""
    import updater
    return JSONResponse(updater.check_update(channel, config.VERSION))

@router.get("/api/system/update/releases")
async def api_update_releases(user: str = Depends(_require_admin)):
    """Liste aller veröffentlichten Release-Tags (für Versionsauswahl / Rollback)."""
    import updater
    return JSONResponse({"releases": updater.list_release_tags()})

@router.post("/api/system/update")
async def api_system_update(request: Request, user: str = Depends(_require_admin)):
    """Trigger-Datei schreiben → Host-Watcher führt git pull + docker compose up --build aus."""
    import updater
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    channel = body.get("channel", "main")
    target_version = (body.get("target_version") or "").strip() or None
    result = updater.request_update(user, config.VERSION, channel=channel, target_version=target_version)
    if not result["ok"]:
        return JSONResponse(result, status_code=409)
    log.info("Update requested by %s (channel: %s, current version: %s, target: %s)",
              user, channel, config.VERSION, target_version or "latest")
    return JSONResponse(result)

@router.get("/api/system/update/status")
async def api_system_update_status(user: str = Depends(_require_admin)):
    """Aktuellen Update-Status aus data/.update-status lesen."""
    import updater
    return JSONResponse(updater.get_status())

@router.post("/api/system/update/clear")
async def api_system_update_clear(user: str = Depends(_require_admin)):
    """Status-Datei löschen (nach erfolgreichem Update oder Fehler)."""
    import updater
    updater.clear_status()
    return JSONResponse({"ok": True})

@router.get("/api/system/update/watcher-status")
async def api_watcher_status(user: str = Depends(_require_admin)):
    """Prüft ob der Host-Watcher-Service läuft (Heartbeat-Datei)."""
    import updater
    return JSONResponse({"ok": updater.watcher_ok()})


@router.get("/api/system/changelog")
async def api_changelog(n: int = 10, user: str = Depends(_require_admin)):
    """Letzte N Einträge aus CHANGELOG.md."""
    try:
        text = (Path("/app/CHANGELOG.md")).read_text(encoding="utf-8")
    except FileNotFoundError:
        return JSONResponse({"entries": [], "error": "CHANGELOG.md nicht gefunden"})
    entries = []
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("## ") and current:
            entries.append("\n".join(current).strip())
            current = [line]
            if len(entries) >= n:
                break
        elif line.startswith("## "):
            current = [line]
        elif current:
            current.append(line)
    if current and len(entries) < n:
        entries.append("\n".join(current).strip())
    return JSONResponse({"entries": entries})

@router.get("/api/support/download")
async def api_support_download(user: str = Depends(_require_admin)):
    """Support-Bundle als ZIP herunterladen (lokal speichern)."""
    import support_upload as _sup
    import asyncio as _aio
    from fastapi.responses import Response as _Resp
    zip_bytes, blob_name = await _aio.get_event_loop().run_in_executor(
        None, _sup.build_bundle, list(_LOG_BUFFER)
    )
    return _Resp(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{blob_name}"'},
    )

@router.post("/api/support/upload")
async def api_support_upload(request: Request, user: str = Depends(_require_admin)):
    """Support-Bundle (Logs, Settings, Audit) an den Provider-Hub hochladen."""
    import hub_client
    import legal_consent
    # Gate C — Art. 28 Abs. 3 DSGVO: Die Verarbeitung muss durch einen Vertrag
    # geregelt SEIN, bevor sie beginnt. Das Bundle enthält Mail-Metadaten
    # (Absender/Empfänger/Betreff) Dritter — ohne AVV darf es nicht übertragen
    # werden.
    if not legal_consent.context_consented("support_upload"):
        raise HTTPException(
            403, "Für die Übermittlung von Diagnosepaketen muss zuerst der "
                 "Auftragsverarbeitungsvertrag abgeschlossen werden "
                 "(Einstellungen → Anbindung & Lizenzen → Rechtliche Dokumente).")
    try:
        body = await request.json()
    except Exception:
        body = {}
    note = str((body or {}).get("note") or "").strip()[:2000]
    result = await hub_client.upload_bundle(list(_LOG_BUFFER), note=note)
    return JSONResponse(result)
