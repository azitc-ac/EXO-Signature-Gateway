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
import collections
import queue as _queue_mod
import threading
import time as _time

log = logging.getLogger("webui")

security = HTTPBasic(auto_error=False)

_STATIC_DIR = Path(__file__).parent / "static"
_TEMPLATE_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
templates.env.globals["version"] = config.VERSION


def _lernmodus_rest_min() -> int:
    """Restminuten des SMTP-Lernmodus — 0, wenn er nicht läuft.

    ⚠️ Bewusst ein Jinja-Global und kein Kontextwert. Der Hinweis auf einen
    laufenden Lernmodus soll auf JEDER Seite erscheinen; hinge er am Kontext,
    müsste ihn jede Route mitgeben, und die eine, die es vergisst, verbirgt
    ausgerechnet den Zustand, in dem sich die Freigabe von selbst erweitert.
    Ein Global kann keine Route vergessen.

    Als Funktion, nicht als Wert: Ein bei Prozessstart berechneter Zeitpunkt
    wäre Minuten später falsch.
    """
    try:
        import smtp_relay
        from datetime import datetime, timezone
        bis = smtp_relay.lernmodus_bis()
        if not bis:
            return 0
        return max(1, round((bis - datetime.now(timezone.utc)).total_seconds() / 60))
    except Exception:                                   # noqa: BLE001
        return 0                                        # nie die Seite kippen


templates.env.globals["lernmodus_rest_min"] = _lernmodus_rest_min


def _smtp_relay_aktiv() -> bool:
    """Ob der SMTP-Relay-Weg eingeschaltet ist — steuert den Menüpunkt.

    Wie `lernmodus_rest_min` bewusst ein Jinja-Global und kein Kontextwert: der
    Menüpunkt steht in base.html und soll ohne Zutun jeder einzelnen Route
    erscheinen bzw. verschwinden. Bei jedem Aufruf frisch gelesen, damit ein
    Umschalten ohne Neustart wirkt.
    """
    try:
        return bool(settings_store.get("SMTP_RELAY_ENABLED"))
    except Exception:                                   # noqa: BLE001
        return False                                    # nie die Seite kippen


templates.env.globals["smtp_relay_aktiv"] = _smtp_relay_aktiv


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


# ── Protokollstrom im Arbeitsspeicher ────────────────────────────────────────
#
# Aus `app.py` hierher verschoben (21.08.2026). Beide Seiten brauchen sie: Dort
# wird der Handler eingehängt, der die Zeilen einsammelt, hier lesen die
# Betriebs-Routen sie aus. Ein Import aus `app.py` heraus wäre ein Zirkel,
# denn `app.py` bindet die Routenmodule selbst ein.
#
# ⚠️ Die kurzlebigen Marken für `/log/stream` sind kein Beiwerk: Ein
# EventSource kann keine Anmeldedaten mitschicken, deshalb bekommt er eine
# Marke mit einer Stunde Laufzeit statt eines dauerhaften Zugangs.
# ── Live log streaming ─────────────────────────────────────────────────────────
_LOG_BUFFER: collections.deque = collections.deque(maxlen=500)
_LOG_SUBSCRIBERS: list[_queue_mod.Queue] = []
_LOG_SUBSCRIBERS_LOCK = threading.Lock()


class _MemoryLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        line = self.format(record)
        _LOG_BUFFER.append(line)
        with _LOG_SUBSCRIBERS_LOCK:
            for q in _LOG_SUBSCRIBERS:
                try:
                    q.put_nowait(line)
                except _queue_mod.Full:
                    pass


_mem_handler = _MemoryLogHandler()
_mem_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
))
logging.getLogger().addHandler(_mem_handler)

# Short-lived tokens for /log/stream (EventSource cannot send HTTP Basic Auth)
_LOG_TOKENS: dict[str, float] = {}


def _make_log_token() -> str:
    token = secrets.token_urlsafe(32)
    _LOG_TOKENS[token] = _time.time() + 3600
    # Purge expired tokens
    expired = [k for k, exp in _LOG_TOKENS.items() if _time.time() > exp]
    for k in expired:
        _LOG_TOKENS.pop(k, None)
    return token


def _check_log_token(token: str) -> bool:
    exp = _LOG_TOKENS.get(token)
    return exp is not None and _time.time() < exp










