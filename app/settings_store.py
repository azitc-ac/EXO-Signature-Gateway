import json
import logging
from pathlib import Path
from threading import RLock

log = logging.getLogger(__name__)

SETTINGS_FILE = Path("/app/data/settings.json")

DEFAULTS: dict = {
    # ── Operational ──────────────────────────────────────────────────────────
    "EXO_PORT": 25,
    "FALLBACK_ON_ERROR": True,
    "LOG_LEVEL": "INFO",
    "WEBUI_USERNAME": "admin",
    "LOOP_HEADER": "X-Sig-Applied",
    "SENT_ITEMS_UPDATE": False,
    "USER_WEBSITES": {},
    "USER_BOOKINGS": {},
    "LE_DOMAIN": "",
    "LE_EMAIL": "",
    # ── Re-injection ─────────────────────────────────────────────────────────
    "REINJECT_MODE": "smtp",       # "smtp" or "graph"
    "RELAY_USER": "",              # Optional SMTP AUTH user (e.g. SES "apikey")
    "RELAY_PASSWORD": "",          # Optional SMTP AUTH password
    # ── Setup wizard ─────────────────────────────────────────────────────────
    "SETUP_COMPLETE": False,
    "ADMIN_PASSWORD_HASH": "",   # pbkdf2:sha256:<salt>:<hash> — empty = use WEBUI_PASSWORD env
    "PUBLIC_HOSTNAME": "",
    "TENANT_ID": "",             # Can be set via env var (takes precedence) or wizard
    "CLIENT_ID": "",             # Can be set via env var (takes precedence) or wizard
    "CLIENT_SECRET": "",         # Can be set via env var (takes precedence) or wizard
    "EXO_SMARTHOST": "",         # Can be set via env var (takes precedence) or wizard
    "TENANT_DOMAIN": "",         # Auto-discovered (e.g. "contoso.onmicrosoft.com")
    "AZURE_APP_CREATED": False,
    "EXO_CONNECTOR_CREATED": False,
    "BOOTSTRAP_CLIENT_ID": "",   # Client-ID der eigenen Bootstrap-App-Registrierung für den Setup-Login
}

_lock = RLock()
_data: dict = {}


def init(env_seed: dict | None = None) -> None:
    global _data
    with _lock:
        merged = dict(DEFAULTS)
        if env_seed:
            merged.update({k: v for k, v in env_seed.items() if k in DEFAULTS})
        if SETTINGS_FILE.exists():
            try:
                merged.update(json.loads(SETTINGS_FILE.read_text()))
            except Exception as exc:
                log.error("Failed to load %s: %s", SETTINGS_FILE, exc)
        _data = merged
        log.info("Settings loaded (persisted file: %s)", SETTINGS_FILE.exists())


def get(key: str):
    with _lock:
        return _data.get(key, DEFAULTS.get(key))


def get_all() -> dict:
    with _lock:
        return dict(_data)


def update(patch: dict) -> None:
    """Update and persist settings. Only keys present in DEFAULTS are accepted."""
    with _lock:
        _data.update({k: v for k, v in patch.items() if k in DEFAULTS})
        _save()


def force_update(patch: dict) -> None:
    """Update and persist settings without DEFAULTS key guard (internal use)."""
    with _lock:
        _data.update(patch)
        _save()


def _save() -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Write all known DEFAULTS keys; unknown keys in _data are also persisted
    to_write = {k: _data.get(k, DEFAULTS[k]) for k in DEFAULTS}
    # Also persist any extra keys that ended up in _data (forward compat)
    for k, v in _data.items():
        if k not in to_write:
            to_write[k] = v
    SETTINGS_FILE.write_text(json.dumps(to_write, indent=2, ensure_ascii=False))
    log.info("Settings saved to %s", SETTINGS_FILE)
