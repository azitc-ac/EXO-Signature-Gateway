"""Zweiter Faktor für die Notbetrieb-Anmeldung (AD-Kennwort + TOTP).

WARUM ÜBERHAUPT
---------------
Die Anmeldung am Webmail prüft das AD-Kennwort direkt am Domänencontroller —
ohne Entra, damit sie im Ausfall geht. Ohne zweiten Faktor wäre das ein
dauerhaft erreichbarer Weg an MFA und Conditional Access vorbei. AD + zweiter
Faktor ist auch der Zielzustand im Ausfall: Beides geht ohne die Cloud.

EINRICHTEN — nur mit starkem Nachweis
-------------------------------------
Wer nur das AD-Kennwort kennt, darf keinen Faktor einrichten (sonst richtet
ihn sich ein Angreifer mit Kennwort selbst ein). Eingerichtet wird deshalb im
Normalbetrieb nach einer FRISCHEN Entra-Anmeldung (MFA und Conditional Access
des Tenants gelten), siehe Route /notfall/einrichten.

PRÜFEN
------
TOTP nach RFC 6238 (SHA-1, 30 s, 6 Stellen — was jede Authenticator-App
kann), ±1 Zeitschritt, jeder Schritt nur einmal (gegen Wiederverwendung).
Dazu Wiederherstellungscodes, je einmal gültig, gespeichert nur als SHA-256.
Gegen Durchprobieren: Backoff je Postfach und Quelle (login_drossel) und nach
MAX_FEHLER Fehlversuchen in Folge eine harte Sperre, die nur Neu-Einrichten
(Entra) oder der Admin aufhebt — der Backoff allein ließe über ein Jahr
zu viele Versuche zu.

Gespeichert in DATA_DIR/notbetrieb_faktoren.json (600, secure_io) — bewusst
NICHT in der Kopie (schatten.db): Löschen der Kopie darf keinen Faktor löschen.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import struct
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import config
import login_drossel
import secure_io
import settings_store

log = logging.getLogger(__name__)

SCHRITT_S = 30
STELLEN = 6
FENSTER = 1                 # ±1 Zeitschritt (Uhrabweichung des Telefons)
OFFEN_S = 600               # so lange gilt ein begonnenes Einrichten
MAX_FEHLER = 10             # danach harte Sperre
ANZAHL_CODES = 10
AUSSTELLER = "Notbetrieb"

_lock = threading.Lock()


class FaktorFehler(Exception):
    """Meldung, die die Person sehen darf."""


# ── Einstellungen ───────────────────────────────────────────────────────────

def pflicht() -> bool:
    """Zweiter Faktor vorgeschrieben (Vorgabe: ja)."""
    return settings_store.get("NOTBETRIEB_ZWEITFAKTOR") is not False


def im_notfall_ohne() -> bool:
    """Im AUSGERUFENEN Notfall auch ohne eingerichteten Faktor anmelden lassen
    (Vorgabe: nein). Eine bewusste Abwägung des Admins: Verfügbarkeit für
    alle, die ihn nicht eingerichtet haben, gegen den Schutz."""
    return settings_store.get("NOTBETRIEB_ZWEITFAKTOR_NOTFALL_OHNE") is True


def nur_kennwort_erlaubt() -> bool:
    """Darf ein Zugang OHNE zweiten Faktor (IMAP, SMTP 587) anmelden? Nur
    ohne Pflicht oder im ausgerufenen Notfall mit erlaubtem Verzicht."""
    import notbetrieb
    return not pflicht() or (notbetrieb.ausgerufen() and im_notfall_ohne())


# ── Ablage ──────────────────────────────────────────────────────────────────

def _pfad() -> Path:
    return Path(config.DATA_DIR) / "notbetrieb_faktoren.json"


def _laden() -> dict:
    try:
        return json.loads(_pfad().read_text("utf-8"))
    except FileNotFoundError:
        return {}


def _speichern(daten: dict) -> None:
    secure_io.write_secret_json(_pfad(), daten)


def _jetzt_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _schluessel(postfach: str) -> str:
    return (postfach or "").strip().lower()


# ── TOTP (RFC 6238 / RFC 4226) ──────────────────────────────────────────────

def _code(geheimnis: str, schritt: int) -> str:
    key = base64.b32decode(geheimnis + "=" * (-len(geheimnis) % 8))
    h = hmac.new(key, struct.pack(">Q", schritt), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    wert = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** STELLEN)
    return str(wert).zfill(STELLEN)


def _schritt(jetzt: float | None = None) -> int:
    return int((time.time() if jetzt is None else jetzt) // SCHRITT_S)


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _neue_codes() -> list[str]:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"          # ohne 0/o, 1/l/i
    return ["".join(secrets.choice(alphabet) for _ in range(4)) + "-" +
            "".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(ANZAHL_CODES)]


# ── Stand ───────────────────────────────────────────────────────────────────

def stand(postfach: str) -> dict:
    with _lock:
        e = _laden().get(_schluessel(postfach)) or {}
    return {"totp": bool(e.get("totp")), "seit": (e.get("totp") or {}).get("seit", ""),
            "codes_uebrig": len(e.get("codes") or []), "gesperrt": bool(e.get("gesperrt"))}


def eingerichtet(postfach: str) -> bool:
    return stand(postfach)["totp"]


def uebersicht() -> list[dict]:
    """Für die Admin-Seite: wer hat was (ohne Geheimnisse)."""
    with _lock:
        daten = _laden()
    return sorted(({"postfach": k, "seit": (e.get("totp") or {}).get("seit", ""),
                    "codes_uebrig": len(e.get("codes") or []), "gesperrt": bool(e.get("gesperrt"))}
                   for k, e in daten.items() if e.get("totp")), key=lambda x: x["postfach"])


# ── Einrichten (nur nach frischer Entra-Anmeldung, siehe Route) ─────────────

def totp_beginnen(postfach: str) -> dict:
    """Neues Geheimnis, noch nicht aktiv — erst `totp_bestaetigen` mit einem
    gültigen Code schaltet es scharf (sonst könnte ein abgebrochenes Einrichten
    den bisherigen Faktor ersetzen)."""
    geheimnis = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
    k = _schluessel(postfach)
    with _lock:
        daten = _laden()
        e = daten.setdefault(k, {})
        e["offen"] = {"geheimnis": geheimnis, "ts": time.time()}
        _speichern(daten)
    import aussenadresse
    name = aussenadresse.ehlo_hostname() or AUSSTELLER
    uri = (f"otpauth://totp/{quote(AUSSTELLER + ' ' + name)}:{quote(k)}?secret={geheimnis}"
           f"&issuer={quote(AUSSTELLER + ' ' + name)}&algorithm=SHA1&digits={STELLEN}&period={SCHRITT_S}")
    return {"geheimnis": geheimnis, "uri": uri, "qr_svg": _qr_svg(uri)}


def _qr_svg(text: str) -> str:
    import io
    import segno
    puffer = io.BytesIO()
    segno.make(text, error="m").save(puffer, kind="svg", scale=5, border=2, xmldecl=False,
                                     svgns=True, dark="#000", light="#fff")
    return puffer.getvalue().decode()


def totp_bestaetigen(postfach: str, code: str) -> list[str]:
    """Aktiviert das begonnene Geheimnis. Gibt die Wiederherstellungscodes
    zurück — einmalig im Klartext, gespeichert wird nur ihr Hash."""
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    k = _schluessel(postfach)
    with _lock:
        daten = _laden()
        e = daten.get(k) or {}
        offen = e.get("offen") or {}
        if not offen or time.time() - offen.get("ts", 0) > OFFEN_S:
            raise FaktorFehler("Das Einrichten ist abgelaufen — bitte neu beginnen.")
        jetzt = _schritt()
        treffer = [s for s in range(jetzt - FENSTER, jetzt + FENSTER + 1)
                   if hmac.compare_digest(_code(offen["geheimnis"], s), code)]
        if len(code) != STELLEN or not treffer:
            raise FaktorFehler("Der Code stimmt nicht — Uhrzeit des Telefons prüfen und erneut eingeben.")
        codes = _neue_codes()
        e["totp"] = {"geheimnis": offen["geheimnis"], "seit": _jetzt_iso(), "letzter": max(treffer)}
        e["codes"] = [_hash(c) for c in codes]
        e.pop("offen", None)
        e.pop("gesperrt", None)
        e["fehler"] = 0
        daten[k] = e
        _speichern(daten)
    log.info("Notbetrieb-Faktor: TOTP eingerichtet für %s", k)
    return codes


def codes_erneuern(postfach: str) -> list[str]:
    k = _schluessel(postfach)
    with _lock:
        daten = _laden()
        e = daten.get(k) or {}
        if not e.get("totp"):
            raise FaktorFehler("Noch kein zweiter Faktor eingerichtet.")
        codes = _neue_codes()
        e["codes"] = [_hash(c) for c in codes]
        _speichern(daten)
    log.info("Notbetrieb-Faktor: Wiederherstellungscodes erneuert für %s", k)
    return codes


def zuruecksetzen(postfach: str, durch: str) -> bool:
    """Admin: Faktor entfernen (verlorenes Telefon, Sperre). Danach muss die
    Person neu einrichten — mit frischer Entra-Anmeldung."""
    k = _schluessel(postfach)
    with _lock:
        daten = _laden()
        if k not in daten:
            return False
        daten.pop(k)
        _speichern(daten)
    log.warning("Notbetrieb-Faktor: zurückgesetzt für %s durch %s", k, durch)
    return True


# ── Prüfen bei der Anmeldung ────────────────────────────────────────────────

def pruefen(postfach: str, code: str, quelle: str = "") -> tuple[bool, str]:
    """(ok, Meldung). Nimmt einen TOTP-Code oder einen Wiederherstellungscode."""
    k = _schluessel(postfach)
    drossel = [f"notbetrieb-faktor:{k}"] + ([f"notbetrieb-faktor-ip:{quelle}"] if quelle else [])
    for s in drossel:
        if login_drossel.gesperrt(s):
            return False, "Zu viele Fehlversuche — bitte kurz warten."
    roh = str(code or "").strip().lower()
    ziffern = "".join(ch for ch in roh if ch.isdigit())
    with _lock:
        daten = _laden()
        e = daten.get(k) or {}
        if not e.get("totp"):
            return False, "Kein zweiter Faktor eingerichtet."
        if e.get("gesperrt"):
            return False, ("Der zweite Faktor ist nach zu vielen Fehlversuchen gesperrt. Richte ihn neu ein "
                           "oder wende dich an die IT.")
        ok, art = False, ""
        if len(ziffern) == STELLEN and len(roh.replace(" ", "")) == STELLEN:
            jetzt = _schritt()
            for s in range(jetzt - FENSTER, jetzt + FENSTER + 1):
                if s > int(e["totp"].get("letzter", 0)) and hmac.compare_digest(_code(e["totp"]["geheimnis"], s),
                                                                                  ziffern):
                    e["totp"]["letzter"] = s
                    ok, art = True, "totp"
                    break
        elif roh:
            h = _hash(roh.replace(" ", ""))
            if h in (e.get("codes") or []):
                e["codes"].remove(h)
                ok, art = True, "wiederherstellung"
        if ok:
            e["fehler"] = 0
        else:
            e["fehler"] = int(e.get("fehler", 0)) + 1
            if e["fehler"] >= MAX_FEHLER:
                e["gesperrt"] = _jetzt_iso()
        _speichern(daten)
        gesperrt_jetzt = bool(e.get("gesperrt")) and not ok
        uebrig = len(e.get("codes") or [])
    if ok:
        login_drossel.erfolg(drossel[0])
        log.info("Notbetrieb-Faktor: %s bestätigt (%s)%s", k, art,
                 f", noch {uebrig} Wiederherstellungscodes" if art == "wiederherstellung" else "")
        return True, ""
    for s in drossel:
        login_drossel.fehlversuch(s)
    if gesperrt_jetzt:
        log.warning("Notbetrieb-Faktor: %s nach %d Fehlversuchen gesperrt (von %s)", k, MAX_FEHLER, quelle)
        return False, "Zu viele Fehlversuche — der zweite Faktor ist jetzt gesperrt."
    log.info("Notbetrieb-Faktor: falscher Code für %s (von %s)", k, quelle)
    return False, "Der Code stimmt nicht."
