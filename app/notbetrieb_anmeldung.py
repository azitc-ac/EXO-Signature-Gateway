"""Anmeldung im Notbetrieb gegen das lokale Active Directory (LDAP-Bind).

WOZU
----
Fällt Microsoft 365 aus, ist womöglich auch Entra weg — eine Anmeldung über
Entra-SSO trägt dann nicht. Wer das Gateway on-prem betreibt, hat in aller
Regel ein AD, aus dem Entra synchronisiert wird. Die Nutzer melden sich im
Notbetrieb deshalb mit ihrem gewohnten Kennwort an: Das Gateway prüft es per
LDAP-Bind direkt am Domänencontroller. Es braucht kein Dienstkonto und speichert
kein Kennwort.

ABLAUF
------
1. Bind mit dem eingegebenen Namen (UPN oder DOMÄNE\\name) und Kennwort —
   NUR verschlüsselt: `ldaps://` oder `ldap://` mit StartTLS vor dem Bind.
   Ein Kennwort im Klartext über das Netz gibt es nicht.
2. Mit derselben Verbindung das eigene Konto lesen (mail, proxyAddresses,
   userPrincipalName) und daraus das Postfach bestimmen — über das
   Rechte-Abbild (`notbetrieb_rechte.konten`), das auch die UPN kennt.
3. `notbetrieb_rechte.zugriff()` liefert, was die Person sehen darf.

⚠️ LEERES KENNWORT
------------------
AD beantwortet einen Simple-Bind mit Namen und LEEREM Kennwort als
„unauthenticated bind“ mit ERFOLG (RFC 4513 §5.1.2). Ohne eigene Prüfung wäre
jeder Name ohne Kennwort angemeldet. Deshalb wird ein leeres Kennwort vor jedem
Netzzugriff abgewiesen — und ldap3 zusätzlich angewiesen, so etwas nie zu
senden. Geprüft in tests/test_notbetrieb.py.
"""
from __future__ import annotations

import logging
import ssl
from urllib.parse import urlparse

import login_drossel
import settings_store

log = logging.getLogger(__name__)

MAX_LAENGE = 256
ZEITLIMIT_S = 8


class AnmeldeFehler(Exception):
    """Fehler mit einer Meldung, die der Nutzer sehen darf (keine Interna)."""


def einstellungen() -> dict:
    return {"server": (settings_store.get("NOTBETRIEB_LDAP_SERVER") or "").strip(),
            "basis": (settings_store.get("NOTBETRIEB_LDAP_BASIS") or "").strip(),
            "ca": (settings_store.get("NOTBETRIEB_LDAP_CA") or "").strip()}


def eingerichtet() -> bool:
    e = einstellungen()
    return bool(e["server"] and e["basis"])


def _server_teile(url: str) -> tuple[str, int, bool]:
    """(host, port, ldaps). Nur ldaps:// oder ldap:// (dann StartTLS)."""
    u = urlparse(url if "://" in url else "ldaps://" + url)
    if u.scheme not in ("ldaps", "ldap") or not u.hostname:
        raise AnmeldeFehler("LDAP-Server ungültig — erwartet ldaps://host oder ldap://host.")
    ldaps = u.scheme == "ldaps"
    return u.hostname, u.port or (636 if ldaps else 389), ldaps


def _pruefe_eingabe(benutzer: str, passwort: str) -> str:
    benutzer = (benutzer or "").strip()
    if not benutzer or not passwort:
        # Siehe Modul-Docstring: leeres Kennwort wäre ein „erfolgreicher“ Bind.
        raise AnmeldeFehler("Name und Kennwort angeben.")
    if len(benutzer) > MAX_LAENGE or len(passwort) > MAX_LAENGE:
        raise AnmeldeFehler("Eingabe zu lang.")
    if any(ord(ch) < 32 for ch in benutzer + passwort):
        raise AnmeldeFehler("Ungültige Zeichen.")
    return benutzer


def _postfach_aus(eintrag: dict) -> list[str]:
    """Kandidaten für das Postfach aus dem AD-Konto, in Rangfolge: mail,
    primäre und weitere SMTP-Adressen, UPN."""
    def werte(a):
        v = eintrag.get(a)
        return [str(x) for x in (v if isinstance(v, list) else [v]) if x]
    out: list[str] = []
    out += [m.lower() for m in werte("mail")]
    proxy = werte("proxyAddresses")
    out += [p[5:].lower() for p in proxy if p.startswith("SMTP:")]
    out += [p[5:].lower() for p in proxy if p.startswith("smtp:")]
    out += [u.lower() for u in werte("userPrincipalName")]
    return list(dict.fromkeys(a for a in out if "@" in a))


