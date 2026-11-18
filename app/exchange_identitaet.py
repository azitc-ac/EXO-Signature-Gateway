"""Exchange-Identitätstoken prüfen — Anmeldung des Verfassen-Add-ins für
Hybrid-Nutzer, deren Outlook-Konto in der LOKALEN Exchange-Organisation liegt.

WARUM
-----
Das Verfassen-Add-in meldet sich per NAA an (MSAL, Entra-Token). Ein Outlook-
Konto auf Exchange lokal (ohne Hybrid Modern Auth) bekommt kein NAA: Microsoft
nennt als NAA-Konten nur Microsoft-Konten und Entra-ID-Identitäten (MS Learn,
„Nested app auth requirement sets“, Abschnitt „Supported accounts and hosts“). MSAL
findet dann keine NAA-Brücke, fällt auf den Browser-Weg zurück und scheitert im
klassischen Outlook an der JavaScript-only-Laufzeit der Ereignisse (keine Web
Crypto API) mit `crypto_nonexistent` (gemessen 18.11.2026, Classic Outlook,
Konto auf Exchange SE).

Der Weg dafür ist das Exchange-Identitätstoken (`getUserIdentityTokenAsync`,
Mailbox 1.1). MS Learn, „Inside the Exchange identity token“: „Legacy Exchange
Online user identity tokens … are no longer supported and turned off across all
Microsoft 365 tenants … Exchange user identity tokens are still supported for
Exchange on-premises.“ Im Ereignis ist der Aufruf nicht gesperrt (MS Learn,
„Activate add-ins with events“, Abschnitt „Unsupported APIs“, nennt ihn nicht).

WAS GEPRÜFT WIRD (MS Learn, „Validate an Exchange identity token“)
------------------------------------------------------------------
Kopf: `typ` = JWT, `alg` = RS256, `x5t` vorhanden.
Nutzlast: `appctx.amurl` zeigt auf ein zugelassenes Metadaten-Dokument,
jetzt zwischen `nbf` und `exp` (mit Spielraum), `aud` = Adresse des Add-ins,
`appctx.version` = ExIdTok.V1. Dazu „Verify the domain“: Die Domäne von
`amurl` muss zum Benutzer passen — „If your add-in service has a preexisting
configuration with the user's tenant, then you can establish if this amurl is
trusted.“ Die vorhandene Konfiguration ist hier die EWS-Adresse der lokalen
Exchange (`EWS_ONPREM_URL`): Nur ein `amurl` auf genau diesem Host wird
angenommen, und geladen wird das Dokument über dieselbe TLS-Sitzung wie EWS
(eigene CA, „Verbinden mit“) — nie eine Adresse, die das Token frei nennt.
Signatur: Schlüssel aus `keys[]` des Metadaten-Dokuments mit passendem
`keyinfo.x5t`; zusätzlich muss der SHA-1-Fingerabdruck des Zertifikats selbst
diesem x5t entsprechen (gemessen 18.11.2026 an Exchange SE: x5t =
base64url(SHA-1(DER))). Zusätzlich zur Vorgabe: `iss` beginnt mit dem
Exchange-Dienstprinzipal `00000002-0000-0ff1-ce00-000000000000@`.

IDENTITÄT
---------
Das Token nennt KEINE Adresse: `appctx` hat nur `msexchuid`, `version`,
`amurl` (MS Learn, Tabelle „appctx“). `msexchuid` hat die Form
`<GUID>@<Server>`. Annahme (Fremdquelle, nicht von Microsoft dokumentiert):
die GUID ist die msExchMailboxGuid = ExchangeGuid des Postfachs. Die
Zuordnung ist deshalb streng: Die GUID muss der ExchangeGuid eines
Hybrid-Eintrags gehören (Zwischenspeicher `exo_mailboxes`, Typ MailUser, oder
MAILBOX_CONFIG-Schlüssel mit `hybrid: True`), und `exo_mailboxes.ist_hybrid`
muss die Adresse bestätigen. Ein EXO-Postfach wird NIE über diesen Weg
angemeldet — weder über die GUID noch über die Adresse. Trifft die Annahme
nicht zu, schlägt die Zuordnung fehl (nichts wird eingefügt) und das
Protokoll nennt die GUID aus dem Token.

Netz: blockierend (requests) — Aufrufer im Event-Loop nutzen asyncio.to_thread.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
import threading
import time
from urllib.parse import urlsplit, urlunsplit

import settings_store

log = logging.getLogger("exchange_identitaet")

KOPF = "X-Exchange-Identitaet"            # HTTP-Kopf, in dem das Add-in das Token schickt
EXCHANGE_PRINZIPAL = "00000002-0000-0ff1-ce00-000000000000"
METADATEN_PFAD = "/autodiscover/metadata/json/1"
TOKEN_VERSION = "ExIdTok.V1"
SPIELRAUM_S = 300                         # Uhrenabweichung zwischen Exchange und Gateway
TOKEN_MAX = 8192                          # Zeichen; echte Tokens haben gut 1 000
METADATEN_MAX = 256 * 1024                # Bytes
METADATEN_GILT_S = 3600                   # Zwischenspeicher je Dokument
NEU_LADEN_PAUSE_S = 300                   # unbekanntes x5t: frühestens dann neu laden
ZEITLIMIT_S = 10

_GUID_RE = re.compile(r"^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})@[^@\s]{1,253}$")
_lock = threading.Lock()
_metadaten: dict[str, tuple[float, dict]] = {}


class Abgelehnt(Exception):
    """Token nicht angenommen. Der Text geht ins Protokoll, nie an den Aufrufer."""


# ── Zerlegen ──────────────────────────────────────────────────────────────────

def _b64url(teil: str) -> bytes:
    if not teil or not re.fullmatch(r"[A-Za-z0-9_-]+", teil):
        raise Abgelehnt("Token-Teil ist kein Base64url")
    try:
        return base64.urlsafe_b64decode(teil + "=" * (-len(teil) % 4))
    except (binascii.Error, ValueError):
        raise Abgelehnt("Token-Teil ist kein Base64url") from None


def zerlegen(token: str) -> tuple[dict, dict, bytes, bytes]:
    """(Kopf, Nutzlast, signierte Bytes, Signatur) — RFC 7519, drei Teile."""
    token = (token or "").strip()
    if not token or len(token) > TOKEN_MAX:
        raise Abgelehnt("Token fehlt oder ist zu lang")
    teile = token.split(".")
    if len(teile) != 3:
        raise Abgelehnt("Token hat nicht drei Teile")
    try:
        kopf = json.loads(_b64url(teile[0]))
        nutzlast = json.loads(_b64url(teile[1]))
    except (ValueError, UnicodeDecodeError):
        raise Abgelehnt("Kopf oder Nutzlast ist kein JSON") from None
    if not isinstance(kopf, dict) or not isinstance(nutzlast, dict):
        raise Abgelehnt("Kopf oder Nutzlast ist kein JSON-Objekt")
    return kopf, nutzlast, f"{teile[0]}.{teile[1]}".encode("ascii"), _b64url(teile[2])


def _appctx(nutzlast: dict) -> dict:
    """`appctx` ist in echten Tokens ein JSON-TEXT im Anspruch (das .NET-Beispiel
    bei Microsoft deserialisiert `appctx.Value`), in der Dokumentation als
    Objekt gezeigt. Beides wird angenommen."""
    roh = nutzlast.get("appctx")
    if isinstance(roh, str):
        try:
            roh = json.loads(roh)
        except ValueError:
            raise Abgelehnt("appctx ist kein JSON") from None
    if not isinstance(roh, dict):
        raise Abgelehnt("appctx fehlt")
    return roh


def _sekunden(wert, name: str) -> int:
    """`nbf`/`exp` kommen als Text („1331579055“) — Zahl oder Ziffernfolge."""
    if isinstance(wert, bool):
        raise Abgelehnt(f"{name} ist keine Zeitangabe")
    if isinstance(wert, int):
        return wert
    if isinstance(wert, str) and wert.strip().isdigit():
        return int(wert.strip())
    raise Abgelehnt(f"{name} fehlt oder ist keine Zeitangabe")


# ── Inhalt ────────────────────────────────────────────────────────────────────

def ews_basis() -> tuple[str, str, int | None]:
    """(Schema, Host, Port) der konfigurierten EWS-Adresse — der Vertrauensanker.
    Ohne EWS-Adresse gibt es keinen zugelassenen Aussteller."""
    import ews_onprem
    url = (settings_store.get(ews_onprem.KEY_URL) or "").strip()
    if not url or ews_onprem.url_pruefen(url):
        raise Abgelehnt("keine EWS-Adresse der lokalen Exchange eingetragen — "
                        "kein zugelassener Aussteller")
    t = urlsplit(url)
    return t.scheme, (t.hostname or "").lower(), t.port


def _port_gleich(a: int | None, b: int | None) -> bool:
    return (a or 443) == (b or 443)


def amurl_pruefen(amurl, basis: tuple[str, str, int | None]) -> str:
    """Adresse, von der das Metadaten-Dokument geladen wird — gebaut aus der
    EWS-Adresse, nicht aus dem Token. Das Token muss genau dorthin zeigen."""
    if not isinstance(amurl, str) or not amurl or len(amurl) > 512:
        raise Abgelehnt("amurl fehlt")
    _schema, host, port = basis
    try:
        t = urlsplit(amurl)
        t_port = t.port
    except ValueError:
        raise Abgelehnt("amurl ist keine Adresse") from None
    if (t.scheme != "https" or (t.hostname or "").lower() != host or t.username or t.password
            or not _port_gleich(t_port, port) or t.path.lower() != METADATEN_PFAD
            or t.query or t.fragment):
        raise Abgelehnt(f"amurl {t.scheme}://{t.hostname}{t.path} passt nicht zur "
                        f"EWS-Adresse ({host}) — nur deren Metadaten-Dokument ist zugelassen")
    return urlunsplit(("https", host + (f":{port}" if port else ""), METADATEN_PFAD, "", ""))


def inhalt_pruefen(kopf: dict, nutzlast: dict, aud_erwartet: str,
                   basis: tuple[str, str, int | None], jetzt: float) -> tuple[dict, str]:
    """Alle Prüfungen ohne Netz. (appctx, Adresse des Metadaten-Dokuments)."""
    if kopf.get("typ") != "JWT":
        raise Abgelehnt("typ ist nicht JWT")
    if kopf.get("alg") != "RS256":
        raise Abgelehnt(f"alg {kopf.get('alg')!r} statt RS256")
    if not isinstance(kopf.get("x5t"), str) or not kopf["x5t"]:
        raise Abgelehnt("x5t fehlt")
    if not aud_erwartet or nutzlast.get("aud") != aud_erwartet:
        raise Abgelehnt(f"aud {str(nutzlast.get('aud'))[:120]!r} ist nicht die Adresse "
                        f"dieses Add-ins ({aud_erwartet})")
    iss = nutzlast.get("iss")
    if not isinstance(iss, str) or not iss.lower().startswith(EXCHANGE_PRINZIPAL + "@"):
        raise Abgelehnt("iss ist nicht der Exchange-Dienst")
    nbf, exp = _sekunden(nutzlast.get("nbf"), "nbf"), _sekunden(nutzlast.get("exp"), "exp")
    if jetzt + SPIELRAUM_S < nbf:
        raise Abgelehnt("Token gilt noch nicht (nbf)")
    if jetzt - SPIELRAUM_S >= exp:
        raise Abgelehnt("Token abgelaufen (exp)")
    ctx = _appctx(nutzlast)
    if ctx.get("version") != TOKEN_VERSION:
        raise Abgelehnt(f"appctx.version {str(ctx.get('version'))[:40]!r} statt {TOKEN_VERSION}")
    return ctx, amurl_pruefen(ctx.get("amurl"), basis)


# ── Signatur ──────────────────────────────────────────────────────────────────

def _x5t(der: bytes) -> str:
    """x5t ist per Definition (RFC 7515 §4.1.7) der SHA-1-Fingerabdruck des
    Zertifikats — ein Bezeichner zum Auffinden, keine Sicherheitsfunktion. Die
    Sicherheit trägt die RS256-Signaturprüfung mit dem so gefundenen Schlüssel."""
    return base64.urlsafe_b64encode(
        hashlib.sha1(der, usedforsecurity=False).digest()).rstrip(b"=").decode("ascii")


def schluessel_waehlen(metadaten: dict, x5t: str):
    """Öffentlicher Schlüssel zu `x5t` aus `keys[]` — oder None (dann ggf. neu laden)."""
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import rsa
    for k in (metadaten or {}).get("keys") or []:
        if not isinstance(k, dict) or k.get("usage") != "signing":
            continue
        info, wert = k.get("keyinfo") or {}, k.get("keyvalue") or {}
        if not isinstance(info, dict) or not isinstance(wert, dict) or info.get("x5t") != x5t:
            continue
        if wert.get("type") != "x509Certificate" or not isinstance(wert.get("value"), str):
            continue
        try:
            der = base64.b64decode(wert["value"], validate=True)
            zert = x509.load_der_x509_certificate(der)
        except (binascii.Error, ValueError):
            continue
        # Der Fingerabdruck des Zertifikats selbst — nicht nur die Beschriftung im Dokument.
        if _x5t(der) != x5t:
            continue
        pub = zert.public_key()
        if not isinstance(pub, rsa.RSAPublicKey) or pub.key_size < 2048:
            continue
        return pub
    return None


def signatur_pruefen(pub, signiert: bytes, signatur: bytes) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    try:
        pub.verify(signatur, signiert, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        raise Abgelehnt("Signatur ungültig") from None


def metadaten_laden(url: str) -> dict:
    """Metadaten-Dokument über die EWS-TLS-Sitzung (eigene CA, „Verbinden mit“),
    ohne Anmeldung (das Dokument ist öffentlich), ohne Umleitungen."""
    import requests
    import ews_onprem
    ip = (settings_store.get(ews_onprem.KEY_VERBINDEN) or "").strip()
    if ip and ews_onprem.verbinden_pruefen(ip):
        ip = ""
    sitzung, ziel = ews_onprem._tls_sitzung(url, ip, (settings_store.get(ews_onprem.KEY_CA) or "").strip())
    try:
        r = sitzung.get(ziel, timeout=ZEITLIMIT_S, allow_redirects=False, stream=True)
        roh = r.raw.read(METADATEN_MAX + 1, decode_content=True)
    except requests.exceptions.RequestException as exc:
        raise Abgelehnt(f"Metadaten-Dokument nicht abrufbar ({str(exc)[:160]})") from None
    finally:
        sitzung.close()
    if r.status_code != 200:
        raise Abgelehnt(f"Metadaten-Dokument: HTTP {r.status_code}")
    if len(roh) > METADATEN_MAX:
        raise Abgelehnt("Metadaten-Dokument zu groß")
    try:
        daten = json.loads(roh)
    except ValueError:
        raise Abgelehnt("Metadaten-Dokument ist kein JSON") from None
    if not isinstance(daten, dict) or not isinstance(daten.get("keys"), list):
        raise Abgelehnt("Metadaten-Dokument ohne keys")
    return daten


def _schluessel(url: str, x5t: str, laden, jetzt: float):
    """Aus dem Zwischenspeicher; bei unbekanntem x5t (Zertifikatswechsel)
    höchstens alle NEU_LADEN_PAUSE_S neu laden."""
    with _lock:
        eintrag = _metadaten.get(url)
    if eintrag and jetzt - eintrag[0] < METADATEN_GILT_S:
        pub = schluessel_waehlen(eintrag[1], x5t)
        if pub is not None or jetzt - eintrag[0] < NEU_LADEN_PAUSE_S:
            return pub
    daten = laden(url)
    with _lock:
        _metadaten[url] = (jetzt, daten)
    return schluessel_waehlen(daten, x5t)


# ── Identität ─────────────────────────────────────────────────────────────────

def form_beschreiben(wert) -> str:
    """Form eines Werts für das Protokoll, OHNE seinen Inhalt preiszugeben:
    Buchstaben-/Ziffernfolgen werden zu ihrer Länge, Trennzeichen bleiben
    („8-4-4-4-12@a6.a7.a3“). Anlass (18.11.2026): Die erste Live-Probe lehnte
    msexchuid ab, ohne dass erkennbar war, welche Form Exchange SE liefert."""
    if wert is None:
        return "fehlt"
    if not isinstance(wert, str):
        return f"Typ {type(wert).__name__}"
    return re.sub(r"[0-9A-Za-z]+", lambda m: str(len(m.group(0))), wert)[:80] or "leer"


def postfach_zuordnen(msexchuid, appctx_schluessel=None) -> str:
    """Hauptadresse des Hybrid-Nutzers zur GUID aus `msexchuid` — oder Abgelehnt."""
    import exo_mailboxes
    import mailbox_match
    m = _GUID_RE.match(msexchuid if isinstance(msexchuid, str) else "")
    if not m:
        raise Abgelehnt("msexchuid fehlt oder hat nicht die Form <GUID>@<Server> "
                        f"(Form: {form_beschreiben(msexchuid)}; appctx-Schlüssel: "
                        f"{', '.join(sorted(appctx_schluessel or [])) or '-'})")
    guid = m.group(1).lower()
    if not exo_mailboxes.hybrid_aktiv():
        raise Abgelehnt("Hybrid-Postfächer sind nicht eingeschaltet (Rückweg smtp)")
    adresse = ""
    eintrag = exo_mailboxes.eintrag_per_guid(guid)
    if eintrag is not None:
        if eintrag.get("type") != exo_mailboxes.HYBRID_TYP:
            raise Abgelehnt(f"GUID {guid} gehört einem Exchange-Online-Postfach — "
                            "dieser Weg gilt nur für Hybrid-Nutzer")
        adresse = eintrag.get("primary") or ""
    if not adresse:
        cfg_all = settings_store.get("MAILBOX_CONFIG") or {}
        for schluessel, cfg in cfg_all.items():
            if (isinstance(cfg, dict) and str(schluessel).lower() == guid
                    and cfg.get("hybrid") is True):
                adresse = (cfg.get("primary") or "").strip().lower()
                break
    if not adresse:
        raise Abgelehnt(f"GUID {guid} gehört keinem Hybrid-Nutzer des Gateways")
    if not exo_mailboxes.ist_hybrid(adresse):
        raise Abgelehnt(f"{adresse} ist kein Hybrid-Nutzer (mehr)")
    if not mailbox_match.match_sender(settings_store.get("MAILBOX_CONFIG") or {}, adresse):
        raise Abgelehnt(f"{adresse} wird vom Gateway nicht betreut")
    return adresse


def pruefen(token: str, aud_erwartet: str, jetzt: float | None = None, laden=None) -> str:
    """Adresse des Hybrid-Nutzers, dem das Token gehört — oder Abgelehnt.
    Reihenfolge: erst alles ohne Netz, dann Signatur, dann Zuordnung."""
    jetzt = time.time() if jetzt is None else jetzt
    kopf, nutzlast, signiert, signatur = zerlegen(token)
    ctx, url = inhalt_pruefen(kopf, nutzlast, aud_erwartet, ews_basis(), jetzt)
    pub = _schluessel(url, kopf["x5t"], laden or metadaten_laden, jetzt)
    if pub is None:
        raise Abgelehnt(f"kein Signaturschlüssel mit x5t {kopf['x5t'][:40]} im Metadaten-Dokument")
    signatur_pruefen(pub, signiert, signatur)
    return postfach_zuordnen(ctx.get("msexchuid"), list(ctx))


def zwischenspeicher_leeren() -> None:
    with _lock:
        _metadaten.clear()
