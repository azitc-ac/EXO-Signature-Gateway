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


def _abwesenheit_aktiv() -> bool:
    """Ob „Zentrale Abwesenheiten verwalten" an ist — steuert Menüpunkt, Spalte
    und Standard-Zeile. Jinja-Global wie `smtp_relay_aktiv`, frisch je Aufruf."""
    try:
        import abwesenheit
        return abwesenheit.funktion_aktiv()
    except Exception:                                   # noqa: BLE001
        return False                                    # nie die Seite kippen


templates.env.globals["abwesenheit_aktiv"] = _abwesenheit_aktiv


def _utc_anzeige(iso: str) -> str:
    """„2026-11-06T23:03:59Z“ → „06.11.2026 23:03 UTC“; Unlesbares unverändert."""
    from datetime import datetime
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").strftime("%d.%m.%Y %H:%M UTC")
    except ValueError:
        return iso


def _bypass_gemeldet() -> dict | None:
    """Fürs Banner: geht ausgehende Post gerade ohne Signatur raus? Zwei Quellen —
    der Heartbeat des Wächters (`bypass_active`) UND die unabhängige EXO-
    Regelabfrage des Schedulers (`rule_state == Disabled`). Letztere fängt einen
    vergessenen Bypass auch dann, wenn der Wächter selbst tot ist.
    Rückgabe {seit} oder None.
    """
    try:
        import waechter_state
        import waechter_register
        st = waechter_state.lesen()
        # rule_state (global, vom Scheduler) fängt einen Bypass auch dann, wenn
        # ALLE Wächter tot sind; die Wächter-Heartbeats fangen ihn früher.
        # Eine abgeschaltete Regel ist nur dann ein Bypass, wenn ein Wächter
        # eingerichtet ist. Ohne ihn ist sie aus, solange kein Postfach
        # aktiviert ist — das ist der Normalfall nach dem Assistenten.
        regel_aus = (settings_store.get("WATCHDOG_ENABLED") is True
                     and st.get("rule_state") == "Disabled")
        if regel_aus or waechter_register.irgendein_bypass_aktiv():
            seit = st.get("rule_checked") or ""
            juengster = max((w.get("last_seen") or "" for w in waechter_register.liste()),
                            default="")
            return {"seit": _utc_anzeige(juengster or seit or st.get("last_seen") or ""),
                    "geprueft": _utc_anzeige(st.get("rule_checked") or "")}
    except Exception:                                       # noqa: BLE001
        pass
    return None


templates.env.globals["bypass_gemeldet"] = _bypass_gemeldet


# ── Betriebszustände als Band auf JEDER Seite (wie Lernmodus und Bypass) ─────
# Bis v1.10.86 standen Wartungsmodus, Zustellfehler und Wächter-Ausfall nur auf
# der Übersicht. Wer auf einer anderen Seite arbeitete, sah sie nicht — und
# ein vergessener Wartungsmodus hält stillschweigend alle Post zurück.

def _wartung_gemeldet() -> dict | None:
    """Wartungsmodus an? Rückgabe {zurueck: Anzahl wegen Wartung gehaltener
    Mails} oder None. Zustellfehler zählen nicht mit — sie haben ihr eigenes Band."""
    try:
        if not settings_store.get("MAINTENANCE_MODE"):
            return None
        import held_mails
        return {"zurueck": max(0, held_mails.count() - held_mails.count("delivery_failed"))}
    except Exception:                                       # noqa: BLE001
        return None


templates.env.globals["wartung_gemeldet"] = _wartung_gemeldet


def _zustellfehler_anzahl() -> int:
    """Mails, die nicht zugestellt werden konnten und in der Warteschlange warten."""
    try:
        import held_mails
        return held_mails.count("delivery_failed")
    except Exception:                                       # noqa: BLE001
        return 0


templates.env.globals["zustellfehler_anzahl"] = _zustellfehler_anzahl


def _exo_abgewiesen_gemeldet() -> dict | None:
    """Wurde in den letzten 24 h eigene Exchange-Post an Port 25 abgewiesen?"""
    try:
        import exo_erkennung
        return exo_erkennung.abweisungen_gemeldet()
    except Exception:                                       # noqa: BLE001
        return None


templates.env.globals["exo_abgewiesen_gemeldet"] = _exo_abgewiesen_gemeldet


def _ooo_zugriff_gemeldet() -> dict | None:
    """Abwesenheit: seit über 2 h kein Zugriff auf Postfächer (abwesenheit.zugriff_gemeldet)."""
    try:
        import abwesenheit
        return abwesenheit.zugriff_gemeldet()
    except Exception:                                       # noqa: BLE001
        return None


templates.env.globals["ooo_zugriff_gemeldet"] = _ooo_zugriff_gemeldet

