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

# Anmelde-Bremse am 587: dieselbe Drossel wie der Web-Login (login_drossel,
# exponentielles Backoff je Schlüssel). NICHT neu implementieren — die
# Fehlversuche werden je Quell-IP gezählt, mit dem Präfix "587:" gegen die
# HTTP-Schlüssel abgegrenzt.
_DROSSEL_PREFIX = "587:"


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
        "extern": bool(rec.get("extern", False)),
        "aktiv": bool(rec.get("aktiv", True)),
        "erstellt": rec.get("erstellt", ""),
    }


def liste() -> list[dict]:
    """Alle Identitäten ohne Passwort-Hash, neueste zuerst."""
    with _lock:
        return [_oeffentlich(r) for r in reversed(_laden())]


# ── CRUD ──────────────────────────────────────────────────────────────────────

def anlegen(name: str, login: str, passwort: str, absender: str = "",
            extern: bool = False) -> dict:
    """Legt eine Identität an. Wirft ValueError bei leerem/doppeltem Login.

    `extern=False` (Vorgabe) heisst „darf nur an interne Empfänger senden" —
    spiegelt das Geräte-Flag des Port-25-Relays.
    """
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
            "extern": bool(extern),
            "aktiv": True,
            "erstellt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        daten.append(rec)
        _speichern()
        return _oeffentlich(rec)


def aktualisieren(id_: str, *, name: str | None = None, absender: str | None = None,
                  aktiv: bool | None = None, passwort: str | None = None,
                  extern: bool | None = None) -> bool:
    """Ändert Felder einer Identität. Leeres Passwort = unverändert.

    Ein Feld, das `None` ist, bleibt unverändert (so lässt sich z.B. nur `extern`
    umschalten, ohne die übrigen Werte mitzusenden).
    """
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
            if extern is not None:
                rec["extern"] = bool(extern)
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

def pruefe_login(login: str | bytes, passwort: str | bytes,
                 quelle: str = "") -> dict | None:
    """Prüft Login+Passwort. Gibt die (öffentliche) Identität zurück oder None.

    Nur aktive Identitäten. Konstante Laufzeit über den Passwortvergleich hinweg
    ist durch pbkdf2 gegeben; ein unbekanntes Login prüft gegen einen Dummy-Hash,
    damit die Antwortzeit „Login existiert nicht“ nicht von „Passwort falsch“
    unterscheidbar ist.

    `quelle` ist die Quell-IP der Anmeldung. Ist sie gedrosselt (zu viele
    Fehlversuche in kurzer Zeit, `login_drossel`), wird abgewiesen, OHNE das
    Passwort noch zu prüfen. Ein erfolgreicher Login löscht die Zählung der IP.
    """
    import login_drossel
    login_n = _norm_login(login)
    if isinstance(passwort, bytes):
        passwort = passwort.decode("utf-8", "replace")
    quelle = (quelle or "").strip()
    schluessel = f"{_DROSSEL_PREFIX}{quelle}" if quelle else ""

    if schluessel and login_drossel.gesperrt(schluessel):
        log.warning("Submission-Auth: %s gedrosselt (%.0fs) — Anmeldung abgewiesen",
                    quelle, login_drossel.sperr_sekunden(schluessel))
        return None

    with _lock:
        rec = next((r for r in _laden()
                    if _norm_login(r.get("login", "")) == login_n
                    and r.get("aktiv", True)), None)

    if rec is not None and _pruefe_passwort(passwort or "", rec.get("passwort_hash", "")):
        if schluessel:
            login_drossel.erfolg(schluessel)      # Erfolg löscht die Zählung der IP
        return _oeffentlich(rec)
    # Fehlschlag: unbekanntes/inaktives Login ODER falsches Passwort.
    if rec is None:
        # Zeit-Angleich: gegen einen konstanten Dummy-Hash rechnen, damit die
        # Antwortzeit „Login existiert nicht“ nicht unterscheidbar ist.
        _pruefe_passwort(passwort or "", _DUMMY_HASH)
    if schluessel:
        login_drossel.fehlversuch(schluessel)
    return None


# Ein fester, gültig geformter Hash für den Zeit-Angleich (Passwort: nie geraten).
_DUMMY_HASH = _hash_passwort(secrets.token_hex(16))


