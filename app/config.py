import json
import os


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise EnvironmentError(f"Required environment variable '{name}' is not set")
    return val


def _optional(name: str, default: str = "") -> str:
    # Ein GESETZTES, aber leeres Env-Var (docker-compose `${VAR:-}`) liefert mit
    # os.environ.get(name, default) den leeren String STATT des Defaults. Bei
    # WEBUI_PASSWORD hätte das nur ein leeres Passwort akzeptiert und admin/admin
    # ausgesperrt; bei numerischen/JSON-Werten wäre `int("")`/`json.loads("")`
    # abgestürzt. Ein leerer Wert wird deshalb wie „nicht gesetzt" behandelt.
    return os.environ.get(name) or default


# ── Secrets / bootstrap ────────────────────────────────────────────────────────
# Azure credentials are now optional at startup — they can be configured via
# the setup wizard and stored in settings_store.  Existing deployments that
# already set env vars continue to work (env takes precedence).
TENANT_ID = _optional("TENANT_ID", "")
CLIENT_ID = _optional("CLIENT_ID", "")
CLIENT_SECRET = _optional("CLIENT_SECRET", "")
EXO_SMARTHOST = _optional("EXO_SMARTHOST", "")

# Web UI auth — secret key auto-generates a fallback so the first-run wizard
# works without any env vars.
WEBUI_SECRET_KEY = _optional("WEBUI_SECRET_KEY") or os.urandom(32).hex()
WEBUI_PASSWORD = _optional("WEBUI_PASSWORD", "admin")
SMIME_KEY_PASSWORD = _optional("SMIME_KEY_PASSWORD", "")  # Empty = no encryption

# ── Structural (fixed at container start) ──────────────────────────────────────
SMTP_PORT = int(_optional("SMTP_PORT", "25"))
SMTP_TLS_CERT = _optional("SMTP_TLS_CERT", "/app/certs/cert.pem")
SMTP_TLS_KEY = _optional("SMTP_TLS_KEY", "/app/certs/key.pem")
WEBUI_PORT = int(_optional("WEBUI_PORT", "8080"))
TEMPLATE_DIR = _optional("TEMPLATE_DIR", "/app/templates")

# Datenverzeichnis — Einstellungen, Schluessel, Datenbanken, ACME-Zustand.
#
# Der Pfad stand als Literal `"/app/data"` an 30 Stellen in 18 Modulen. Das war
# nicht bloss unschoen: Er ist der Ort, an dem Geheimnisse liegen, und wer ihn
# nicht an einer Stelle sehen kann, kann ihn auch nicht an einer Stelle
# absichern oder verlegen. In den Tests musste jedes Modul einzeln umgebogen
# werden (`tests/test_seiten.py` fuehrt dafuer eine Liste), und wer ein Modul
# vergass, schrieb waehrend eines Testlaufs in das ECHTE Verzeichnis — am
# 26.07.2026 beinahe passiert.
#
# Ueber `_optional` und damit aus der Umgebung setzbar, genau wie TEMPLATE_DIR.
DATA_DIR = _optional("DATA_DIR", "/app/data")

# ── Env seeds for settings that live in settings.json ─────────────────────────
# Used as initial values when settings.json does not yet exist (migration helper)
_ENV_SEEDS: dict = {
    "EXO_PORT": int(_optional("EXO_PORT", "25")),
    "FALLBACK_ON_ERROR": _optional("FALLBACK_ON_ERROR", "true").lower() == "true",
    "LOG_LEVEL": _optional("LOG_LEVEL", "INFO").upper(),
    "WEBUI_USERNAME": _optional("WEBUI_USERNAME", "admin"),
    "USER_WEBSITES": json.loads(_optional("USER_WEBSITES", "{}")),
    "USER_BOOKINGS": json.loads(_optional("USER_BOOKINGS", "{}")),
    "SENT_ITEMS_UPDATE": _optional("SENT_ITEMS_UPDATE", "false").lower() == "true",
}

# ── Support Upload ────────────────────────────────────────────────────────────
# Azure Blob Storage SAS-URL für Support-Bundles.
# Format: "https://{account}.blob.core.windows.net/{container}/{blob_name}?sv=...&sig=..."
# {blob_name} wird beim Upload durch den generierten Dateinamen ersetzt.
# SAS-Berechtigungen: Create + Write (sp=cw), Container-Scope (sr=c).
# In docker-compose.yml setzen: SUPPORT_BLOB_URL_TEMPLATE=https://...
SUPPORT_BLOB_URL_TEMPLATE = _optional("SUPPORT_BLOB_URL_TEMPLATE", "")

# ── Version ───────────────────────────────────────────────────────────────────
def _read_version() -> str:
    for path in ("/app/VERSION", "VERSION"):
        try:
            with open(path) as f:
                return f.read().strip()
        except FileNotFoundError:
            continue
    return "dev"

VERSION = _read_version()