def _ldap_pruefen(benutzer: str, passwort: str, e: dict) -> dict:
    """Bind + eigenes Konto lesen. Gibt die Attribute zurück oder wirft."""
    import ldap3
    from ldap3.core.exceptions import LDAPException
    from ldap3.utils.conv import escape_filter_chars

    host, port, ldaps = _server_teile(e["server"])
    tls = ldap3.Tls(validate=ssl.CERT_REQUIRED, version=ssl.PROTOCOL_TLS_CLIENT,
                    ca_certs_data=e["ca"] or None)
    server = ldap3.Server(host, port=port, use_ssl=ldaps, tls=tls, connect_timeout=ZEITLIMIT_S,
                          get_info=ldap3.NONE)
    conn = ldap3.Connection(server, user=benutzer, password=passwort, authentication=ldap3.SIMPLE,
                            receive_timeout=ZEITLIMIT_S, raise_exceptions=False, read_only=True,
                            auto_bind=ldap3.AUTO_BIND_NONE)
    try:
        if not conn.open():
            raise AnmeldeFehler("Domänencontroller nicht erreichbar.")
        if not ldaps and not conn.start_tls():
            # Ohne Verschlüsselung geht kein Kennwort über die Leitung.
            raise AnmeldeFehler("Verschlüsselte Verbindung zum Domänencontroller fehlgeschlagen.")
        if not conn.bind():
            raise AnmeldeFehler("Name oder Kennwort falsch.")
        if "\\" in benutzer:
            filt = f"(sAMAccountName={escape_filter_chars(benutzer.split(chr(92), 1)[1])})"
        else:
            filt = f"(userPrincipalName={escape_filter_chars(benutzer)})"
        conn.search(e["basis"], f"(&(objectClass=user){filt})",
                    attributes=["mail", "proxyAddresses", "userPrincipalName"], size_limit=2)
        if len(conn.entries) != 1:
            raise AnmeldeFehler("Konto im Verzeichnis nicht eindeutig gefunden.")
        return conn.entries[0].entry_attributes_as_dict
    except LDAPException as exc:
        log.warning("Notbetrieb-Anmeldung: LDAP-Fehler: %s", type(exc).__name__)
        raise AnmeldeFehler("Domänencontroller nicht erreichbar.") from None
    finally:
        try:
            conn.unbind()
        except Exception:                                   # noqa: BLE001
            pass


def anmelden(benutzer: str, passwort: str, quelle: str = "") -> dict:
    """{ok, benutzer, postfach, zugriff} oder {ok: False, error}. Kennwörter
    werden weder gespeichert noch protokolliert."""
    import notbetrieb_rechte
    try:
        benutzer = _pruefe_eingabe(benutzer, passwort)
    except AnmeldeFehler as exc:
        return {"ok": False, "error": str(exc)}
    schluessel = f"notbetrieb:{benutzer.lower()}"
    # Je Name UND je Quelladresse: sonst probiert ein Angreifer ein Kennwort an
    # vielen Namen durch, ohne je einen einzelnen Zähler zu füllen.
    drossel = [schluessel] + ([f"notbetrieb-ip:{quelle}"] if quelle else [])
    for s in drossel:
        if login_drossel.gesperrt(s):
            return {"ok": False, "error": "Zu viele Fehlversuche — bitte kurz warten.",
                    "warten_s": int(login_drossel.sperr_sekunden(s))}
    e = einstellungen()
    if not eingerichtet():
        return {"ok": False, "error": "Die Notbetrieb-Anmeldung ist nicht eingerichtet."}
    try:
        konto = _ldap_pruefen(benutzer, passwort, e)
    except AnmeldeFehler as exc:
        for s in drossel:
            login_drossel.fehlversuch(s)
        log.info("Notbetrieb-Anmeldung abgewiesen: %s (%s)", benutzer, exc)
        return {"ok": False, "error": str(exc)}
    login_drossel.erfolg(schluessel)
    for kandidat in _postfach_aus(konto):
        zugriff = notbetrieb_rechte.zugriff(kandidat)
        if zugriff:
            log.info("Notbetrieb-Anmeldung: %s → %s (%s Postfächer)", benutzer, zugriff[0]["postfach"],
                     len(zugriff))
            return {"ok": True, "benutzer": benutzer, "postfach": zugriff[0]["postfach"], "zugriff": zugriff}
    log.info("Notbetrieb-Anmeldung: %s angemeldet, aber kein Postfach in Exchange Online", benutzer)
    return {"ok": False, "angemeldet": True,
            "error": "Angemeldet — zu diesem Konto gibt es aber kein Postfach in Exchange Online "
                     "(liegt es noch im lokalen Exchange?)."}
