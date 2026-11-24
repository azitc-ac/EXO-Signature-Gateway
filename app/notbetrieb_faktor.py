"""Zweiter Faktor für die Notbetrieb-Anmeldung (AD-Kennwort + Passkey oder TOTP).

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
Oder ein Passkey (WebAuthn, py_webauthn): bequemer — Fingerabdruck, Gesicht,
Windows Hello, Sicherheitsschlüssel — und nicht abphishbar, weil der Browser
die Signatur an den Hostnamen bindet. TOTP bleibt der Ausweichweg für Geräte
ohne Passkey. Dazu Wiederherstellungscodes, je einmal gültig, gespeichert nur
als SHA-256.
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

PK_OFFEN_S = 180            # so lange gilt eine Passkey-Herausforderung
PK_ANMELDEN_S = 60          # so lange wartet der Browser beim Anmelden auf den Passkey (dann Code-Weg)
MAX_PASSKEYS = 10

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


def _aktiv(e: dict) -> bool:
    """Hat dieser Eintrag einen scharfen Faktor (TOTP oder Passkey)? EINE
    Stelle — Stand, Übersicht, Prüfen und Codes fragen alle hier."""
    return bool(e.get("totp") or e.get("passkeys"))


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

def _passkeys_oeffentlich(e: dict) -> list[dict]:
    return [{"id": p["id"], "name": p.get("name", ""), "rp": p.get("rp", ""), "seit": p.get("seit", ""),
             "zuletzt": p.get("zuletzt", "")} for p in e.get("passkeys") or []]


def stand(postfach: str) -> dict:
    with _lock:
        e = _laden().get(_schluessel(postfach)) or {}
    return {"aktiv": _aktiv(e), "totp": bool(e.get("totp")), "seit": (e.get("totp") or {}).get("seit", ""),
            "passkeys": _passkeys_oeffentlich(e),
            "codes_uebrig": len(e.get("codes") or []), "gesperrt": bool(e.get("gesperrt"))}


def zugang(adresse: str) -> dict | None:
    """Kurzstand des Notfall-Zugangs für die Self-Service-Seite (Teams-Reiter,
    Outlook-Add-in): eingerichtet ja/nein. None, wenn der Notbetrieb aus ist
    oder die Adresse kein Postfach der Kopie hat — dann erscheint dort nichts."""
    import notbetrieb
    if not notbetrieb.funktion_aktiv():
        return None
    postfach = notbetrieb.postfach_fuer(adresse)
    if not postfach:
        return None
    st = stand(postfach)
    return {"bereit": st["aktiv"] and not st["gesperrt"], "gesperrt": st["gesperrt"],
            "passkeys": len(st["passkeys"]), "totp": st["totp"]}


def abdeckung() -> dict:
    """Wie viele Benutzerpostfächer könnten sich mit zweitem Faktor anmelden?
    EINE Zählung für Bereitschaft und Notbetrieb-Seite, mit Bezugsgröße —
    „3“ allein wäre nicht zu deuten. Geteilte Postfächer melden sich nicht an."""
    import notbetrieb
    eingerichtet = {f["postfach"] for f in uebersicht() if not f["gesperrt"]}
    if not notbetrieb._db_pfad().exists():
        return {"eingerichtet": 0, "nutzer": 0}
    with notbetrieb._lock, notbetrieb._conn() as conn:
        nutzer = {z["primary_adr"].lower() for z in conn.execute(
            "SELECT primary_adr FROM konten WHERE typ='UserMailbox'")}
    return {"eingerichtet": len(eingerichtet & nutzer), "nutzer": len(nutzer)}


def uebersicht() -> list[dict]:
    """Für die Admin-Seite: wer hat was (ohne Geheimnisse)."""
    with _lock:
        daten = _laden()
    return sorted(({"postfach": k, "totp": bool(e.get("totp")), "passkeys": len(e.get("passkeys") or []),
                    "seit": min([x for x in [(e.get("totp") or {}).get("seit", "")] +
                                 [p.get("seit", "") for p in e.get("passkeys") or []] if x] or [""]),
                    "codes_uebrig": len(e.get("codes") or []), "gesperrt": bool(e.get("gesperrt"))}
                   for k, e in daten.items() if _aktiv(e)), key=lambda x: x["postfach"])


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
        if not _aktiv(e):
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

def _pruefen_mit(postfach: str, quelle: str, pruefer) -> tuple[bool, str]:
    """Der EINE Prüfrahmen für Code und Passkey: Drossel je Postfach und
    Quelle, harte Sperre nach MAX_FEHLER, Protokoll. `pruefer(e)` läuft unter
    der Sperre und gibt (ok, art, meldung_bei_fehler) zurück; er darf `e`
    ändern (verbrauchter Code, Zähler) — gespeichert wird hier."""
    k = _schluessel(postfach)
    drossel = [f"notbetrieb-faktor:{k}"] + ([f"notbetrieb-faktor-ip:{quelle}"] if quelle else [])
    for s in drossel:
        if login_drossel.gesperrt(s):
            return False, "Zu viele Fehlversuche — bitte kurz warten."
    with _lock:
        daten = _laden()
        e = daten.get(k) or {}
        if not _aktiv(e):
            return False, "Kein zweiter Faktor eingerichtet."
        if e.get("gesperrt"):
            return False, ("Der zweite Faktor ist nach zu vielen Fehlversuchen gesperrt. Richte ihn neu ein "
                           "oder wende dich an die IT.")
        ok, art, meldung = pruefer(e)
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
    log.info("Notbetrieb-Faktor: %s für %s abgewiesen (von %s)", art or "Code", k, quelle)
    return False, meldung


def pruefen(postfach: str, code: str, quelle: str = "") -> tuple[bool, str]:
    """(ok, Meldung). Nimmt einen TOTP-Code oder einen Wiederherstellungscode."""
    roh = str(code or "").strip().lower()
    ziffern = "".join(ch for ch in roh if ch.isdigit())

    def pruefer(e: dict):
        if e.get("totp") and len(ziffern) == STELLEN and len(roh.replace(" ", "")) == STELLEN:
            jetzt = _schritt()
            for s in range(jetzt - FENSTER, jetzt + FENSTER + 1):
                if s > int(e["totp"].get("letzter", 0)) and hmac.compare_digest(_code(e["totp"]["geheimnis"], s),
                                                                                  ziffern):
                    e["totp"]["letzter"] = s
                    return True, "totp", ""
        elif roh:
            h = _hash(roh.replace(" ", ""))
            if h in (e.get("codes") or []):
                e["codes"].remove(h)
                return True, "wiederherstellung", ""
        return False, "", "Der Code stimmt nicht."
    return _pruefen_mit(postfach, quelle, pruefer)


# ── Passkeys (WebAuthn) ─────────────────────────────────────────────────────
#
# rp_id ist der Hostname, unter dem die Seite im Browser offen ist — so, wie
# der Browser ihn erzwingt (ein Passkey für sig.firma.at signiert nie für eine
# andere Seite). Er wird JE PASSKEY gespeichert: Wer das Webmail unter einem
# anderen Namen öffnet, bekommt dort keinen Passkey angeboten, sondern den
# Code-Weg — statt eines rätselhaften Browserfehlers. Unter einer IP-Adresse
# gibt es keine Passkeys (WebAuthn verlangt einen Namen).

def passkey_rp(host: str) -> str | None:
    """Hostname taugt als rp_id? Sonst None (IP-Adresse, leer)."""
    import ipaddress
    host = (host or "").strip().lower().rstrip(".")
    if not host or ":" in host:
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        return host


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(t: str) -> bytes:
    return base64.urlsafe_b64decode(t + "=" * (-len(t) % 4))


def _herkunft_pruefen(antwort: dict, rp: str) -> str:
    """Die Herkunft (origin) aus clientDataJSON — der Browser trägt sie ein,
    nicht der Aufrufer. Sie muss https und genau der rp-Host sein (Port egal)."""
    from urllib.parse import urlsplit
    try:
        cd = json.loads(_unb64(str(((antwort or {}).get("response") or {}).get("clientDataJSON") or "")))
        herkunft = str(cd.get("origin") or "")
    except (ValueError, TypeError):
        raise FaktorFehler("Ungültige Passkey-Antwort.")
    teile = urlsplit(herkunft)
    if teile.scheme != "https" or (teile.hostname or "") != rp:
        raise FaktorFehler("Der Passkey kam von einer fremden Seite.")
    return herkunft


def passkey_beginnen(postfach: str, host: str) -> dict:
    """Registrierung (nach frischer Entra-Anmeldung, siehe Route). Gibt die
    Optionen für navigator.credentials.create() als JSON-Objekt zurück."""
    import webauthn
    from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                          ResidentKeyRequirement, UserVerificationRequirement)
    rp = passkey_rp(host)
    if not rp:
        raise FaktorFehler("Passkeys gehen nur unter einem Namen, nicht unter einer IP-Adresse. Öffne die Seite "
                           "über den Namen des Gateways — oder richte die Authenticator-App ein.")
    k = _schluessel(postfach)
    with _lock:
        daten = _laden()
        e = daten.setdefault(k, {})
        if len(e.get("passkeys") or []) >= MAX_PASSKEYS:
            raise FaktorFehler(f"Höchstens {MAX_PASSKEYS} Passkeys — entferne zuerst einen.")
        user_id = _unb64(e["pk_benutzer"]) if e.get("pk_benutzer") else secrets.token_bytes(16)
        e["pk_benutzer"] = _b64(user_id)
        optionen = webauthn.generate_registration_options(
            rp_id=rp, rp_name=f"{AUSSTELLER} {rp}", user_name=k, user_id=user_id, user_display_name=k,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.PREFERRED),
            exclude_credentials=[PublicKeyCredentialDescriptor(id=_unb64(p["id"]))
                                 for p in e.get("passkeys") or [] if p.get("rp") == rp],
            timeout=PK_OFFEN_S * 1000)
        e["pk_offen"] = {"c": _b64(optionen.challenge), "rp": rp, "art": "einrichten", "ts": time.time()}
        _speichern(daten)
    return json.loads(webauthn.options_to_json(optionen))


def passkey_bestaetigen(postfach: str, antwort: dict, name: str = "") -> list[str]:
    """Prüft die Registrierung und speichert den öffentlichen Schlüssel. Gibt
    Wiederherstellungscodes zurück, wenn es noch keine gab (erster Faktor) —
    sonst eine leere Liste (die vorhandenen gelten weiter)."""
    import webauthn
    from webauthn.helpers.exceptions import WebAuthnException
    k = _schluessel(postfach)
    with _lock:
        daten = _laden()
        e = daten.get(k) or {}
        offen = e.pop("pk_offen", None) or {}                 # einmal — auch bei Fehlschlag verbraucht
        _speichern(daten)
    if offen.get("art") != "einrichten" or time.time() - offen.get("ts", 0) > PK_OFFEN_S:
        raise FaktorFehler("Das Einrichten ist abgelaufen — bitte neu beginnen.")
    herkunft = _herkunft_pruefen(antwort, offen["rp"])
    try:
        erg = webauthn.verify_registration_response(credential=antwort, expected_challenge=_unb64(offen["c"]),
                                                    expected_rp_id=offen["rp"], expected_origin=herkunft)
    except (WebAuthnException, ValueError, TypeError, KeyError) as exc:
        log.info("Notbetrieb-Faktor: Passkey-Registrierung für %s abgewiesen: %s", k, exc)
        raise FaktorFehler("Der Passkey ließ sich nicht prüfen — bitte erneut versuchen.")
    neu = {"id": _b64(erg.credential_id), "pk": _b64(erg.credential_public_key), "zaehler": erg.sign_count,
           "rp": offen["rp"], "name": (name or "").strip()[:60] or "Passkey", "seit": _jetzt_iso(), "zuletzt": ""}
    codes: list[str] = []
    with _lock:
        daten = _laden()
        e = daten.setdefault(k, {})
        liste = e.setdefault("passkeys", [])
        if any(p["id"] == neu["id"] for p in liste):
            raise FaktorFehler("Dieser Passkey ist schon eingerichtet.")
        liste.append(neu)
        if not e.get("codes"):
            codes = _neue_codes()
            e["codes"] = [_hash(c) for c in codes]
        e.pop("gesperrt", None)
        e["fehler"] = 0
        _speichern(daten)
    log.info("Notbetrieb-Faktor: Passkey „%s“ eingerichtet für %s (%s)", neu["name"], k, offen["rp"])
    return codes


def passkey_entfernen(postfach: str, pid: str) -> bool:
    k = _schluessel(postfach)
    with _lock:
        daten = _laden()
        e = daten.get(k) or {}
        vorher = len(e.get("passkeys") or [])
        e["passkeys"] = [p for p in e.get("passkeys") or [] if p["id"] != pid]
        if len(e["passkeys"]) == vorher:
            return False
        if not e["passkeys"]:
            e.pop("passkeys")
        _speichern(daten)
    log.info("Notbetrieb-Faktor: Passkey entfernt für %s", k)
    return True


def passkey_anmelden_optionen(postfach: str, host: str) -> dict | None:
    """Optionen für navigator.credentials.get() — nur Passkeys, die unter
    diesem Hostnamen eingerichtet wurden. None: hier keiner (dann Code-Weg)."""
    import webauthn
    from webauthn.helpers.structs import PublicKeyCredentialDescriptor, UserVerificationRequirement
    rp = passkey_rp(host)
    k = _schluessel(postfach)
    with _lock:
        daten = _laden()
        e = daten.get(k) or {}
        passende = [p for p in e.get("passkeys") or [] if rp and p.get("rp") == rp]
        if not passende or e.get("gesperrt"):
            return None
        optionen = webauthn.generate_authentication_options(
            rp_id=rp, allow_credentials=[PublicKeyCredentialDescriptor(id=_unb64(p["id"])) for p in passende],
            user_verification=UserVerificationRequirement.PREFERRED, timeout=PK_ANMELDEN_S * 1000)
        e["pk_offen"] = {"c": _b64(optionen.challenge), "rp": rp, "art": "anmelden", "ts": time.time()}
        _speichern(daten)
    return json.loads(webauthn.options_to_json(optionen))


def passkey_pruefen(postfach: str, antwort: dict, quelle: str = "") -> tuple[bool, str]:
    """Anmeldung mit Passkey — im selben Prüfrahmen wie der Code (Drossel,
    Sperre). Die Herausforderung gilt genau einmal."""
    import webauthn
    from webauthn.helpers.exceptions import WebAuthnException

    def pruefer(e: dict):
        offen = e.pop("pk_offen", None) or {}
        if offen.get("art") != "anmelden" or time.time() - offen.get("ts", 0) > PK_OFFEN_S:
            return False, "passkey", "Die Passkey-Anfrage ist abgelaufen — bitte erneut."
        pid = str((antwort or {}).get("rawId") or (antwort or {}).get("id") or "")
        p = next((x for x in e.get("passkeys") or [] if x["id"] == pid and x.get("rp") == offen["rp"]), None)
        if not p:
            return False, "passkey", "Dieser Passkey ist hier nicht eingerichtet."
        try:
            herkunft = _herkunft_pruefen(antwort, offen["rp"])
            erg = webauthn.verify_authentication_response(
                credential=antwort, expected_challenge=_unb64(offen["c"]), expected_rp_id=offen["rp"],
                expected_origin=herkunft, credential_public_key=_unb64(p["pk"]),
                credential_current_sign_count=int(p.get("zaehler", 0)))
        except (FaktorFehler, WebAuthnException, ValueError, TypeError, KeyError) as exc:
            log.info("Notbetrieb-Faktor: Passkey-Prüfung gescheitert: %s", exc)
            return False, "passkey", "Der Passkey ließ sich nicht prüfen."
        p["zaehler"] = erg.new_sign_count
        p["zuletzt"] = _jetzt_iso()
        return True, "passkey", ""
    return _pruefen_mit(postfach, quelle, pruefer)
