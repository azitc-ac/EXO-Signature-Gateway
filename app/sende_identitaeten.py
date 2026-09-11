"""Sende-Identitäten — benannte Absender mit eigenem SMTP-AUTH-Login.

Fundament der „Plattform für verwaltete Sende-Identitäten": Eine Anwendung oder
ein Gerät (SAP, ein Kopierer, ein Kundendienst-Postfach) meldet sich am
Submission-Listener (587) mit einem **Login** an, das hier verwaltet wird —
unabhängig von der IP. Das ist der niedrige Auth-Balken (Basic über TLS), den
ältere Geräte brauchen und den EXO für die Direkteinlieferung nicht mehr bietet.

Ein Login gehört zu **genau einer** Identität (bewusste Entscheidung 11.09.);
mehrere Geräte dürfen sich dasselbe Login teilen. Statistik und Protokoll ordnen
authentifizierte Post deshalb der Identität zu, nicht dem einzelnen Gerät.

Speicher: `data/sende_identitaeten.json`, 600 (enthält Passwort-Hashes). Es
werden NUR Hashes gespeichert (pbkdf2-sha256), nie das Klartext-Passwort — auch
ein Leser der Datei bekommt keine Anmeldedaten.

Dieses Modul ist gateway-eigen und wird von Kern (handler, main) UND Oberfläche
benutzt; es importiert deshalb KEIN webui (Ringschluss). Das Passwort-Hashing
spiegelt bewusst `webui/deps.py` (gleiche Parameter), statt es von dort zu
importieren.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

import config
import secure_io

log = logging.getLogger(__name__)

PFAD = Path(config.DATA_DIR) / "sende_identitaeten.json"
_lock = threading.RLock()
_cache: list[dict] | None = None

# pbkdf2-Parameter identisch zu webui/deps.py — wird dort geändert, hier
# nachziehen (bewusst dupliziert, um den Ringschluss Kern↔webui zu vermeiden).
_ITER = 260_000


# ── Passwort-Hashing ──────────────────────────────────────────────────────────

def _hash_passwort(passwort: str) -> str:
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", passwort.encode(), salt.encode(), _ITER)
    return f"pbkdf2:sha256:{salt}:{key.hex()}"


def _pruefe_passwort(passwort: str, gespeichert: str) -> bool:
    try:
        _, alg, salt, key_hex = gespeichert.split(":", 3)
        assert alg == "sha256"
    except Exception:
        return False
    key = hashlib.pbkdf2_hmac("sha256", passwort.encode(), salt.encode(), _ITER)
    return hmac.compare_digest(key.hex(), key_hex)


# ── Speicher ──────────────────────────────────────────────────────────────────

def _laden() -> list[dict]:
    global _cache
    if _cache is not None:
        return _cache
    if not PFAD.exists():
        _cache = []
        return _cache
    try:
        import json
        obj = json.loads(PFAD.read_text("utf-8"))
        _cache = list(obj.get("identitaeten", []))
    except Exception as exc:                                  # noqa: BLE001
        log.warning("sende_identitaeten: laden fehlgeschlagen: %s", exc)
        _cache = []
    return _cache


def _speichern() -> None:
    secure_io.write_secret_json(PFAD, {"identitaeten": _cache or []})


def _norm_login(login: str | bytes) -> str:
    if isinstance(login, bytes):
        login = login.decode("utf-8", "replace")
    return login.strip().lower()


# ── öffentliche Sicht (ohne Hash) ─────────────────────────────────────────────

def _oeffentlich(rec: dict) -> dict:
    return {
        "id": rec["id"],
        "name": rec.get("name", ""),
        "login": rec.get("login", ""),
        "absender": rec.get("absender", ""),
        "aktiv": bool(rec.get("aktiv", True)),
        "erstellt": rec.get("erstellt", ""),
    }


def liste() -> list[dict]:
    """Alle Identitäten ohne Passwort-Hash, neueste zuerst."""
    with _lock:
        return [_oeffentlich(r) for r in reversed(_laden())]


# ── CRUD ──────────────────────────────────────────────────────────────────────

def anlegen(name: str, login: str, passwort: str, absender: str = "") -> dict:
    """Legt eine Identität an. Wirft ValueError bei leerem/doppeltem Login."""
    name = (name or "").strip()
    login_n = _norm_login(login)
    passwort = passwort or ""
    absender = (absender or "").strip().lower()
    if not login_n:
        raise ValueError("Login darf nicht leer sein.")
    if len(passwort) < 8:
        raise ValueError("Passwort zu kurz (mindestens 8 Zeichen).")
    with _lock:
        daten = _laden()
        if any(_norm_login(r.get("login", "")) == login_n for r in daten):
            raise ValueError(f"Login „{login_n}“ ist bereits vergeben.")
        rec = {
            "id": secrets.token_hex(6),
            "name": name or login_n,
            "login": login_n,
            "passwort_hash": _hash_passwort(passwort),
            "absender": absender,
            "aktiv": True,
            "erstellt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        daten.append(rec)
        _speichern()
        return _oeffentlich(rec)


def aktualisieren(id_: str, *, name: str | None = None, absender: str | None = None,
                  aktiv: bool | None = None, passwort: str | None = None) -> bool:
    """Ändert Felder einer Identität. Leeres Passwort = unverändert."""
    with _lock:
        for rec in _laden():
            if rec["id"] != id_:
                continue
            if name is not None:
                rec["name"] = name.strip() or rec.get("name", "")
            if absender is not None:
                rec["absender"] = absender.strip().lower()
            if aktiv is not None:
                rec["aktiv"] = bool(aktiv)
            if passwort:
                if len(passwort) < 8:
                    raise ValueError("Passwort zu kurz (mindestens 8 Zeichen).")
                rec["passwort_hash"] = _hash_passwort(passwort)
            _speichern()
            return True
    return False


def entfernen(id_: str) -> bool:
    with _lock:
        daten = _laden()
        neu = [r for r in daten if r["id"] != id_]
        if len(neu) == len(daten):
            return False
        _cache_setzen(neu)
        _speichern()
        return True


def _cache_setzen(neu: list[dict]) -> None:
    global _cache
    _cache = neu


# ── Anmeldung + Grenzen ───────────────────────────────────────────────────────

def pruefe_login(login: str | bytes, passwort: str | bytes) -> dict | None:
    """Prüft Login+Passwort. Gibt die (öffentliche) Identität zurück oder None.

    Nur aktive Identitäten. Konstante Laufzeit über den Passwortvergleich hinweg
    ist durch pbkdf2 gegeben; ein unbekanntes Login prüft gegen einen Dummy-Hash,
    damit die Antwortzeit „Login existiert nicht“ nicht von „Passwort falsch“
    unterscheidbar ist.
    """
    login_n = _norm_login(login)
    if isinstance(passwort, bytes):
        passwort = passwort.decode("utf-8", "replace")
    with _lock:
        rec = next((r for r in _laden()
                    if _norm_login(r.get("login", "")) == login_n
                    and r.get("aktiv", True)), None)
    if rec is None:
        # Zeit-Angleich: gegen einen konstanten Dummy-Hash rechnen.
        _pruefe_passwort(passwort or "", _DUMMY_HASH)
        return None
    if _pruefe_passwort(passwort or "", rec.get("passwort_hash", "")):
        return _oeffentlich(rec)
    return None


# Ein fester, gültig geformter Hash für den Zeit-Angleich (Passwort: nie geraten).
_DUMMY_HASH = _hash_passwort(secrets.token_hex(16))


def pruefe(ident: dict, sender: str, recipients: list[str]) -> tuple[bool, str, str]:
    """Darf diese authentifizierte Identität diese Nachricht einliefern?

    Grenze in Etappe 1: Ist der Identität eine feste Absenderadresse zugeordnet
    (`absender`, i.d.R. das Shared Mailbox), muss der Envelope-Absender genau
    diese sein — sonst könnte ein Login als beliebiger Absender einliefern.
    Ohne zugeordneten Absender wird angenommen (weitere Grenzen folgen in der
    Zustell-Etappe). Rückgabe: (erlaubt, grund, smtp_antwort).
    """
    pin = (ident.get("absender") or "").strip().lower()
    if pin and (sender or "").strip().lower() != pin:
        grund = (f"Identität „{ident.get('name') or ident.get('login')}“ darf nur "
                 f"als {pin} senden, nicht als {sender or '(leer)'}")
        return False, grund, "553 5.7.1 Absender nicht für diese Anmeldung zugelassen"
    return True, "", ""
