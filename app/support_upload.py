"""
Support-Bundle-Upload zu Azure Blob Storage.

Ein-Klick-Upload eines Diagnose-Pakets (Logs, Einstellungen, Audit-Events,
ACME-Status) an das Support-Azure-Blob des Betreibers.  Sensible Felder
(CLIENT_SECRET, WEBUI_PASSWORD, …) werden vor dem Upload maskiert.

Konfiguration: SUPPORT_BLOB_URL_TEMPLATE in config.py / Env-Var.
"""

import io
import json
import logging
import os
import platform
import re
import secrets
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

import config

log = logging.getLogger(__name__)

# Sensible Settings-Keys: Wert durch *** ersetzen
_SENSITIVE = re.compile(
    r"(secret|password|pfx|key_password|kv_client)", re.IGNORECASE
)

# Blob-URL-Template aus config/env
_BLOB_URL_TEMPLATE: str = config.SUPPORT_BLOB_URL_TEMPLATE

# Max. Größe der einzelnen Log-Dateien im Bundle (Bytes)
_MAX_LOGFILE_BYTES = 4 * 1024 * 1024   # 4 MB
_MAX_ROTATED_FILES = 3                  # letzte N Rotations-Logs


def is_configured() -> bool:
    return bool(_BLOB_URL_TEMPLATE and "{blob_name}" in _BLOB_URL_TEMPLATE)


# ── Datensammlung ─────────────────────────────────────────────────────────────

def _sanitize_settings(data: dict) -> dict:
    """Gibt eine Kopie zurück, in der alle sensiblen Werte durch '***' ersetzt sind."""
    result = {}
    for k, v in data.items():
        if _SENSITIVE.search(k):
            result[k] = "***"
        elif isinstance(v, dict):
            result[k] = _sanitize_settings(v)
        else:
            result[k] = v
    return result


def _system_info() -> dict:
    import socket
    import shutil
    disk = shutil.disk_usage("/app/data")
    return {
        "version": config.VERSION,
        "hostname": config.SUPPORT_BLOB_URL_TEMPLATE and socket.gethostname() or socket.gethostname(),
        "public_hostname": "",       # filled below
        "python": platform.python_version(),
        "platform": platform.system(),
        "upload_ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "disk_data_used_mb": round((disk.total - disk.free) / 1024 / 1024, 1),
        "disk_data_free_mb": round(disk.free / 1024 / 1024, 1),
    }


def _read_log_file(path: Path) -> str:
    """Liest bis zu _MAX_LOGFILE_BYTES der Log-Datei (Tail bei Überschreitung)."""
    if not path.exists():
        return ""
    size = path.stat().st_size
    try:
        with path.open("rb") as fh:
            if size > _MAX_LOGFILE_BYTES:
                fh.seek(-_MAX_LOGFILE_BYTES, os.SEEK_END)
                fh.readline()   # unvollständige Zeile überspringen
            return fh.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return f"[Lesefehler: {exc}]\n"


def _acme_files() -> dict[str, str]:
    """Nicht-sensitive ACME-Dateien (JSON, account_url). Kein account_key!"""
    acme_dir = Path("/app/data/acme")
    result = {}
    if not acme_dir.exists():
        return result
    for f in acme_dir.iterdir():
        if f.suffix == ".json" or f.name.startswith("account_url"):
            try:
                result[f.name] = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
    return result


# ── Bundle bauen ──────────────────────────────────────────────────────────────

