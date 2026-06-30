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
    "USER_OVERRIDES": {},  # {email: {"user.jobTitle": "...", "custom.var": "..."}} — per-user overrides
    "WEBSITE_URL": "",  # Globale Website-URL für alle Nutzer (user.website)
    "CUSTOM_TEMPLATE_VARS": [],   # [{"name": "mobile", "entra_field": "mobilePhone"}, ...]
    "MAILBOX_CONFIG": {},  # {email: {"sig": true, "smime": true, "use_policy": true}} — empty = all mailboxes processed
    "TEMPLATE_POLICIES": {"sig": "default", "addin": "*"},  # Standard-Richtlinien: {sig: template_name, addin: "*"|[list]}
    "LE_DOMAIN": "",
    "LE_EMAIL": "",
    "LOG_RETENTION_DAYS": 30,
    "LOG_TIMEZONE": "Europe/Berlin",
    "SMIME_HARVEST_RCPT": "",
    "SMIME_TAG_ENCRYPTED": "verschlüsselt",
    "SMIME_TAG_ENCRYPTED_ENABLED": True,
    "SMIME_TAG_SIGNED": "signiert von {signer}",
    "SMIME_TAG_SIGNED_ENABLED": True,
    "SMIME_TAG_POSITION": "prepend",  # "prepend" or "append"
    "SMIME_STRIP_INBOUND": True,      # Strip S/MIME signature wrapper from inbound signed mails
    "SMIME_KEY_ENCRYPT": True,        # Encrypt stored private keys with SMIME_KEY_PASSWORD
    "SMIME_KEY_PASSWORD": "",         # AES-256 password for private key encryption (empty = no encryption)
    "ADMIN_USERS": [],               # List of UPN strings allowed to log in via Entra SSO
    "SSO_SESSION_SECRET": "",        # Auto-generated on first use; signs session cookies
    "ENC_TRIGGER": "#enc",            # Keyword in subject to request encryption
    "SMIME_SIGNING_ENABLED": True,    # Automatically sign outbound mails when a cert exists
    "NOSIG_TRIGGER": "#nosig",        # Keyword in subject to suppress signing for this mail
    # ── Re-injection ─────────────────────────────────────────────────────────
    "REINJECT_MODE": "smtp",       # "smtp", "graph", or "imap" (smtp587 = legacy alias for imap)
    "GRAPH_SMTP_FALLBACK": False,  # Allow SMTP fallback when Graph re-inject fails
    "RELAY_USER": "",              # Optional SMTP AUTH user (e.g. SES "apikey")
    "RELAY_PASSWORD": "",          # Optional SMTP AUTH password
    # ── SMTP submission (port 587) for inbound S/MIME from external senders ───
    "SMTP_SUBMIT_HOST": "smtp.office365.com",
    "SMTP_SUBMIT_PORT": 587,
    "SMTP_SUBMIT_USER": "",          # EXO mailbox for SMTP AUTH envelope sender
    "SMTP_SUBMIT_PASSWORD": "",      # Basic auth fallback (if no OAuth)
    "SMTP_SUBMIT_CLIENT_ID": "",     # Optional: separate app reg with SMTP.SendAsApp
    "SMTP_SUBMIT_CLIENT_SECRET": "", # Secret for SMTP_SUBMIT_CLIENT_ID
    "IMAP_ACCESS_CONFIGURED": False, # True after New-ServicePrincipal + Add-MailboxPermission ran
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
    "SMIME_RULES_CREATED": False,
    "BOOTSTRAP_CLIENT_ID": "",   # Client-ID der eigenen Bootstrap-App-Registrierung für den Setup-Login
    "BOOTSTRAP_REDIRECT_URIS": [],  # Tatsächlich in Azure registrierte Redirect-URIs (wird nach jedem Patch aktualisiert)
    # ── Notifications & scheduler ─────────────────────────────────────────────
    "NOTIFICATION_MAILBOX": "",      # Mailbox receiving alerts + reports (also used as FROM)
    "DAILY_REPORT_ENABLED": False,   # Send daily stats email
    "DAILY_REPORT_TIME": "08:00",    # HH:MM in LOG_TIMEZONE
    "CERT_WARN_DAYS": 14,            # Warn this many days before S/MIME cert expiry
    "LE_RENEW_DAYS": 7,              # Attempt LE renewal this many days before expiry
    "NOTIFY_STARTUP": None,          # None/True = send; False = suppress startup notification
    "NOTIFY_SMIME_EXPIRY": None,     # None/True = send; False = suppress S/MIME expiry admin alert
    "NOTIFY_CERT_RENEWAL": None,     # None/True = send; False = suppress renewal success/failure
    "NOTIFY_LE_EVENTS": None,        # None/True = send; False = suppress LE cert events
    # ── Azure Key Vault (S/MIME private key storage) ──────────────────────────
    "KEYVAULT_URL": "",                 # e.g. https://myvault.vault.azure.net — empty = local key files
    "KV_KEY_MODE": "fallback",          # "fallback" = exportable + local backup; "strict" = no export, no backup
    "KV_KEY_STATUS": {},                # {email: {"exists": bool, "checked": "ISO8601"}} — cached KV key status
    # ── ACME ─────────────────────────────────────────────────────────────────
    "ACME_REPLY_METHOD": "graph",       # "graph" or "direct_smtp" — how challenge replies are sent
    # ── S/MIME lifecycle management ───────────────────────────────────────────
    "GATEWAY_EXTERNAL_URL": "",      # e.g. https://mail.company.com:8080 — used in renewal links
    "CERT_RENEWAL_THRESHOLDS": [30, 14, 7, 1],  # Notify user at these days-before-expiry
    "CA_USER_CONFIG": {},            # {email: {backend, portal_url, notify_user}}
    # ── Notifications (extended) ──────────────────────────────────────────────
    "NOTIFICATIONS_ENABLED": True,           # Global on/off switch for all notifications
    "NOTIFICATION_RECIPIENTS": [],           # List of mailbox emails for notifications
    "NOTIFICATION_DG_EMAIL": "",             # PrimarySmtpAddress of notification DG (auto-set)
    "NOTIFY_LOCAL_ADMIN_LOGIN": None,        # None/True = send; False = suppress local admin login notification
    # ── Outlook Add-in ───────────────────────────────────────────────────────
    "ADDIN_ENABLED": False,             # Show add-in setup section and serve manifest
    "ADDIN_BASE_URL": "",               # External public URL override (e.g. https://sig.zarenko.net)
    "STRIP_CLIENT_SIGS": True,          # Strip client-generated Outlook signatures before injection
    "SIG_STRIP_MIN_MATCH_PCT": 50,      # Fingerprint match threshold % for signature stripping
    "SKIP_DUPLICATE_SIG": True,         # Skip re-injection if gateway signature already in compose area
    "GATEWAY_NAME": "EXO Signature Gateway",  # Prefix for EXO connectors, rules, distribution groups
    "APP_POOL": [],   # [{client_id, client_secret, label}] — leer = primäre CLIENT_ID/SECRET nutzen
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
                log.error("Failed to load %s: %s — trying backup", SETTINGS_FILE, exc)
                bak = SETTINGS_FILE.with_suffix(".bak")
                if bak.exists():
                    try:
                        merged.update(json.loads(bak.read_text()))
                        log.warning("Loaded settings from backup %s", bak)
                    except Exception as bak_exc:
                        log.error("Backup also unreadable: %s — using defaults", bak_exc)
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
        if not _data:
            init()
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
    # Atomic write: temp → rename so a crash mid-write never corrupts settings.
    # Keep a .bak of the last known-good state for recovery.
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(to_write, indent=2, ensure_ascii=False))
    if SETTINGS_FILE.exists():
        SETTINGS_FILE.replace(SETTINGS_FILE.with_suffix(".bak"))
    tmp.replace(SETTINGS_FILE)
    log.info("Settings saved to %s", SETTINGS_FILE)
