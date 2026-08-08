"""Gemeinsames Fundament der Weboberfläche — Anmeldung, Vorlagen, Protokoll.

WARUM ES DIESE DATEI GIBT
-------------------------
`app.py` war 5.655 Zeilen mit 232 Routen. Zum Aufteilen braucht jedes
Routenmodul dieselben paar Dinge: die Anmeldeprüfung, das Vorlagenverzeichnis,
den Protokollkanal. Holte es sie aus `app.py`, entstünde ein Ringschluss —
`app.py` bindet die Routenmodule ein, die Routenmodule importieren `app.py`.

Deshalb liegt der gemeinsame Kern HIER, und diese Datei importiert ihrerseits
KEIN Routenmodul und nicht `app.py`. Die Richtung ist damit eindeutig:

    app.py  ──bindet ein──>  routen/*.py  ──importiert──>  deps.py

⚠️ Diese Datei darf nichts aus `webui.app` oder `webui.routen` importieren.
Sonst ist der Ringschluss wieder da, und zwar als Importfehler beim Start —
also erst im Betrieb, nicht beim Schreiben.

ZUR ANMELDEPRÜFUNG
------------------
`_check_auth` und `_require_admin` bleiben DIESELBEN Objekte, die `app.py`
bisher führte (es importiert sie von hier und reicht sie weiter). Das ist kein
Zufall, sondern Bedingung: Die Tests hängen sich über
`app.dependency_overrides[_check_auth]` ein, und der Schlüssel ist das
Funktionsobjekt selbst. Eine Kopie hier und eine dort — und die Umgehung in den
Tests passte zu keinem `Depends` mehr; alle Aufrufe endeten in 401. Genau
dieser Fehler ist beim Bau von `test_speichern_schutz.py` schon einmal
aufgetreten und dort dokumentiert.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

import config
import settings_store
import sso as sso_mod

log = logging.getLogger("webui")

security = HTTPBasic(auto_error=False)

_STATIC_DIR = Path(__file__).parent / "static"
_TEMPLATE_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
templates.env.globals["version"] = config.VERSION


def _gateway_name() -> str:
    """Name des Gateways — bei jedem Aufruf frisch gelesen, damit eine Änderung
    ohne Neustart wirkt."""
    return settings_store.get("GATEWAY_NAME") or "EXO Signature Gateway"


class _NotAuthenticated(Exception):
    """Nicht angemeldet. Der Behandler hängt an der Anwendung in `app.py` —
    er braucht `@app.exception_handler` und gehört deshalb dorthin."""

    def __init__(self, is_api: bool = False):
        self.is_api = is_api


# ── Passwörter ────────────────────────────────────────────────────────────────

def _hash_password(password: str) -> str:
    """Liefert `pbkdf2:sha256:<salt>:<hash>`."""
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
    return f"pbkdf2:sha256:{salt}:{key.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        _, alg, salt, key_hex = stored.split(":", 3)
        assert alg == "sha256"
    except Exception:
        return False
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
    return hmac.compare_digest(key.hex(), key_hex)


def _check_password(password: str) -> bool:
    """Gegen den gespeicherten Hash prüfen, ersatzweise gegen die Umgebung."""
    stored_hash = settings_store.get("ADMIN_PASSWORD_HASH") or ""
    if stored_hash:
        return _verify_password(password, stored_hash)
    return secrets.compare_digest(password.encode(), config.WEBUI_PASSWORD.encode())


# ── Anmeldung ─────────────────────────────────────────────────────────────────

def _get_session_user(request: Request) -> str | None:
    """Kennung aus dem Sitzungskeks — oder, für das Office-Add-in, aus dem
    Kopffeld `X-Addin-Session`.

    Die Anmeldung des Add-ins läuft in einem Office-Dialog, dessen Keks NICHT
    zuverlässig mit der Aufgabenleiste geteilt wird. Der Dialog reicht das
    signierte Sitzungsmerkmal deshalb zurück, und die Aufgabenleiste schickt es
    hier als Kopffeld. Dasselbe Merkmal, dieselbe Prüfung — nur ein anderer Weg.
    """
    cookie = (request.cookies.get(sso_mod.SESSION_COOKIE)
              or request.headers.get("X-Addin-Session"))
    if not cookie:
        return None
    payload = sso_mod.verify_session_cookie(cookie)
    if not payload:
        return None
    return payload.get("u")


def _get_session_role(request: Request) -> str:
    """Rolle aus dem Sitzungskeks. HTTP-Basic gilt immer als Verwaltung."""
    cookie = request.cookies.get(sso_mod.SESSION_COOKIE)
    if cookie:
        payload = sso_mod.verify_session_cookie(cookie)
        if payload:
            return payload.get("r", sso_mod.ROLE_ADMIN)
    return sso_mod.ROLE_ADMIN


def _check_auth(request: Request,
                credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """Sitzungskeks → örtliches HTTP-Basic → 401 bzw. Weiterleitung."""
    user = _get_session_user(request)
    if user:
        return user
    # Örtliche Verwaltung als Notzugang — bleibt immer erreichbar.
    if credentials and credentials.username and credentials.password:
        username = settings_store.get("WEBUI_USERNAME") or "admin"
        if (secrets.compare_digest(credentials.username.encode(), username.encode())
                and _check_password(credentials.password)):
            return credentials.username
    path = request.url.path
    is_api = path.startswith("/api/") or path.startswith("/log/")
    raise _NotAuthenticated(is_api=is_api)


def _require_admin(request: Request, user: str = Depends(_check_auth)) -> str:
    """Verlangt die Verwaltungsrolle; Bearbeiter bekommen 403."""
    if _get_session_role(request) != sso_mod.ROLE_ADMIN:
        raise HTTPException(403, "Admin-Berechtigung erforderlich")
    return user
