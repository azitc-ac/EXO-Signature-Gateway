"""
Backup und Wiederherstellung aller persistenten Gateway-Daten.

Backup-Inhalt (ZIP):
  data/   — settings.json, auth.pfx, smime/, acme/, mail_audit.db,
             stats*.json, selfservice_tokens.json
  templates/ — Signatur-Templates (*.html, *.txt)

Nicht enthalten (werden auf dem Zielsystem neu erstellt):
  data/logs/          — Laufzeit-Logs
  data/le-config/     — Let's Encrypt-Verzeichnis
  data/le-logs/
  data/le-work/
  data/acme-webroot/
"""

import io
import logging
import re
import secrets
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import config

log = logging.getLogger(__name__)

DATA_DIR = Path("/app/data")
TEMPLATE_DIR = Path(config.TEMPLATE_DIR)

_EXCLUDE_DATA_SUBDIRS = {"logs", "le-config", "le-logs", "le-work", "acme-webroot"}
_EXCLUDE_DATA_FILES   = {"settings.bak"}


def create_backup() -> tuple[bytes, str]:
    """Erstellt vollständiges ZIP-Backup. Returns (zip_bytes, filename)."""
    import socket
    ts        = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    rand      = secrets.token_hex(2)
    safe_host = re.sub(r"[^a-z0-9]+", "-", socket.gethostname().lower())[:20].strip("-") or "gateway"
    filename  = f"backup-{safe_host}-{ts}-{rand}.zip"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:

        zf.writestr("README.txt", (
            f"EXO Signature Gateway — Backup\n"
            f"Erstellt: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
            f"Host:     {socket.gethostname()}\n"
            f"Version:  {config.VERSION}\n"
            f"\n"
            f"Inhalt:\n"
            f"  data/       — Konfiguration, S/MIME-Keys, ACME-State, Audit-DB, Statistiken\n"
            f"  templates/  — Signatur-Templates (HTML + TXT)\n"
            f"\n"
            f"Nicht enthalten (werden neu erstellt):\n"
            f"  Logs, Let's Encrypt-Zertifikate\n"
            f"\n"
            f"Wiederherstellung:\n"
            f"  Web UI → Einstellungen → Backup → ZIP hochladen → Container neu starten\n"
            f"  Oder: ZIP entpacken, data/ und templates/ in das Gateway-Verzeichnis kopieren.\n"
        ))

        # /app/data/ — selektiv
        if DATA_DIR.exists():
            for item in sorted(DATA_DIR.iterdir()):
                if item.is_dir():
                    if item.name in _EXCLUDE_DATA_SUBDIRS:
                        continue
                    for f in sorted(item.rglob("*")):
                        if f.is_file():
                            zf.write(f, "data/" + str(f.relative_to(DATA_DIR)))
                elif item.is_file() and item.name not in _EXCLUDE_DATA_FILES:
                    zf.write(item, f"data/{item.name}")

        # /app/templates/ — nur *.html und *.txt
        if TEMPLATE_DIR.exists():
            for f in sorted(TEMPLATE_DIR.iterdir()):
                if f.is_file() and f.suffix in (".html", ".txt"):
                    zf.write(f, f"templates/{f.name}")

    log.info("Backup created: %s (%d KB)", filename, len(buf.getvalue()) // 1024)
    return buf.getvalue(), filename


def validate_backup(zip_bytes: bytes) -> list[str]:
    """Prüft Grundstruktur. Returns Fehlerliste (leer = OK)."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = set(zf.namelist())
            if "data/settings.json" not in names:
                return ["Kein gültiges Gateway-Backup: data/settings.json fehlt."]
            return []
    except zipfile.BadZipFile:
        return ["Ungültige ZIP-Datei."]
    except Exception as exc:
        return [f"Lesefehler: {exc}"]


def restore_backup(zip_bytes: bytes) -> dict:
    """
    Stellt Backup wieder her.
    Returns {"ok": bool, "restored_files": int, "warnings": list[str], "error": str}
    """
    errors = validate_backup(zip_bytes)
    if errors:
        return {"ok": False, "error": errors[0], "restored_files": 0, "warnings": []}

    warnings: list[str] = []
    restored = 0

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for entry in zf.infolist():
                name = entry.filename
                if entry.is_dir() or name in ("README.txt",):
                    continue

                if name.startswith("data/"):
                    rel    = name[len("data/"):]
                    target = (DATA_DIR / rel).resolve()
                    # Pfad-Traversal-Schutz
                    if not str(target).startswith(str(DATA_DIR.resolve())):
                        warnings.append(f"Übersprungen (ungültiger Pfad): {name}")
                        continue
                    # Ausgeschlossene Unterverzeichnisse nicht wiederherstellen
                    parts = Path(rel).parts
                    if parts and parts[0] in _EXCLUDE_DATA_SUBDIRS:
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(name))
                    restored += 1

                elif name.startswith("templates/"):
                    rel    = name[len("templates/"):]
                    target = (TEMPLATE_DIR / rel).resolve()
                    if not str(target).startswith(str(TEMPLATE_DIR.resolve())):
                        warnings.append(f"Übersprungen (ungültiger Pfad): {name}")
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(name))
                    restored += 1

        # Settings live neu laden
        try:
            import settings_store
            settings_store.init()
        except Exception as exc:
            warnings.append(f"Settings-Reload: {exc} — Neustart empfohlen.")

        log.info("Backup restored: %d files, %d warnings", restored, len(warnings))
        return {"ok": True, "restored_files": restored, "warnings": warnings, "error": ""}

    except Exception as exc:
        log.error("Backup restore failed: %s", exc)
        return {"ok": False, "error": str(exc), "restored_files": restored, "warnings": warnings}
