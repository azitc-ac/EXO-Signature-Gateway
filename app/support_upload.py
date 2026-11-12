"""
Support-Bundle-Builder.

Erstellt ein Diagnose-Paket (Logs, Einstellungen mit maskierten Secrets,
Audit-Events, ACME-Status) als ZIP. Der Versand erfolgt über den Provider-Hub
(siehe hub_client.py); dieses Modul liefert nur das Bundle (build_bundle) und
wird außerdem vom lokalen Download genutzt.
"""

import io
import json
import logging
import os
import platform
import re
import secrets
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

log = logging.getLogger(__name__)

# Maskierung — zweistufig (seit v1.11.47):
#
# 1. settings_store.public_view() — die EINE Geheimnisliste (SECRET_KEYS). Bis
#    v1.11.46 stand hier nur ein eigenes Suchmuster; damit gingen HUB_API_KEY,
#    LICENSE_KEY, DIGICERT_API_KEY, RELAY_TARGET_PW, DIREKT_SMARTHOST_PW und
#    APP_POOL (Liste mit client_secret — Listen wurden nicht durchsucht) im
#    Klartext ins Paket, obwohl die AVV maskierte Geheimnisse zusagt.
# 2. Zusätzlich dieses Muster auf JEDER Ebene, auch in Listen — für Schlüssel,
#    die (noch) nicht in SECRET_KEYS stehen, etwa veraltete Einträge.
_SENSITIVE = re.compile(
    r"(secret|passw|pfx|kv_client|token|api_?key|license_key|_pw$|_pw_)", re.IGNORECASE
)
_MASKE = "***"

# Zugangsdaten in Adressen (http://nutzer:passwort@proxy:3128)
_URL_ZUGANG = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")

# Inhalte statt Konfiguration: Abwesenheitstexte der Postfächer und
# Benutzerangaben (Telefon, Titel). Für die Fehlersuche genügt, DASS es sie gibt.
_NUR_ANZAHL = frozenset({"_OOO_STATE", "USER_OVERRIDES"})

# Mailprotokoll im Paket: so viele Tage zurück (AVV Ziffer 3.2)
AUDIT_TAGE = 7
AUDIT_HOECHSTENS = 1000

# Geheimnisse, die in Protokollzeilen landen könnten
_LOG_GEHEIM = [
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*"), "<JWT>"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{16,}"), r"\1 <maskiert>"),
    (re.compile(r"(?i)\b(client_secret|password|passwort|api_key|apikey|token)(\s*[=:]\s*)[^\s,;&\"']{4,}"),
     r"\1\2<maskiert>"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "<privater Schlüssel entfernt>"),
]

# Max. Größe der einzelnen Log-Dateien im Bundle (Bytes)
_MAX_LOGFILE_BYTES = 4 * 1024 * 1024   # 4 MB
_MAX_ROTATED_FILES = 3                  # letzte N Rotations-Logs


# ── Datensammlung ─────────────────────────────────────────────────────────────

def _maskieren(wert, schluessel: str = ""):
    """Rekursiv: Werte unter sensiblen Schlüsseln → ***, Zugangsdaten in
    Adressen entfernt — in Wörterbüchern UND Listen."""
    if schluessel and _SENSITIVE.search(schluessel):
        return _MASKE if wert not in (None, "", [], {}) else wert
    if isinstance(wert, dict):
        return {k: _maskieren(v, str(k)) for k, v in wert.items()}
    if isinstance(wert, list):
        return [_maskieren(v) for v in wert]
    if isinstance(wert, str):
        return _URL_ZUGANG.sub(r"\1***@", wert)
    return wert


def _sanitize_settings(data: dict) -> dict:
    """Kopie für das Paket: Geheimnisse aus SECRET_KEYS maskiert (public_view),
    dazu das Suchmuster auf jeder Ebene, Inhalte nur als Anzahl."""
    import settings_store
    sichtbar = dict(data)
    for k in settings_store.SECRET_KEYS:
        if sichtbar.get(k) not in (None, "", [], {}):
            sichtbar[k] = _MASKE
    for k in _NUR_ANZAHL:
        if k in sichtbar:
            v = sichtbar[k]
            n = len(v) if isinstance(v, (dict, list)) else int(bool(v))
            sichtbar[k] = f"<{n} Einträge, Inhalt nicht übertragen>"
    return _maskieren(sichtbar)


def _log_bereinigen(text: str) -> str:
    """Tokens, Passwörter und Schlüssel aus Protokolltext entfernen."""
    for muster, ersatz in _LOG_GEHEIM:
        text = muster.sub(ersatz, text)
    return _URL_ZUGANG.sub(r"\1***@", text)


def _system_info() -> dict:
    import socket
    import shutil
    disk = shutil.disk_usage(config.DATA_DIR)
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
    acme_dir = Path(config.DATA_DIR) / "acme"
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
            f"  audit_events.jsonl        — Mail-Audit-Log der letzten {AUDIT_TAGE} Tage\n"
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

        # Audit-Events: wirklich nur die letzten AUDIT_TAGE Tage. Bis v1.11.46
        # stand hier „letzte 7 Tage“, abgefragt wurden aber die neuesten 1000
        # Einträge ohne Datumsgrenze — bei wenig Verkehr bis zu zwei Jahre.
        von = (datetime.now(timezone.utc) - timedelta(days=AUDIT_TAGE)).strftime("%Y-%m-%d")
        events = mail_audit.query_events(date_from=von, limit=AUDIT_HOECHSTENS)
        lines = [json.dumps(e, ensure_ascii=False) for e in events]
        zf.writestr("audit_events.jsonl", "\n".join(lines))

        # Runtime-Log (In-Memory-Buffer)
        zf.writestr("logs/runtime.txt", _log_bereinigen("\n".join(runtime_log_lines)))

        # Persistente Log-Dateien
        current_log = LOG_DIR / _LOG_FILENAME
        zf.writestr(f"logs/{_LOG_FILENAME}", _log_bereinigen(_read_log_file(current_log)))

        rotated = sorted(
            [f for f in LOG_DIR.iterdir() if _ROTATED_RE.match(f.name)],
            reverse=True,
        )[:_MAX_ROTATED_FILES]
        for f in rotated:
            zf.writestr(f"logs/{f.name}", _log_bereinigen(_read_log_file(f)))

    return buf.getvalue(), blob_name