# Der Wächter meldet sich alle 5 Minuten; nach 15 gilt er als verstummt.
WAECHTER_STILL_MIN = 15


def _waechter_gemeldet() -> dict | None:
    """Ist der eingerichtete Bypass-Wächter funktionsunfähig?

    Zwei Fälle: Er meldet sich nicht mehr (`still`), oder er lebt, kann die
    Transportregel aber nicht schalten (`exo`). In beiden Fällen gäbe es im
    Ausfall keinen Bypass. Ein AKTIVER Bypass ist nicht dieser Fall — den meldet
    `bypass_gemeldet` mit eigenem Band; hier entfiele er sonst doppelt.
    Rückgabe {art, zuletzt, fehler} oder None."""
    try:
        if settings_store.get("WATCHDOG_ENABLED") is not True or _bypass_gemeldet():
            return None
        import waechter_register
        import waechter_state
        from datetime import datetime, timezone
        st = waechter_state.lesen()
        waechter = waechter_register.liste()
        zuletzt = max((w.get("last_seen") or "" for w in waechter), default="") \
            or (st.get("last_seen") or "")
        fehler = next((w.get("exo_error") for w in waechter if w.get("exo_error")), "") \
            or (st.get("exo_error") or "")
        anzeige = _utc_anzeige(zuletzt) if zuletzt else "noch nie"
        if fehler:
            return {"art": "exo", "zuletzt": anzeige, "fehler": str(fehler)[:200]}
        try:
            alter = (datetime.now(timezone.utc) - datetime.strptime(
                zuletzt, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds()
        except ValueError:
            alter = None                                    # nie gesehen / unlesbar
        if alter is None or alter > WAECHTER_STILL_MIN * 60:
            return {"art": "still", "zuletzt": anzeige, "fehler": ""}
    except Exception:                                       # noqa: BLE001
        pass
    return None


templates.env.globals["waechter_gemeldet"] = _waechter_gemeldet


def _aufgaben_offen() -> list[dict]:
    """Offene Aufgaben für den Sammelpunkt oben rechts und die Banner
    (`aufgaben.py`). Jinja-Global wie `bypass_gemeldet`: keine Route kann ihn
    vergessen."""
    try:
        import aufgaben
        return aufgaben.offene()
    except Exception as exc:                                # noqa: BLE001
        log.warning("Aufgaben nicht lesbar: %s", exc)       # nie die Seite kippen
        return []


templates.env.globals["aufgaben_offen"] = _aufgaben_offen


def _gateway_name() -> str:
    """Name des Gateways — bei jedem Aufruf frisch gelesen, damit eine Änderung
    ohne Neustart wirkt."""
    return settings_store.get("GATEWAY_NAME") or "EXO Signature Gateway"


class _NotAuthenticated(Exception):
    """Nicht angemeldet. Der Behandler hängt an der Anwendung in `app.py` —
    er braucht `@app.exception_handler` und gehört deshalb dorthin."""

    def __init__(self, is_api: bool = False):
        self.is_api = is_api


class _KennwortZuerst(Exception):
    """Angemeldet mit dem Auslieferungs-Kennwort — erst ändern (v1.10.113).

    Behandler in `app.py`: Seiten leiten zur Einrichtung (Schritt 1), API-
    Aufrufe bekommen 403 mit Hinweis."""

    def __init__(self, is_api: bool = False):
        self.is_api = is_api


# Was mit dem Auslieferungs-Kennwort erreichbar bleibt. Die Einrichtungsseite
# selbst prüft ihre Anmeldung im Rumpf (nicht über `_check_auth`) und ist
# deshalb ohnehin offen; hier steht nur, was über `_check_auth` läuft.
NUR_MIT_VORGABE_KENNWORT = frozenset({"/api/setup/change-password"})


def _vorgabe_kennwort_sperre(request: Request, user: str) -> None:
    """Örtlicher admin mit Vorgabe-Kennwort → nur Kennwort ändern.

    ⚠️ Bis v1.10.112 meldete das Auslieferungs-Kennwort (`admin`) voll an; der
    Hinweis darauf im Assistenten und im Sammelpunkt sperrte nichts. Wer ein
    per Compose aufgesetztes Gateway erreichte, bevor der Betreiber Schritt 1
    abgeschlossen hatte, hatte die Verwaltung.

    Nur für `LOKALER_ADMIN`: Eine Entra-Anmeldung (SSO) kommt nicht über das
    Vorgabe-Kennwort herein und bleibt unberührt.
    """
    if user != LOKALER_ADMIN or request.url.path in NUR_MIT_VORGABE_KENNWORT:
        return
    from webui import hilfen as _hilfen
    if _hilfen._password_change_required():
        path = request.url.path
        raise _KennwortZuerst(is_api=path.startswith("/api/") or path.startswith("/log/"))


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
    # Leeres Passwort nie akzeptieren — auch nicht, falls WEBUI_PASSWORD wider
    # Erwarten leer wäre (der admin-Default in config verhindert das bereits).
    if not password or not config.WEBUI_PASSWORD:
        return False
    return secrets.compare_digest(password.encode(), config.WEBUI_PASSWORD.encode())


# Kennung der örtlichen Verwaltung in Sitzung und Protokoll. Einen
# Benutzernamen fragt die Anmeldung seit v1.10.77 nicht mehr ab: Es gibt genau
# ein örtliches Konto, und das Feld verleitete dazu, den SSH-Benutzer der VM
# einzutragen.
LOKALER_ADMIN = "admin"


def admin_anmeldung(password_in: str, ip: str, art: str) -> tuple[bool, float]:
    """Die EINE Prüfung der örtlichen Admin-Anmeldung: Drossel, Vergleich, Buchung.

    Rückgabe `(ok, warte)`. `warte > 0`: gedrosselt, nicht geprüft — der
    Aufrufer meldet „kurz warten“. `art` erscheint im Anmeldeprotokoll
    (Formular, HTTP-Basic, Einrichtungsseite).

    Bis v1.10.73 stand dieser Ablauf dreimal, je verschieden: Das Formular
    drosselte nach IP und Benutzer, HTTP-Basic nur nach IP, /setup gar nicht —
    und die Einrichtungsseite auf Port 80 fragte überhaupt nicht nach.
    tests/test_admin_anmeldung.py verlangt, dass jede Stelle hier durchgeht.
    """
    import login_drossel
    try:
        import anmelde_ereignisse
    except Exception:                                           # noqa: BLE001
        anmelde_ereignisse = None
    # Zweiter Schlüssel fest: bremst auch verteilte Versuche über viele Adressen.
    schluessel = [ip or "?", f"u:{LOKALER_ADMIN}"]
    warte = max((login_drossel.sperr_sekunden(k) for k in schluessel), default=0.0)
    if warte > 0:
        log.warning("Anmeldung gedrosselt (%s): %s (noch %.0fs)", art, ip, warte)
        if anmelde_ereignisse:
            anmelde_ereignisse.merke("web", ip, "gedrosselt")
        return False, warte
    if _check_password(password_in or ""):
        for k in schluessel:
            login_drossel.erfolg(k)
        return True, 0.0
    for k in schluessel:
        login_drossel.fehlversuch(k)
    if anmelde_ereignisse:
        anmelde_ereignisse.merke("web", ip, f"{art} abgewiesen")
    log.warning("Fehlgeschlagene Anmeldung (%s) von %s", art, ip)
    return False, 0.0


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
    """Rolle aus dem Sitzungsmerkmal — aus DEMSELBEN Weg wie `_get_session_user`
    (Cookie ODER `X-Addin-Session`-Header). Nur wenn gar kein gültiges
    Sitzungsmerkmal vorliegt, gilt der HTTP-Basic-Notzugang als Verwaltung.

    ⚠️ Früher las diese Funktion NUR den Cookie und gab sonst `ROLE_ADMIN`
    zurück. Ein Bearbeiter, der sein (per Add-in an JS zurückgereichtes) Token
    als Header ohne Cookie schickte, wurde von `_get_session_user` angemeldet,
    hier aber mangels Cookie zur Verwaltung — eine Rechteausweitung. Rolle und
    Kennung müssen aus derselben Quelle kommen.
    """
    token = (request.cookies.get(sso_mod.SESSION_COOKIE)
             or request.headers.get("X-Addin-Session"))
    if token:
        payload = sso_mod.verify_session_cookie(token)
        if payload:
            return payload.get("r", sso_mod.ROLE_ADMIN)
    # Kein/ungültiges Sitzungsmerkmal → HTTP-Basic-Notzugang gilt als Verwaltung.
    return sso_mod.ROLE_ADMIN


def _check_auth(request: Request,
                credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """Sitzungskeks → örtliches HTTP-Basic → 401 bzw. Weiterleitung.

    Setzt `request.state.angemeldet`. ⚠️ base.html zeigt Aufgaben und
    Betriebsbänder NUR damit: `user_role` aus der Middleware steht auch ohne
    Anmeldung auf „admin“ (Basic hat keinen Keks), und bis v1.10.87 zeigte die
    öffentliche Anmeldeseite deshalb Befunde, die nur Admins sehen dürfen."""
    user = _get_session_user(request)
    if user:
        # ⚠️ Eine Self-Service-Sitzung (ROLE_SELF) ist KEINE Verwaltungs-/Editor-
        # Sitzung. Ohne diese Sperre käme ein Postfach-Nutzer über die reine
        # `_check_auth`-Wache an alles, was einem Bearbeiter offensteht — u.a. die
        # VORLAGEN (anlegen/ändern/löschen). ROLE_SELF gilt ausschliesslich für die
        # `_require_self`-Endpunkte (/self, /api/self/*); hier → 403 (kein Login-
        # Loop, der Nutzer IST angemeldet, nur nicht berechtigt).
        if _get_session_role(request) == sso_mod.ROLE_SELF:
            raise HTTPException(403, "Nur Self-Service verfügbar (/self).")
        _vorgabe_kennwort_sperre(request, user)
        request.state.angemeldet = True
        return user
    # Örtliche Verwaltung als Notzugang — bleibt immer erreichbar.
    # Der Benutzername der Basic-Anmeldung zählt nicht (LOKALER_ADMIN).
    if credentials and credentials.password:
        ip = request.client.host if request.client else "?"
        ok, warte = admin_anmeldung(credentials.password, ip, "HTTP-Basic")
        if warte > 0:
            raise HTTPException(429, "Zu viele Fehlversuche — bitte kurz warten.")
        if ok:
            _vorgabe_kennwort_sperre(request, LOKALER_ADMIN)
            request.state.angemeldet = True
            return LOKALER_ADMIN
    path = request.url.path
    is_api = path.startswith("/api/") or path.startswith("/log/")
    raise _NotAuthenticated(is_api=is_api)


def _require_admin(request: Request, user: str = Depends(_check_auth)) -> str:
    """Verlangt die Verwaltungsrolle; Bearbeiter bekommen 403."""
    if _get_session_role(request) != sso_mod.ROLE_ADMIN:
        raise HTTPException(403, "Admin-Berechtigung erforderlich")
    return user


def self_service_rolle(upn: str) -> str | None:
    """Rolle `ROLE_SELF`, wenn Self-Service freigeschaltet ist UND der angemeldete
    Nutzer ein im Gateway aktiviertes Postfach ist — sonst None.

    Beim Login für Nicht-Admins aufgerufen: ein gewöhnlicher Postfach-Nutzer soll
    NUR dann eine Sitzung bekommen, wenn der Betreiber Self-Service erlaubt und das
    Postfach überhaupt verwaltet wird. Sonst bleibt es bei der bisherigen Abweisung.
    """
    if not upn or settings_store.get("SELF_SERVICE_ENABLED") is not True:
        return None
    import mailbox_match
    cfg = settings_store.get("MAILBOX_CONFIG") or {}
    treffer = mailbox_match.match_sender(cfg, upn.strip().lower())
    # match_sender liefert {} wenn die Adresse zu keinem Eintrag passt.
    return sso_mod.ROLE_SELF if treffer else None


def _require_self(request: Request) -> str:
    """Self-Service: verlangt eine GÜLTIGE Sitzung (Cookie ODER X-Addin-Session)
    und liefert die E-Mail des angemeldeten Nutzers (klein).

    ⚠️ KEIN HTTP-Basic-Notzugang: der hat keine Postfach-Identität und gälte als
    Verwaltung. Self-Service braucht die EIGENE Identität. Die Self-Endpunkte
    beziehen jede Aktion ausschliesslich auf DIESE Adresse — nie auf einen
    Parameter (sonst könnte Nutzer A das Postfach von Nutzer B verändern).

    Jede gültige Sitzung genügt (Admin wie Self): ein Admin verwaltet darüber sein
    eigenes Postfach. Die Freigabe-Prüfung (SELF_SERVICE_ENABLED) sitzt zusätzlich
    in den schreibenden Endpunkten.
    """
    user = _get_session_user(request)
    if not user:
        raise HTTPException(401, "Anmeldung erforderlich")
    return user.strip().lower()


def _require_kampagnen(request: Request, user: str = Depends(_check_auth)) -> str:
    """Verlangt die Verwaltungs- ODER die Kampagnen-Rolle; sonst 403.

    Marketing soll Banner-Kampagnen ohne Vollzugriff fahren können. Ein
    Signatur-Editor darf das ausdrücklich NICHT — die Rolle ist eng geschnitten.
    Die Grenze wird in `test_wachen.py` (KAMPAGNEN_DARF) und `test_rollen.py`
    (echte Aufrufe) geprüft; wer sie lockert, sieht es dort scheitern.
    """
    if _get_session_role(request) not in (sso_mod.ROLE_ADMIN, sso_mod.ROLE_CAMPAIGN):
        raise HTTPException(403, "Kampagnen-Berechtigung erforderlich")
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










