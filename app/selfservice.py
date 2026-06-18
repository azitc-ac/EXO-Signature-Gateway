"""
Self-service upload tokens for S/MIME certificate renewal.

Allows users to upload a renewed PKCS#12 via a time-limited, token-authenticated
link — without needing admin credentials for the gateway web UI.

Storage: /app/data/selfservice_tokens.json
  {email: {token, expires_iso}}   — one token per user at a time

Tokens are valid for TOKEN_TTL_DAYS days and can be renewed by the admin at any time.
"""
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock

log = logging.getLogger(__name__)

TOKEN_TTL_DAYS = 30
_TOKEN_FILE = Path("/app/data/selfservice_tokens.json")
_lock = RLock()

# In-memory index: {token_hex → {email, expires_iso}}
_by_token: dict[str, dict] = {}
_loaded = False


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    if not _TOKEN_FILE.exists():
        return
    try:
        raw: dict = json.loads(_TOKEN_FILE.read_text())
        now = _now_iso()
        for email, v in raw.items():
            if v.get("expires", "") > now:
                _by_token[v["token"]] = {"email": email, "expires": v["expires"]}
    except Exception as exc:
        log.warning("selfservice: could not load tokens: %s", exc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _persist() -> None:
    data = {
        info["email"]: {"token": tok, "expires": info["expires"]}
        for tok, info in _by_token.items()
    }
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(json.dumps(data, indent=2))


def generate_token(email: str) -> str:
    """Generate (or replace) a self-service upload token for *email*.
    Returns the new token. Any previous token for this email is revoked.
    """
    with _lock:
        _load()
        # Revoke previous token for this email
        stale = [t for t, i in _by_token.items() if i["email"] == email]
        for t in stale:
            del _by_token[t]
        token = secrets.token_hex(24)
        expires = (datetime.now(timezone.utc) + timedelta(days=TOKEN_TTL_DAYS)).isoformat()
        _by_token[token] = {"email": email, "expires": expires}
        _persist()
        log.info("selfservice: token generated for %s (expires %s)", email, expires[:10])
        return token


def validate_token(token: str) -> str | None:
    """Return the email address if *token* is valid and unexpired, else None."""
    with _lock:
        _load()
        info = _by_token.get(token)
        if not info:
            return None
        if _now_iso() > info["expires"]:
            del _by_token[token]
            _persist()
            log.info("selfservice: expired token used for %s", info.get("email"))
            return None
        return info["email"]


def get_token_info(email: str) -> dict | None:
    """Return {token, expires} for *email*'s current valid token, or None."""
    with _lock:
        _load()
        now = _now_iso()
        for token, info in _by_token.items():
            if info["email"] == email and info["expires"] > now:
                return {"token": token, "expires": info["expires"][:10]}
        return None


def revoke_token(email: str) -> None:
    with _lock:
        _load()
        stale = [t for t, i in _by_token.items() if i["email"] == email]
        for t in stale:
            del _by_token[t]
        _persist()