def pruefe(ident: dict, sender: str, recipients: list[str]) -> tuple[bool, str, str]:
    """Darf diese authentifizierte Identität diese Nachricht einliefern?

    Drei Grenzen, in dieser Reihenfolge — Rückgabe: (erlaubt, grund, smtp_antwort):

    1. **Absender-Pin** — ist der Identität eine feste Absenderadresse zugeordnet
       (`absender`, i.d.R. das Shared Mailbox), muss der Envelope-Absender genau
       diese sein; sonst könnte ein Login als beliebiger Absender einliefern.
    2. **Absenderdomäne im Tenant** — wie beim Port-25-Relay (`smtp_relay.pruefe`)
       darf eine Identität nur AS einer tenant-eigenen Adresse einliefern; sonst
       wäre das Gateway ein offenes Relay für beliebige Absenderdomänen. Geprüft
       gegen `smtp_relay._eigene_domaenen()` (bekannte Postfachadressen +
       `TENANT_DOMAIN`).
    3. **Ziel intern/extern** — Vorgabe ist „nur intern": jeder Empfänger muss ein
       bekanntes Postfach sein. Nur wenn `extern` gesetzt ist, sind externe
       Empfänger erlaubt. Spiegelt das `extern`-Flag der Relay-Geräte.

    Kann die Tenant-Domänen-/Adressliste (noch) nicht bestimmt werden, wird
    vorsichtshalber mit 451 (temporär) abgewiesen statt geraten — dieselbe
    Ausfallrichtung wie das Relay (im Zweifel nicht zustellen).

    ── Zustellweg (bewusste Entscheidung, nicht implizit) ──
    Eine angenommene Nachricht durchläuft danach den normalen Handler und geht
    über `reinject.send()` zurück an Exchange — genau wie Post vom Connector. Sie
    wird also signiert/S-MIME-behandelt, WENN ihr Absender in `MAILBOX_CONFIG`
    aktiviert ist; ein sonstiger Geräte-Absender läuft unverändert durch. Im Modus
    `smtp` trägt der Smarthost jede tenant-eigene Absenderadresse; in den
    Graph-Modi kann Graph nur „als“ ein echtes Postfach senden (deshalb Grenze 2).
    """
    import smtp_relay

    sender_n = (sender or "").strip().lower()
    name = ident.get("name") or ident.get("login") or "?"

    # 1. Absender-Pin
    pin = (ident.get("absender") or "").strip().lower()
    if pin and sender_n != pin:
        grund = (f"Identität „{name}“ darf nur als {pin} senden, "
                 f"nicht als {sender or '(leer)'}")
        return False, grund, "553 5.7.1 Absender nicht für diese Anmeldung zugelassen"

    # 2. Absenderdomäne muss dem Tenant gehören (Schutz vor offenem Relay)
    domaenen = smtp_relay._eigene_domaenen()
    if not domaenen:
        return (False,
                f"Identität „{name}“: Tenant-Domänen (noch) nicht bekannt — "
                "Absender lässt sich nicht prüfen",
                "451 4.3.2 Einlieferung temporär nicht möglich")
    dom = sender_n.rsplit("@", 1)[-1] if "@" in sender_n else ""
    if dom not in domaenen:
        return (False,
                f"Identität „{name}“ darf nicht als {sender or '(leer)'} senden — "
                f"Absenderdomäne {dom or '(leer)'} gehört nicht zu diesem Tenant",
                "550 5.7.1 Absenderdomäne für diese Anmeldung nicht zulässig")

    # 3. Ziel: nur intern (Vorgabe) oder auch extern
    if not ident.get("extern"):
        import exo_mailboxes
        adressen = exo_mailboxes.known_addresses()
        if not adressen:
            return (False,
                    f"Identität „{name}“: Postfachliste (noch) nicht bekannt — "
                    "interne Ziele lassen sich nicht prüfen",
                    "451 4.3.2 Einlieferung temporär nicht möglich")
        fremd = [e for e in recipients if (e or "").strip().lower() not in adressen]
        if fremd:
            return (False,
                    f"Identität „{name}“ darf nur an interne Empfänger senden — "
                    "ausserhalb des Tenants: " + ", ".join(fremd[:3]),
                    "550 5.7.1 Diese Anmeldung darf nur an interne Empfänger senden")

    return True, "", ""