def build_bundle(runtime_log_lines: list[str]) -> tuple[bytes, str]:
    """
    Erstellt ein ZIP-Bundle mit allen Diagnosedaten.

    Returns (zip_bytes, blob_name).
    """
    import settings_store
    import mail_audit
    from log_manager import LOG_DIR, _LOG_FILENAME, _ROTATED_RE

    # System-Info
    sysinfo = _system_info()
    all_settings = settings_store.get_all()
    sysinfo["public_hostname"] = str(all_settings.get("PUBLIC_HOSTNAME") or "")

    # Blob-Name: support-{host}-{datum}-{rand}.zip
    safe_host = re.sub(r"[^a-z0-9]+", "-", sysinfo["public_hostname"].lower())[:20].strip("-") or "gateway"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    rand = secrets.token_hex(2)
    blob_name = f"support-{safe_host}-{ts}-{rand}.zip"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:

        # README
        zf.writestr("README.txt", (
            f"EXO Signature Gateway — Support-Bundle\n"
            f"Erstellt: {sysinfo['upload_ts']}\n"
            f"Version:  {sysinfo['version']}\n"
            f"Host:     {sysinfo['public_hostname']}\n"
            f"\nInhalt:\n"
            f"  system_info.json          — Gateway-Version, Plattform, Disk\n"
            f"  settings_sanitized.json   — Konfiguration (Secrets maskiert)\n"
            f"  mailbox_health.json       — Letzter Postfach-Health-Check\n"
            f"  acme/                     — ACME-Bestellstatus (kein Private Key)\n"
            f"  audit_events.jsonl        — Letzte 7 Tage Mail-Audit-Log\n"
            f"  logs/runtime.txt          — In-Memory-Log (letzte 500 Zeilen)\n"
            f"  logs/app.log*             — Persistent Log-Dateien (max. 4 MB/Datei)\n"
        ))

        # System-Info
        zf.writestr("system_info.json", json.dumps(sysinfo, indent=2, ensure_ascii=False))

        # Sanitized Settings
        zf.writestr("settings_sanitized.json",
                    json.dumps(_sanitize_settings(all_settings), indent=2, ensure_ascii=False))

        # Mailbox Health
        health = all_settings.get("MAILBOX_HEALTH") or {}
        zf.writestr("mailbox_health.json",
                    json.dumps(health, indent=2, ensure_ascii=False))

        # ACME-Dateien (keine Private Keys)
        for name, content in _acme_files().items():
            zf.writestr(f"acme/{name}", content)

        # Audit-Events (letzte 7 Tage)
        events = mail_audit.query_events(limit=1000)
        lines = [json.dumps(e, ensure_ascii=False) for e in events]
        zf.writestr("audit_events.jsonl", "\n".join(lines))

        # Runtime-Log (In-Memory-Buffer)
        zf.writestr("logs/runtime.txt", "\n".join(runtime_log_lines))

        # Persistente Log-Dateien
        current_log = LOG_DIR / _LOG_FILENAME
        zf.writestr(f"logs/{_LOG_FILENAME}", _read_log_file(current_log))

        rotated = sorted(
            [f for f in LOG_DIR.iterdir() if _ROTATED_RE.match(f.name)],
            reverse=True,
        )[:_MAX_ROTATED_FILES]
        for f in rotated:
            zf.writestr(f"logs/{f.name}", _read_log_file(f))

    return buf.getvalue(), blob_name


# ── Upload ────────────────────────────────────────────────────────────────────

async def upload_bundle(runtime_log_lines: list[str]) -> dict:
    """
    Baut das Bundle und lädt es hoch.

    Returns {"ok": bool, "ticket_id": str, "size_kb": float, "error": str}
    """
    if not is_configured():
        return {"ok": False, "error": "SUPPORT_BLOB_URL_TEMPLATE nicht konfiguriert."}

    import asyncio
    try:
        zip_bytes, blob_name = await asyncio.get_event_loop().run_in_executor(
            None, build_bundle, runtime_log_lines
        )
    except Exception as exc:
        log.error("Support bundle build failed: %s", exc)
        return {"ok": False, "error": f"Bundle-Erstellung fehlgeschlagen: {exc}"}

    url = _BLOB_URL_TEMPLATE.replace("{blob_name}", blob_name)
    size_kb = round(len(zip_bytes) / 1024, 1)

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.put(
                url,
                content=zip_bytes,
                headers={
                    "x-ms-blob-type": "BlockBlob",
                    "Content-Type": "application/zip",
                    "x-ms-version": "2023-11-03",
                },
            )
        if resp.status_code in (200, 201):
            log.info("Support bundle uploaded: %s (%s KB)", blob_name, size_kb)
            return {"ok": True, "ticket_id": blob_name, "size_kb": size_kb}
        log.error("Support bundle upload failed: HTTP %s — %s",
                  resp.status_code, resp.text[:300])
        return {
            "ok": False,
            "error": f"Azure Blob HTTP {resp.status_code}: {resp.text[:200]}",
        }
    except Exception as exc:
        log.error("Support bundle upload error: %s", exc)
        return {"ok": False, "error": f"Netzwerkfehler: {exc}"}
