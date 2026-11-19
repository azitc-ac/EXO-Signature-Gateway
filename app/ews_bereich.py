"""Zugriffsbereich des EWS-Dienstkontos: eine AD-Gruppe statt Handpflege (seit v1.12.92).

WOFÜR
-----
Das Gateway bedient Hybrid-Nutzer (Postfach in der lokalen Exchange) per EWS
mit einem Dienstkonto und ApplicationImpersonation (ews_onprem). Welche
Postfächer das Konto übernehmen darf, legt ein Management Scope fest. Bis
v1.12.89 pflegte der Admin ihn von Hand; ein neuer Hybrid-Nutzer ausserhalb des
Scopes scheiterte mit ErrorImpersonateUserDenied (Aufgabe `ews-bereich`).

Jetzt: Der Scope filtert auf EINE universelle AD-Sicherheitsgruppe
(`MemberOfGroup -eq '<DN>'`), und das Gateway pflegt deren Mitglieder selbst per
LDAPS. Soll = Hybrid-Nutzer mit Signatur ODER S/MIME (`soll_mitglieder`).

WER DARF WAS
------------
* Admin-Anmeldung: EINMAL für die Einrichtung, nie gespeichert (nicht in den
  Einstellungen, nicht im Protokoll, nicht in Fehlertexten — `_schwaerzen`).
* Dienstkonto: bekommt per AD-Delegation NUR „WriteProperty member“ auf GENAU
  diese Gruppe (ACE in nTSecurityDescriptor, `ace_member_schreiben`). Keine
  Exchange-Verwaltungsrechte. Damit pflegt das Gateway die Mitglieder.
  ⚠️ Das heisst auch: Wer das Gateway übernimmt, kann jedes Postfach in die
  Gruppe legen und dann per EWS übernehmen. Der Scope begrenzt also, was das
  Gateway NICHT selbst ändert — er ist kein Schutz gegen ein kompromittiertes
  Gateway. Darum wird jede Änderung protokolliert (GATEWAY_AUDIT_LOG, `_audit`).

EXCHANGE-TEIL: NUR ALS BEFEHLE
------------------------------
Scope und Rollenzuweisung brauchen die Exchange-Verwaltungsshell der LOKALEN
Organisation. Sie läuft über WinRM (WSMan, `http://<server>/PowerShell/`).
PowerShell unter Linux kann WSMan nicht (Microsoft: „WSMan remoting isn't
supported on non-Windows platforms“, OMI-Client braucht OpenSSL 1.0), und die
Exchange-Endpunkte gibt es nicht über SSH. Deshalb erzeugt das Gateway diese
Befehle nur (`plan`, „Befehle kopieren“) und PRÜFT danach (`pruefen`: EWS auf
ein Mitglied). Ausgeführt wird nur der AD-Teil per LDAPS.

EINE QUELLE
-----------
`plan()` liefert die Schritte samt Befehlstext. Vorschau, Kopieren und
Ausführen (`einrichten`) lesen dieselbe Liste; ausgeführt werden genau die
Schritte mit `art == "ad"`, in derselben Reihenfolge, mit denselben Werten.
tests/test_ews_bereich.py vergleicht beides.

BESTAND
-------
Hat das Dienstkonto schon eine ApplicationImpersonation-Zuweisung (z. B. Scope
als feste Adressliste), zeigt `bestand_erkennen` das an. Der Umbau übernimmt die
alten Adressen in die Gruppe; die alte Zuweisung samt Scope steht erst in den
Befehlen, wenn der Admin das bestätigt (`alt_entfernen`).

KONTOWECHSEL
------------
Legt die Einrichtung ein NEUES Dienstkonto an, hat es bis zu den
Exchange-Befehlen (Scope, Rollenzuweisung) keine ApplicationImpersonation.
Würde es sofort EWS_ONPREM_KONTO/_KENNWORT ersetzen, fiele EWS für ALLE
Hybrid-Nutzer aus — auch für die, die mit dem bisherigen Konto liefen. Darum
steht es zunächst als AUSSTEHEND in EWS_BEREICH_NEU_KONTO/_KENNWORT; `pruefen`
testet MIT IHM (LDAP-Delegation, EWS-Probe) und übernimmt es erst, wenn alles
bestanden ist. `verwerfen` löscht es wieder. Alle drei schreiben über EINEN
Weg (`_zugang_schreiben`). Nur ohne bisheriges Konto (Ersteinrichtung) gilt es
sofort — da gibt es nichts, was ausfallen könnte.

AUSBREITUNG
-----------
Exchange übernimmt Gruppen- und Scope-Änderungen für Impersonation verzögert
(AD-Replikation, RBAC-Zwischenspeicher). Ein frisch aufgenommenes Mitglied gilt
`WARTEZEIT_S` lang als „wartet“ (`wartet`), und health_check wertet eine
Ablehnung in dieser Zeit nicht als Fehler (Aufgabe `ews-bereich` bleibt still).
"""
from __future__ import annotations

import logging
import re
import secrets
import string
import struct
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import ps_text
import settings_store

log = logging.getLogger("ews_bereich")

KEY_AUTO = "EWS_BEREICH_AUTO"         # Schalter: Mitglieder automatisch pflegen
KEY_LDAP = "EWS_LDAP_SERVER"          # Domänencontroller (LDAPS), host[:port]
KEY_BASIS = "EWS_LDAP_BASIS"          # Basis-DN der Suche; leer = aus dem RootDSE
KEY_OU = "EWS_BEREICH_OU"             # Ablage für Konto und Gruppe; leer = CN=Users,<Basis>
KEY_GRUPPE = "EWS_BEREICH_GRUPPE"     # sAMAccountName der Gruppe
KEY_NEU_KONTO = "EWS_BEREICH_NEU_KONTO"        # ausstehendes Dienstkonto (noch nicht wirksam)
KEY_NEU_KENNWORT = "EWS_BEREICH_NEU_KENNWORT"  # dessen Kennwort (SECRET_KEYS)
STAND_KEY = "_EWS_BEREICH_STAND"      # Laufzeitzustand (INTERNAL_KEYS)

GRUPPE_VORGABE = "EXO-Gateway-EWS"
KONTO_VORGABE = "svc-exo-gateway"
LDAPS_PORT = 636
ZEITLIMIT_S = 15
# Annahme (nicht gemessen): AD-Replikation plus RBAC-Zwischenspeicher der
# Exchange liegen meist unter einer Stunde. Zwei Stunden Spielraum, danach
# zählt eine Ablehnung wieder als Fehler.
WARTEZEIT_S = 2 * 3600
SUCHE_JE_ANFRAGE = 40                 # Adressen je ODER-Filter

# Attribut `member` — System-Id-Guid laut AD-Schema (learn.microsoft.com/
# windows/win32/adschema/a-member).
MEMBER_GUID = uuid.UUID("bf9679c0-0de6-11d0-a285-00aa003049e2")
ADS_RIGHT_DS_WRITE_PROP = 0x20
ACCESS_ALLOWED_ACE_TYPE = 0x00
ACCESS_ALLOWED_OBJECT_ACE_TYPE = 0x05
ACE_OBJECT_TYPE_PRESENT = 0x1
ACE_INHERITED_OBJECT_TYPE_PRESENT = 0x2
INHERITED_ACE = 0x10
DACL_SECURITY_INFORMATION = 0x4
ACL_REVISION_DS = 4
SE_DACL_PRESENT = 0x0004
SE_SELF_RELATIVE = 0x8000

UAC_NORMAL_ACCOUNT = 0x0200
UAC_DONT_EXPIRE_PASSWORD = 0x10000
GRUPPE_UNIVERSAL_SICHERHEIT = -2147483640   # 0x80000008: Sicherheit | universell

_lauf = threading.Lock()
_audit_lock = threading.Lock()


class BereichFehler(Exception):
    """Fehler mit Code und Text für die Oberfläche. Der Text ist geschwärzt."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ── Konfiguration und Stand ───────────────────────────────────────────────────

def _wert(key: str) -> str:
    v = settings_store.get(key)
    return v.strip() if isinstance(v, str) else ""


def auto_an() -> bool:
    return settings_store.get(KEY_AUTO) is True


def gruppe_name() -> str:
    return _wert(KEY_GRUPPE) or GRUPPE_VORGABE


def scope_name(gruppe: str | None = None) -> str:
    return f"{gruppe or gruppe_name()} Scope"


def zuweisung_name(gruppe: str | None = None) -> str:
    return f"{gruppe or gruppe_name()} Impersonation"


def stand() -> dict:
    s = settings_store.get(STAND_KEY)
    return dict(s) if isinstance(s, dict) else {}


def _stand_setzen(**felder) -> dict:
    with settings_store._lock:
        s = stand()
        s.update(felder)
        settings_store.force_update({STAND_KEY: s})
    return s


def eingerichtet() -> bool:
    """Kennt das Gateway die Gruppe (DN)? Erst dann gibt es etwas abzugleichen."""
    return bool(stand().get("gruppe_dn"))


def _jetzt() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime | None = None) -> str:
    return (t or _jetzt()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _aus_iso(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _audit(aktion: str, ziel: str, detail: str) -> None:
    """Jede Änderung am AD/an der Einrichtung: Audit-Eintrag UND Protokollzeile.
    Dieselbe Liste wie die übrigen Selbstheilungen (Betrieb → Audit)."""
    import health_check
    with _audit_lock:
        health_check._append_audit(aktion, ziel, detail)
    log.info("EWS-Bereich: %s %s — %s", aktion, ziel, detail)


def letzte_aenderungen(anzahl: int = 8) -> list[dict]:
    """Die jüngsten Einträge dieses Moduls aus dem Audit (für die Karte)."""
    alle = settings_store.get("GATEWAY_AUDIT_LOG") or []
    eigene = [e for e in alle if isinstance(e, dict)
              and str(e.get("action", "")).startswith("ews_bereich_")]
    return list(reversed(eigene[-anzahl:]))


# ── Soll ──────────────────────────────────────────────────────────────────────

def soll_mitglieder(mailbox_cfg: dict | None = None) -> set[str]:
    """Primäradressen der Hybrid-Nutzer mit Signatur ODER S/MIME.

    MAILBOX_CONFIG enthält nur aktive Einträge (Speicherweg verwirft „beides
    aus“); geprüft wird trotzdem ausdrücklich, damit ein Eintrag mit sig=False
    und smime=False nie hineinkommt. Hybrid = Kennzeichen `hybrid` am Eintrag
    oder `exo_mailboxes.ist_hybrid` — dieselbe Weiche wie ews_onprem.zustaendig."""
    import exo_mailboxes
    cfg = settings_store.get("MAILBOX_CONFIG") if mailbox_cfg is None else mailbox_cfg
    aus: set[str] = set()
    for key, e in (cfg or {}).items():
        if not isinstance(e, dict):
            continue
        if not (e.get("sig") or e.get("smime") or e.get("smime_sign") or e.get("smime_encrypt")):
            continue
        adresse = (key if "@" in key else (e.get("primary") or "")).strip().lower()
        if not adresse:
            continue
        if e.get("hybrid") or exo_mailboxes.ist_hybrid(adresse):
            aus.add(adresse)
    return aus


# ── Schwärzen ─────────────────────────────────────────────────────────────────

def _schwaerzen(text, *geheim) -> str:
    """Ein Kennwort darf in keinem Fehlertext stehen — auch nicht, wenn die
    Bibliothek es in ihre Meldung übernimmt."""
    t = str(text or "")
    for g in geheim:
        if g and len(g) >= 1:
            t = t.replace(g, "••••")
    return t[:300]


# ── Sicherheitsbeschreibung (nTSecurityDescriptor) ───────────────────────────
# Selbstrelatives Format nach MS-DTYP 2.4.6. Nur die DACL wird gelesen und
# geschrieben (LDAP_SERVER_SD_FLAGS_OID mit DACL_SECURITY_INFORMATION) — Besitzer
# und SACL bleiben unberührt.

def _sid_laenge(daten: bytes, ab: int) -> int:
    return 8 + 4 * daten[ab + 1]


@dataclass
class Ace:
    typ: int
    flags: int
    roh: bytes            # komplette ACE samt Kopf

    @property
    def geerbt(self) -> bool:
        return bool(self.flags & INHERITED_ACE)

    def zerlegt(self) -> dict | None:
        """{maske, objekttyp, sid} für (Objekt-)Erlaubnis-ACEs, sonst None."""
        r = self.roh
        if self.typ == ACCESS_ALLOWED_ACE_TYPE:
            return {"maske": struct.unpack_from("<I", r, 4)[0], "objekttyp": None,
                    "sid": r[8:8 + _sid_laenge(r, 8)]}
        if self.typ == ACCESS_ALLOWED_OBJECT_ACE_TYPE:
            maske, oflags = struct.unpack_from("<II", r, 4)
            pos = 12
            objekttyp = None
            if oflags & ACE_OBJECT_TYPE_PRESENT:
                objekttyp = uuid.UUID(bytes_le=bytes(r[pos:pos + 16]))
                pos += 16
            if oflags & ACE_INHERITED_OBJECT_TYPE_PRESENT:
                pos += 16
            return {"maske": maske, "objekttyp": objekttyp, "sid": r[pos:pos + _sid_laenge(r, pos)]}
        return None


@dataclass
class Sd:
    control: int
    besitzer: bytes = b""
    gruppe: bytes = b""
    sacl: bytes = b""
    dacl_rev: int = ACL_REVISION_DS
    aces: list[Ace] = field(default_factory=list)


def sd_lesen(daten: bytes) -> Sd:
    if len(daten) < 20 or daten[0] != 1:
        raise BereichFehler("sd", "Sicherheitsbeschreibung nicht lesbar")
    _rev, _sbz, control, o_own, o_grp, o_sacl, o_dacl = struct.unpack_from("<BBHIIII", daten, 0)
    sd = Sd(control=control)
    if o_own:
        sd.besitzer = daten[o_own:o_own + _sid_laenge(daten, o_own)]
    if o_grp:
        sd.gruppe = daten[o_grp:o_grp + _sid_laenge(daten, o_grp)]
    if o_sacl:
        groesse = struct.unpack_from("<H", daten, o_sacl + 2)[0]
        sd.sacl = daten[o_sacl:o_sacl + groesse]
    if o_dacl:
        sd.dacl_rev, _s, _groesse, anzahl, _s2 = struct.unpack_from("<BBHHH", daten, o_dacl)
        pos = o_dacl + 8
        for _ in range(anzahl):
            typ, flags, groesse = struct.unpack_from("<BBH", daten, pos)
            if groesse < 4 or pos + groesse > len(daten):
                raise BereichFehler("sd", "Sicherheitsbeschreibung beschädigt (ACE)")
            sd.aces.append(Ace(typ, flags, bytes(daten[pos:pos + groesse])))
            pos += groesse
    return sd


def sd_schreiben(sd: Sd) -> bytes:
    """Selbstrelativ neu aufbauen: Kopf, SACL, DACL, Besitzer, Gruppe."""
    aces = b"".join(a.roh for a in sd.aces)
    dacl = struct.pack("<BBHHH", sd.dacl_rev, 0, 8 + len(aces), len(sd.aces), 0) + aces
    teile = []
    pos = 20
    o_sacl = o_dacl = o_own = o_grp = 0
    if sd.sacl:
        o_sacl, pos = pos, pos + len(sd.sacl)
        teile.append(sd.sacl)
    o_dacl, pos = pos, pos + len(dacl)
    teile.append(dacl)
    if sd.besitzer:
        o_own, pos = pos, pos + len(sd.besitzer)
        teile.append(sd.besitzer)
    if sd.gruppe:
        o_grp, pos = pos, pos + len(sd.gruppe)
        teile.append(sd.gruppe)
    control = sd.control | SE_DACL_PRESENT | SE_SELF_RELATIVE
    return struct.pack("<BBHIIII", 1, 0, control, o_own, o_grp, o_sacl, o_dacl) + b"".join(teile)


def ace_member_schreiben(sid: bytes) -> Ace:
    """Objekt-ACE: Erlaubt WriteProperty auf `member`, nicht vererbbar."""
    rumpf = struct.pack("<II", ADS_RIGHT_DS_WRITE_PROP, ACE_OBJECT_TYPE_PRESENT) \
        + MEMBER_GUID.bytes_le + bytes(sid)
    return Ace(ACCESS_ALLOWED_OBJECT_ACE_TYPE, 0,
               struct.pack("<BBH", ACCESS_ALLOWED_OBJECT_ACE_TYPE, 0, 4 + len(rumpf)) + rumpf)


def darf_member_schreiben(sd: Sd, sid: bytes) -> bool:
    """Hat `sid` WriteProperty auf member (gezielt oder auf alle Eigenschaften)?
    Verweigernde ACEs werden nicht ausgewertet — gemeldet wird, was die
    Einrichtung setzt."""
    for a in sd.aces:
        z = a.zerlegt()
        if not z or bytes(z["sid"]) != bytes(sid):
            continue
        if z["maske"] & ADS_RIGHT_DS_WRITE_PROP and z["objekttyp"] in (None, MEMBER_GUID):
            return True
    return False


def delegation_ergaenzen(sd: Sd, sid: bytes) -> bool:
    """ACE einfügen, falls er fehlt — nach den ausdrücklichen, vor den geerbten
    ACEs (kanonische Reihenfolge). True = geändert."""
    if darf_member_schreiben(sd, sid):
        return False
    pos = next((i for i, a in enumerate(sd.aces) if a.geerbt), len(sd.aces))
    sd.aces.insert(pos, ace_member_schreiben(sid))
    sd.dacl_rev = max(sd.dacl_rev, ACL_REVISION_DS)
    return True


# ── Verzeichnis (LDAPS) ───────────────────────────────────────────────────────

def ldap_ziel() -> tuple[str, int]:
    """(Host, Port) aus EWS_LDAP_SERVER. BereichFehler, wenn leer oder ungültig."""
    roh = _wert(KEY_LDAP)
    if not roh:
        raise BereichFehler("konfiguration", "Domänencontroller (LDAPS) nicht eingetragen")
    return ldap_server_pruefen(roh)


def ldap_server_pruefen(roh: str) -> tuple[str, int]:
    roh = (roh or "").strip()
    m = re.fullmatch(r"([A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?)(?::(\d{1,5}))?", roh)
    if not m:
        raise BereichFehler("konfiguration", "Domänencontroller: Hostname wie dc01.firma.at, "
                                             "optional mit :Port (Vorgabe 636)")
    port = int(m.group(2) or LDAPS_PORT)
    if not 0 < port < 65536:
        raise BereichFehler("konfiguration", "Ungültiger Port")
    return m.group(1), port


_DN_TEIL = re.compile(r"^(?:CN|OU|DC|O|L|ST|C)=[^,=+<>#;\"\\']+(?:,(?:CN|OU|DC|O|L|ST|C)=[^,=+<>#;\"\\']+)*$",
                      re.I)


def dn_pruefen(dn: str, was: str) -> str:
    """Fehlertext oder "". Bewusst streng: keine Sonderzeichen, die in
    LDAP-DN, OPATH-Filter oder PowerShell eigene Bedeutung hätten (Apostroph,
    Anführungszeichen, Escape). Solche Namen bleiben dem Weg von Hand."""
    dn = (dn or "").strip()
    if dn and not _DN_TEIL.match(dn):
        return f"{was}: erwartet wird ein DN wie OU=Dienste,DC=firma,DC=at (ohne Sonderzeichen)"
    return ""


def name_pruefen(name: str, was: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,19}", (name or "").strip()):
        return f"{was}: 1–20 Zeichen, nur Buchstaben, Ziffern, Leerzeichen, . _ -"
    return ""


class _TlsKontext:
    """ldap3 baut sonst einen eigenen Kontext. Hier derselbe wie für EWS
    (ews_onprem._tls_kontext: certifi + eigene CA aus EWS_ONPREM_CA), mit
    Hostnamenprüfung. Eine Quelle für die Vertrauensanker."""

    def __init__(self, host: str):
        self.host = host
        self.sni = host
        self.validate = 2       # ssl.CERT_REQUIRED (für ldap3-interne Abfragen)
        self.valid_names = None

    def wrap_socket(self, connection, do_handshake=False):
        import ews_onprem
        ctx = ews_onprem._tls_kontext(_wert(ews_onprem.KEY_CA))
        ctx.check_hostname = True
        connection.socket = ctx.wrap_socket(connection.socket, server_side=False,
                                            do_handshake_on_connect=True,
                                            server_hostname=self.host)

    def start_tls(self, connection):            # nie benutzt: nur LDAPS
        raise BereichFehler("tls", "StartTLS wird nicht verwendet")


def _namen_aufloesen(host: str, port: int) -> None:
    """Vorab-DNS-Prüfung: ldap3 meldet einen nicht auflösbaren Namen nur als
    „invalid server address“, ohne DNS zu nennen."""
    import ipaddress
    import socket
    try:
        ipaddress.ip_address(host.strip("[]"))
        return
    except ValueError:
        pass
    try:
        socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise BereichFehler(
            "dns", f"Name „{host}“ ist im Gateway nicht auflösbar (DNS). Trage die IP-Adresse "
            "des Domänencontrollers ein (sie muss im Zertifikat des Domänencontrollers stehen) "
            "oder sorge dafür, dass der DNS-Server des Gateways den Namen kennt.") from None


class Verzeichnis:
    """Schlanke Hülle um ldap3. Tests ersetzen sie über `_verbinden`."""

    def __init__(self, benutzer: str, kennwort: str):
        import ldap3
        from ldap3.core.exceptions import LDAPException
        host, port = ldap_ziel()
        _namen_aufloesen(host, port)
        self._geheim = kennwort
        self._ldap3 = ldap3
        server = ldap3.Server(host, port=port, use_ssl=True, get_info=ldap3.DSA,
                              connect_timeout=ZEITLIMIT_S)
        server.tls = _TlsKontext(host)
        try:
            self.c = ldap3.Connection(server, user=benutzer, password=kennwort,
                                      authentication=ldap3.SIMPLE, auto_bind=False,
                                      receive_timeout=ZEITLIMIT_S, raise_exceptions=False)
            if not self.c.bind():
                raise BereichFehler("anmeldung", "Anmeldung am Domänencontroller abgelehnt "
                                    f"({_schwaerzen(self.c.result.get('description'), kennwort)})")
        except LDAPException as exc:
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                import ews_onprem
                raise BereichFehler("tls", "TLS-Prüfung am Domänencontroller fehlgeschlagen — "
                                    "Zertifikat nicht vertrauenswürdig oder nicht auf den Hostnamen "
                                    "ausgestellt."
                                    + ews_onprem._tls_hinweis("das Zertifikat des Domänencontrollers",
                                                              "des Domänencontrollers")) from None
            raise BereichFehler("verbindung", "Domänencontroller nicht erreichbar "
                                f"({_schwaerzen(exc, kennwort)})") from None
        self.basis = _wert(KEY_BASIS) or self._rootdse("defaultNamingContext")
        if not self.basis:
            raise BereichFehler("konfiguration", "Basis-DN nicht ermittelbar — bitte eintragen")

    def _rootdse(self, name: str) -> str:
        try:
            werte = (self.c.server.info.other or {}).get(name) or []
            return str(werte[0]) if werte else ""
        except Exception:                                    # noqa: BLE001
            return ""

    def _pruefe(self, ok: bool, was: str) -> None:
        if not ok:
            r = self.c.result or {}
            raise BereichFehler("ldap", _schwaerzen(f"{was}: {r.get('description')} "
                                                    f"{r.get('message') or ''}".strip(), self._geheim))

    def suchen(self, filt: str, attribute: list[str], basis: str | None = None,
               bereich: str = "SUBTREE", steuerung=None) -> list[dict]:
        scope = {"SUBTREE": self._ldap3.SUBTREE, "BASE": self._ldap3.BASE}[bereich]
        eintraege = self.c.extend.standard.paged_search(
            basis or self.basis, filt, search_scope=scope, attributes=attribute,
            controls=steuerung, paged_size=500, generator=True)
        aus = []
        for e in eintraege:
            if e.get("type") != "searchResEntry":
                continue
            aus.append({"dn": e["dn"], **{k: v for k, v in (e.get("raw_attributes") or {}).items()}})
        r = self.c.result or {}
        if r.get("result") not in (0, None):
            raise BereichFehler("ldap", _schwaerzen(f"Suche: {r.get('description')}", self._geheim))
        return aus

    def lesen(self, dn: str, attribute: list[str], steuerung=None) -> dict | None:
        ok = self.c.search(dn, "(objectClass=*)", search_scope=self._ldap3.BASE,
                           attributes=attribute, controls=steuerung)
        if not ok:
            if (self.c.result or {}).get("result") == 32:      # noSuchObject
                return None
            self._pruefe(False, f"Lesen von {dn}")
        e = self.c.response[0]
        return {"dn": e["dn"], **(e.get("raw_attributes") or {})}

    def anlegen(self, dn: str, klassen: list[str], attribute: dict) -> None:
        self._pruefe(self.c.add(dn, klassen, attribute), f"Anlegen von {dn}")

    def ersetzen(self, dn: str, attribute: dict, steuerung=None) -> None:
        aend = {k: [(self._ldap3.MODIFY_REPLACE, [v])] for k, v in attribute.items()}
        self._pruefe(self.c.modify(dn, aend, controls=steuerung), f"Ändern von {dn}")

    def mitglied(self, gruppe_dn: str, dn: str, hinzu: bool) -> None:
        art = self._ldap3.MODIFY_ADD if hinzu else self._ldap3.MODIFY_DELETE
        self._pruefe(self.c.modify(gruppe_dn, {"member": [(art, [dn])]}),
                     f"Mitglied {'aufnehmen' if hinzu else 'entfernen'} ({dn})")

    def kennwort_setzen(self, dn: str, kennwort: str) -> None:
        # unicodePwd: UTF-16LE in Anführungszeichen; AD nimmt es nur über eine
        # verschlüsselte Verbindung an (hier immer LDAPS).
        wert = ('"%s"' % kennwort).encode("utf-16-le")
        ok = self.c.modify(dn, {"unicodePwd": [(self._ldap3.MODIFY_REPLACE, [wert])]})
        if not ok:
            r = self.c.result or {}
            raise BereichFehler("ldap", _schwaerzen(f"Kennwort setzen: {r.get('description')}",
                                                    kennwort, self._geheim))

    def sd_steuerung(self):
        from ldap3.protocol.microsoft import security_descriptor_control
        return security_descriptor_control(sdflags=DACL_SECURITY_INFORMATION)

    def schliessen(self) -> None:
        try:
            self.c.unbind()
        except Exception:                                    # noqa: BLE001
            pass


def _verbinden(benutzer: str, kennwort: str) -> Verzeichnis:
    """Die EINE Stelle, an der eine Verbindung entsteht (Tests ersetzen sie)."""
    return Verzeichnis(benutzer, kennwort)


def _verbinden_dienst() -> Verzeichnis:
    konto, kw, _neu = dienst_zugang()
    if not (konto and kw):
        raise BereichFehler("konfiguration", "Dienstkonto oder Kennwort nicht gespeichert")
    return _verbinden(konto, kw)


# ── Dienstkonto: wirksam und ausstehend ───────────────────────────────────────

def ausstehend() -> str:
    """Name des ausstehenden Dienstkontos oder "" — nur mit Kennwort ein Wechsel."""
    konto = _wert(KEY_NEU_KONTO)
    return konto if konto and settings_store.get(KEY_NEU_KENNWORT) else ""


def wirksames_konto() -> str:
    import ews_onprem
    return _wert(ews_onprem.KEY_KONTO)


def dienst_zugang() -> tuple[str, str, bool]:
    """(Konto, Kennwort, ausstehend?) für LDAP. Ein ausstehendes Konto geht vor:
    Die Einrichtung hat GENAU IHM die Delegation an der Gruppe erteilt — das
    bisherige Konto darf die Mitglieder womöglich nicht ändern. EWS selbst
    nutzt weiter das gespeicherte Konto (ews_onprem._zugang), bis `pruefen`
    übernimmt."""
    import ews_onprem
    if ausstehend():
        return _wert(KEY_NEU_KONTO), str(settings_store.get(KEY_NEU_KENNWORT) or ""), True
    return _wert(ews_onprem.KEY_KONTO), _wert(ews_onprem.KEY_KENNWORT), False


def _zugang_schreiben(wirksam: tuple[str, str] | None = None,
                      ausstehend_neu: tuple[str, str] = ("", ""),
                      weitere: dict | None = None) -> None:
    """DER Schreibweg für Dienstkonto-Zugangsdaten — Anlegen (ausstehend oder,
    bei Ersteinrichtung, wirksam), Übernehmen und Verwerfen. `wirksam` ersetzt
    EWS_ONPREM_KONTO/_KENNWORT; `ausstehend_neu` setzt bzw. leert (Vorgabe)
    EWS_BEREICH_NEU_*. Ein Aufruf, ein `update` — kein halber Zustand.
    EWS_ONPREM_KENNWORT und EWS_BEREICH_NEU_KENNWORT sind SECRET_KEYS."""
    import ews_onprem
    felder = dict(weitere or {})
    felder[KEY_NEU_KONTO], felder[KEY_NEU_KENNWORT] = ausstehend_neu
    if wirksam is not None:
        felder[ews_onprem.KEY_KONTO], felder[ews_onprem.KEY_KENNWORT] = wirksam
    settings_store.update(felder)
    _stand_setzen(ausstehend_seit=_iso() if ausstehend_neu[0] else "")


def verwerfen(wer: str) -> dict:
    """„Ausstehendes Konto verwerfen“: Zugangsdaten löschen, das bisherige
    Konto bleibt. Das AD-Konto selbst bleibt bestehen — das Gateway löscht im
    AD nichts, was es nicht pflegt."""
    konto = ausstehend()
    if not konto:
        _zugang_schreiben()          # auch einen halben Rest (Konto ohne Kennwort) leeren
        return {"ok": False, "error": "Kein ausstehendes Dienstkonto"}
    _zugang_schreiben()
    _audit("ews_bereich_konto_verworfen", konto,
           f"Ausstehendes Dienstkonto {konto} verworfen durch {wer}; weiter wirksam: "
           f"{wirksames_konto() or '—'}. Das AD-Konto besteht weiter.")
    return {"ok": True, "konto": konto,
            "hinweis": f"Das Konto {konto} besteht im AD weiter — entferne es dort, "
                       "wenn du es nicht mehr brauchst."}


# ── LDAP-Hilfen über dem Verzeichnis ──────────────────────────────────────────

def _esc(wert: str) -> str:
    """RFC 4515: Sonderzeichen in Filterwerten."""
    aus = []
    for ch in wert:
        if ch in "*()\\\x00":
            aus.append("\\%02x" % ord(ch))
        else:
            aus.append(ch)
    return "".join(aus)


def _text(wert) -> str:
    if isinstance(wert, list):
        wert = wert[0] if wert else b""
    return wert.decode("utf-8", "replace") if isinstance(wert, (bytes, bytearray)) else str(wert or "")


def _texte(wert) -> list[str]:
    if not isinstance(wert, list):
        wert = [wert] if wert else []
    return [_text(w) for w in wert]


def adressen_aufloesen(v: Verzeichnis, adressen: set[str]) -> tuple[dict[str, str], dict[str, str]]:
    """{adresse: DN} und {adresse: Grund} — über mail und proxyAddresses (smtp:)."""
    gefunden: dict[str, list[str]] = {a: [] for a in adressen}
    liste = sorted(adressen)
    for i in range(0, len(liste), SUCHE_JE_ANFRAGE):
        teil = liste[i:i + SUCHE_JE_ANFRAGE]
        oder = "".join(f"(mail={_esc(a)})(proxyAddresses=smtp:{_esc(a)})" for a in teil)
        filt = f"(&(objectCategory=person)(objectClass=user)(|{oder}))"
        for e in v.suchen(filt, ["mail", "proxyAddresses"]):
            eigene = {m.lower() for m in _texte(e.get("mail"))}
            eigene |= {p[5:].lower() for p in _texte(e.get("proxyAddresses"))
                       if p.lower().startswith("smtp:")}
            for a in teil:
                if a in eigene and e["dn"] not in gefunden[a]:
                    gefunden[a].append(e["dn"])
    dns, fehlt = {}, {}
    for a, treffer in gefunden.items():
        if len(treffer) == 1:
            dns[a] = treffer[0]
        elif not treffer:
            fehlt[a] = "kein Benutzerobjekt mit dieser Adresse im AD"
        else:
            fehlt[a] = f"{len(treffer)} Benutzerobjekte mit dieser Adresse — nicht eindeutig"
    return dns, fehlt


def gruppe_finden(v: Verzeichnis, name: str) -> dict | None:
    e = v.suchen(f"(&(objectClass=group)(sAMAccountName={_esc(name)}))",
                 ["distinguishedName", "groupType", "mail", "objectSid"])
    return e[0] if e else None


def konto_finden(v: Verzeichnis, konto: str) -> dict | None:
    """Konto als DOMÄNE\\sam, UPN oder sam."""
    k = (konto or "").strip()
    if "\\" in k:
        filt = f"(sAMAccountName={_esc(k.split(chr(92), 1)[1])})"
    elif "@" in k:
        filt = f"(|(userPrincipalName={_esc(k)})(sAMAccountName={_esc(k.split('@')[0])}))"
    else:
        filt = f"(sAMAccountName={_esc(k)})"
    e = v.suchen(f"(&(objectCategory=person)(objectClass=user){filt})",
                 ["objectSid", "userPrincipalName", "sAMAccountName"])
    return e[0] if len(e) == 1 else None


def mitglieder_lesen(v: Verzeichnis, gruppe_dn: str) -> list[str]:
    e = v.lesen(gruppe_dn, ["member"])
    if e is None:
        raise BereichFehler("gruppe", f"Gruppe {gruppe_dn} nicht (mehr) vorhanden")
    return _texte(e.get("member"))


def _mail_von(v: Verzeichnis, dn: str) -> str:
    try:
        e = v.lesen(dn, ["mail"])
    except BereichFehler:
        return ""
    return _text((e or {}).get("mail")).lower()


# ── Abgleich ──────────────────────────────────────────────────────────────────

def wartet(adresse: str, jetzt: datetime | None = None) -> bool:
    """Frisch in die Gruppe aufgenommen — Exchange übernimmt das verzögert."""
    seit = _aus_iso((stand().get("wartet") or {}).get((adresse or "").lower(), ""))
    return bool(seit and ((jetzt or _jetzt()) - seit).total_seconds() < WARTEZEIT_S)


def abgleichen(ausloeser: str, aendern: bool = True, verzeichnis: Verzeichnis | None = None) -> dict:
    """Soll ↔ Ist der Gruppe. `aendern=False` misst nur (Schalter aus):
    keine Änderung, aber die Abweichung steht danach im Stand (Aufgabe).

    → {ok, hinzu, weg, fehlt, fehler, soll, ist, geaendert}"""
    if not _lauf.acquire(blocking=False):
        return {"ok": False, "laeuft": True, "error": "Ein Abgleich läuft bereits"}
    v = verzeichnis
    try:
        s = stand()
        gdn = s.get("gruppe_dn")
        if not gdn:
            return {"ok": False, "error": "Gruppe nicht eingerichtet"}
        cfg = settings_store.get("MAILBOX_CONFIG")
        if not isinstance(cfg, dict):
            return {"ok": False, "error": "Postfachliste nicht lesbar — nichts geändert"}
        soll = soll_mitglieder(cfg)
        try:
            v = v or _verbinden_dienst()
            soll_dn, fehlt = adressen_aufloesen(v, soll)
            ist = mitglieder_lesen(v, gdn)
        except BereichFehler as exc:
            _stand_setzen(messung={"ts": _iso(), "ok": False, "fehler": exc.detail,
                                   "ausloeser": ausloeser})
            log.warning("EWS-Bereich: Abgleich (%s) gescheitert — %s", ausloeser, exc.detail)
            return {"ok": False, "code": exc.code, "error": exc.detail}
        ist_klein = {d.lower(): d for d in ist}
        soll_klein = {d.lower(): (d, a) for a, d in soll_dn.items()}
        hinzu = [soll_klein[k] for k in sorted(set(soll_klein) - set(ist_klein))]
        weg = [ist_klein[k] for k in sorted(set(ist_klein) - set(soll_klein))]
        wartend = {a: t for a, t in (s.get("wartet") or {}).items()
                   if a in soll and wartet(a)}
        fehler: list[dict] = []
        erledigt_hinzu: list[str] = []
        erledigt_weg: set[str] = set()
        weg_adr = {dn: (_mail_von(v, dn) or dn) for dn in weg}
        if aendern:
            for dn, adresse in hinzu:
                try:
                    v.mitglied(gdn, dn, True)
                except BereichFehler as exc:
                    fehler.append({"adresse": adresse, "detail": exc.detail})
                    continue
                erledigt_hinzu.append(adresse)
                wartend[adresse] = _iso()
                _audit("ews_bereich_mitglied_hinzu", adresse,
                       f"in Gruppe {gruppe_name()} aufgenommen ({ausloeser})")
            for dn in weg:
                adresse = weg_adr[dn]
                try:
                    v.mitglied(gdn, dn, False)
                except BereichFehler as exc:
                    fehler.append({"adresse": adresse, "detail": exc.detail})
                    continue
                erledigt_weg.add(dn)
                wartend.pop(adresse, None)
                _audit("ews_bereich_mitglied_weg", adresse,
                       f"aus Gruppe {gruppe_name()} entfernt — weder Signatur noch S/MIME "
                       f"als Hybrid-Nutzer ({ausloeser})")
        offen_hinzu = [a for _d, a in hinzu if a not in erledigt_hinzu]
        offen_weg = [weg_adr[d] for d in weg if d not in erledigt_weg]
        ist_adressen = sorted({a for a, d in soll_dn.items() if d.lower() in ist_klein}
                              | set(erledigt_hinzu) | set(offen_weg))
        messung = {"ts": _iso(), "ok": not fehler, "ausloeser": ausloeser, "geaendert": aendern,
                   "soll": len(soll), "ist": len(ist) + len(erledigt_hinzu) - len(erledigt_weg),
                   "fehlen": offen_hinzu, "zuviel": offen_weg,
                   "nicht_gefunden": fehlt, "fehler": fehler[:20]}
        _stand_setzen(messung=messung, wartet=wartend, mitglieder=ist_adressen,
                      soll_bei_messung=sorted(soll))
        try:
            import aufgaben
            aufgaben.vergessen()
        except Exception:                                    # noqa: BLE001
            pass
        log.info("EWS-Bereich: Abgleich (%s, %s) — Soll %d, +%d, -%d, offen +%d/-%d, "
                 "nicht im AD %d, Fehler %d", ausloeser, "ändernd" if aendern else "nur messen",
                 len(soll), len(erledigt_hinzu), len(erledigt_weg), len(offen_hinzu),
                 len(offen_weg), len(fehlt), len(fehler))
        return {"ok": not fehler, "hinzu": erledigt_hinzu, "weg": [weg_adr[d] for d in weg if d in erledigt_weg],
                "fehlen": offen_hinzu, "zuviel": offen_weg, "nicht_gefunden": fehlt,
                "fehler": fehler, "soll": len(soll), "geaendert": aendern}
    finally:
        if v is not None and verzeichnis is None:
            v.schliessen()
        _lauf.release()


def abweichung() -> dict:
    """NUR lokal (Aufgabe, Seitenaufbau): letzte Messung plus Postfächer, die
    seitdem aktiviert/abgeschaltet wurden. {zustand, fehlen, zuviel, ...}.
    zustand: "entfaellt" | "unbekannt" | "ok" | "offen"."""
    s = stand()
    if not s.get("gruppe_dn"):
        return {"zustand": "entfaellt"}
    m = s.get("messung") or {}
    if not m:
        return {"zustand": "unbekannt", "grund": "Noch nicht abgeglichen"}
    if not m.get("ok") and m.get("fehler") and isinstance(m.get("fehler"), str):
        return {"zustand": "unbekannt", "grund": f"Letzter Abgleich gescheitert: {m['fehler']}"}
    soll = soll_mitglieder()
    ist = set(s.get("mitglieder") or [])
    nicht_gefunden = {a: g for a, g in (m.get("nicht_gefunden") or {}).items() if a in soll}
    fehlen = sorted((soll - ist) - set(nicht_gefunden))
    zuviel = sorted(ist - soll)
    fehler = [f for f in (m.get("fehler") or []) if isinstance(f, dict)]
    offen = bool(fehlen or zuviel or fehler or nicht_gefunden)
    return {"zustand": "offen" if offen else "ok", "fehlen": fehlen, "zuviel": zuviel,
            "nicht_gefunden": nicht_gefunden, "fehler": fehler, "ts": m.get("ts", "")}


# ── Hintergrund (Speichern der Postfachliste, Scheduler) ──────────────────────

_hintergrund: set = set()


def nach_postfach_aenderung() -> str:
    """Aus dem Speicherweg der Postfachliste (Event-Loop). Mit Schalter an:
    Abgleich im Hintergrund; sonst nichts (die Aufgabe zeigt die Abweichung).
    → "gestartet" | "aus" | "nicht_eingerichtet"."""
    if not eingerichtet():
        return "nicht_eingerichtet"
    if not auto_an():
        try:
            import aufgaben
            aufgaben.vergessen()
        except Exception:                                    # noqa: BLE001
            pass
        return "aus"
    import asyncio

    async def _lauf_bg():
        try:
            await asyncio.to_thread(abgleichen, "Postfachliste gespeichert", True)
        except Exception as exc:                             # noqa: BLE001
            log.warning("EWS-Bereich: Abgleich nach Speichern gescheitert — %s", exc)
    t = asyncio.get_running_loop().create_task(_lauf_bg())
    _hintergrund.add(t)
    t.add_done_callback(_hintergrund.discard)
    return "gestartet"


def planmaessig() -> dict | None:
    """Scheduler: mit Schalter an abgleichen, sonst nur messen. Ohne
    Einrichtung oder ohne Dienstkonto kein Aufruf."""
    import ews_onprem
    if not eingerichtet() or not ews_onprem.konfiguriert() or not _wert(KEY_LDAP):
        return None
    return abgleichen("planmäßig", aendern=auto_an())


# ── Einrichtung: der Plan (EINE Quelle) ───────────────────────────────────────

@dataclass
class Schritt:
    kennung: str
    art: str                  # "ad" (Gateway führt aus) | "exchange" (nur Befehl)
    titel: str
    befehl: str
    werte: dict = field(default_factory=dict)


@dataclass
class Parameter:
    basis: str
    ou: str
    gruppe: str
    konto_anlegen: bool
    konto_sam: str
    konto_upn: str
    alt: dict = field(default_factory=dict)        # bestand_erkennen()
    alt_entfernen: bool = False

    @property
    def gruppe_dn(self) -> str:
        return f"CN={self.gruppe},{self.ou}"

    @property
    def konto_dn(self) -> str:
        return f"CN={self.konto_sam},{self.ou}"

    @property
    def konto_exchange(self) -> str:
        return self.konto_upn or self.konto_sam


def parameter(daten: dict, basis: str = "") -> Parameter:
    """Aus Formular + Einstellungen. BereichFehler bei ungültigen Werten."""
    import ews_onprem
    basis = (basis or daten.get("basis") or _wert(KEY_BASIS)).strip()
    ou = (daten.get("ou") or _wert(KEY_OU) or (f"CN=Users,{basis}" if basis else "")).strip()
    gruppe = (daten.get("gruppe") or gruppe_name()).strip()
    anlegen = bool(daten.get("konto_anlegen"))
    if anlegen:
        sam = (daten.get("konto") or KONTO_VORGABE).strip()
        upn_dom = ".".join(t.split("=", 1)[1] for t in basis.split(",")
                           if t.strip().upper().startswith("DC="))
        upn = f"{sam}@{upn_dom}" if upn_dom else ""
    else:
        vorhanden = (daten.get("konto") or _wert(ews_onprem.KEY_KONTO)).strip()
        sam = vorhanden.split("\\", 1)[-1].split("@")[0]
        upn = vorhanden if "@" in vorhanden else ""
    for f in (dn_pruefen(basis, "Basis-DN"), dn_pruefen(ou, "Ablage (OU)"),
              name_pruefen(gruppe, "Gruppenname"), name_pruefen(sam, "Dienstkonto")):
        if f:
            raise BereichFehler("eingabe", f)
    if not basis or not ou:
        raise BereichFehler("eingabe", "Basis-DN fehlt")
    alt = daten.get("alt") if isinstance(daten.get("alt"), dict) else {}
    return Parameter(basis, ou, gruppe, anlegen, sam, upn, alt, bool(daten.get("alt_entfernen")))


def plan(p: Parameter) -> list[Schritt]:
    kommentar = ps_text.kommentar   # Titel stehen als #-Zeile im Skript
    schritte: list[Schritt] = []
    if p.konto_anlegen:
        schritte.append(Schritt(
            "konto", "ad", f"Dienstkonto {kommentar(p.konto_sam)} anlegen (Kennwort zufällig, läuft nicht ab)",
            f"New-ADUser -Name {ps_text.quoted(p.konto_sam)} -SamAccountName {ps_text.quoted(p.konto_sam)} "
            f"-UserPrincipalName {ps_text.quoted(p.konto_upn)} -Path {ps_text.quoted(p.ou)} "
            f"-Description {ps_text.quoted('EWS-Dienstkonto des EXO Signature Gateway')} "
            "-AccountPassword (Read-Host -AsSecureString 'Kennwort') "
            "-PasswordNeverExpires $true -CannotChangePassword $true -Enabled $true",
            {"dn": p.konto_dn, "sam": p.konto_sam, "upn": p.konto_upn}))
    schritte.append(Schritt(
        "gruppe", "ad", f"Universelle Sicherheitsgruppe {kommentar(p.gruppe)} anlegen (ohne Mailadresse)",
        f"New-ADGroup -Name {ps_text.quoted(p.gruppe)} -SamAccountName {ps_text.quoted(p.gruppe)} -GroupScope Universal "
        f"-GroupCategory Security -Path {ps_text.quoted(p.ou)} "
        f"-Description {ps_text.quoted('Postfächer, die das EXO Signature Gateway per EWS bedient')}",
        {"dn": p.gruppe_dn, "sam": p.gruppe}))
    schritte.append(Schritt(
        "delegation", "ad", f"Dienstkonto darf NUR die Mitglieder von {kommentar(p.gruppe)} ändern",
        f"Add-ADPermission -Identity {ps_text.quoted(p.gruppe_dn)} -User {ps_text.quoted(p.konto_exchange)} "
        "-AccessRights WriteProperty -Properties member",
        {"gruppe_dn": p.gruppe_dn, "konto": p.konto_exchange}))
    alte = sorted({a for z in (p.alt.get("zuweisungen") or [])
                   if z.get("art") == "liste" for a in z.get("adressen") or []})
    if alte:
        schritte.append(Schritt(
            "uebernahme", "ad", f"{kommentar(len(alte))} Adressen aus der bisherigen festen Liste in die Gruppe",
            "\n".join(f"Add-ADGroupMember -Identity {ps_text.quoted(p.gruppe_dn)} -Members "
                      f"(Get-ADUser -LDAPFilter {ps_text.quoted('(mail=' + _esc(a) + ')')})" for a in alte),
            {"adressen": alte}))
    schritte.append(Schritt(
        "scope", "exchange", f"Management Scope {kommentar(scope_name(p.gruppe))} auf die Gruppe",
        f"New-ManagementScope -Name {ps_text.quoted(scope_name(p.gruppe))} "
        f"-RecipientRestrictionFilter {ps_text.quoted(f'MemberOfGroup -eq {chr(39)}{p.gruppe_dn}{chr(39)}')}",
        {"scope": scope_name(p.gruppe), "gruppe_dn": p.gruppe_dn}))
    schritte.append(Schritt(
        "zuweisung", "exchange", "ApplicationImpersonation für das Dienstkonto, begrenzt auf den Scope",
        f"New-ManagementRoleAssignment -Name {ps_text.quoted(zuweisung_name(p.gruppe))} "
        f"-Role ApplicationImpersonation -User {ps_text.quoted(p.konto_exchange)} "
        f"-CustomRecipientWriteScope {ps_text.quoted(scope_name(p.gruppe))}",
        {"zuweisung": zuweisung_name(p.gruppe), "scope": scope_name(p.gruppe)}))
    if p.alt_entfernen:
        for z in p.alt.get("zuweisungen") or []:
            if z.get("art") == "unsere":
                continue
            befehle = [f"Remove-ManagementRoleAssignment -Identity {ps_text.quoted(z.get('name'))} -Confirm:$false"]
            if z.get("bereich"):
                befehle.append(f"# erst, wenn keine andere Zuweisung den Scope nutzt:\n"
                               f"Remove-ManagementScope -Identity {ps_text.quoted(z.get('bereich'))} -Confirm:$false")
            schritte.append(Schritt(
                "alt_" + re.sub(r"\W", "_", str(z.get("name")))[:40], "exchange",
                f"Bisherige Zuweisung {kommentar(z.get('name'))} entfernen (erst nach erfolgreicher Prüfung)",
                "\n".join(befehle), {"zuweisung": z.get("name"), "scope": z.get("bereich")}))
    return schritte


def befehle_text(schritte: list[Schritt]) -> str:
    """Alles zum Kopieren — für Admins, die es selbst in der Shell machen."""
    zeilen = ["# EXO Signature Gateway — Zugriffsbereich des EWS-Dienstkontos",
              "# In der Exchange-Verwaltungsshell (lokal) mit dem AD-Modul ausführen.",
              "Import-Module ActiveDirectory", ""]
    for s in schritte:
        zeilen += ["# " + ps_text.kommentar(s.titel), s.befehl, ""]
    return "\n".join(zeilen)


BESTAND_BEFEHL = (
    "[pscustomobject]@{\n"
    "  Zuweisungen = @(Get-ManagementRoleAssignment -Role ApplicationImpersonation | ForEach-Object {\n"
    "    [pscustomobject]@{ Name = \"$($_.Name)\"; Konto = \"$($_.RoleAssigneeName)\";\n"
    "      Bereich = \"$($_.CustomRecipientWriteScope)\"; Schreibbereich = \"$($_.RecipientWriteScope)\" } })\n"
    "  Bereiche = @(Get-ManagementScope | ForEach-Object {\n"
    "    [pscustomobject]@{ Name = \"$($_.Name)\"; Filter = \"$($_.RecipientFilter)\" } })\n"
    "} | ConvertTo-Json -Depth 4 -Compress")


_KLAUSEL = re.compile(r"^(PrimarySmtpAddress|WindowsEmailAddress|EmailAddresses)\s+-eq\s+'([^']+)'$", re.I)
_GRUPPE = re.compile(r"^MemberOfGroup\s+-eq\s+'([^']+)'$", re.I)


def _filter_art(filt: str) -> tuple[str, list[str], str]:
    """(art, adressen, gruppe_dn) für einen RecipientFilter."""
    f = (filt or "").strip()
    flach = re.sub(r"[()]", " ", f).strip()
    m = _GRUPPE.match(re.sub(r"\s+", " ", flach))
    if m:
        return "gruppe", [], m.group(1)
    teile = [t.strip() for t in re.split(r"\s+-or\s+", flach, flags=re.I) if t.strip()]
    adressen = []
    for t in teile:
        k = _KLAUSEL.match(re.sub(r"\s+", " ", t))
        if not k:
            return "anders", [], ""
        a = k.group(2).strip().lower()
        if a.startswith("smtp:"):
            a = a[5:]
        adressen.append(a)
    return ("liste", adressen, "") if adressen else ("anders", [], "")


def bestand_erkennen(roh: str, konto: str, gruppe_dn: str = "") -> dict:
    """Ausgabe von BESTAND_BEFEHL (JSON) auswerten: Welche
    ApplicationImpersonation-Zuweisungen hat `konto`, und wie ist ihr Bereich
    gefasst? Ändert nichts. {zuweisungen: [{name, bereich, art, adressen, filter}],
    umbau: bool}"""
    import json
    try:
        d = json.loads(roh or "")
    except ValueError:
        raise BereichFehler("eingabe", "Die Ausgabe ist kein JSON — bitte die ganze Ausgabe "
                                       "des Befehls einfügen") from None
    if not isinstance(d, dict):
        raise BereichFehler("eingabe", "Unerwartete Form der Ausgabe")
    zuw = d.get("Zuweisungen") or []
    ber = d.get("Bereiche") or []
    zuw = zuw if isinstance(zuw, list) else [zuw]
    ber = ber if isinstance(ber, list) else [ber]
    filter_je = {str(b.get("Name", "")).lower(): str(b.get("Filter", "")) for b in ber
                 if isinstance(b, dict)}
    k = (konto or "").strip().lower()
    namen = {k, k.split("\\", 1)[-1], k.split("@")[0]} - {""}
    aus = []
    for z in zuw:
        if not isinstance(z, dict):
            continue
        wer = str(z.get("Konto", "")).strip().lower()
        if not ({wer, wer.split("\\", 1)[-1], wer.split("@")[0]} & namen):
            continue
        bereich = str(z.get("Bereich") or "").strip()
        if not bereich:
            art, adressen, gdn = ("organisation" if str(z.get("Schreibbereich", "")).lower()
                                  in ("organization", "") else "anders"), [], ""
            filt = ""
        else:
            filt = filter_je.get(bereich.lower(), "")
            art, adressen, gdn = _filter_art(filt)
            if art == "gruppe" and gruppe_dn and gdn.lower() == gruppe_dn.lower():
                art = "unsere"
        aus.append({"name": str(z.get("Name", "")), "bereich": bereich, "art": art,
                    "adressen": adressen, "filter": filt[:500], "gruppe_dn": gdn})
    return {"zuweisungen": aus, "umbau": any(z["art"] != "unsere" for z in aus)}


# ── Einrichtung ausführen (nur der AD-Teil) ───────────────────────────────────

def zufallskennwort(laenge: int = 32) -> str:
    """Lang, zufällig, erfüllt die AD-Komplexität (alle vier Klassen)."""
    zeichen = string.ascii_letters + string.digits + "-_.!#%+=?"
    while True:
        kw = "".join(secrets.choice(zeichen) for _ in range(laenge))
        if (any(c.islower() for c in kw) and any(c.isupper() for c in kw)
                and any(c.isdigit() for c in kw) and any(c in "-_.!#%+=?" for c in kw)):
            return kw


def _sid_von(v: Verzeichnis, dn: str) -> bytes:
    e = v.lesen(dn, ["objectSid"])
    sid = (e or {}).get("objectSid")
    sid = sid[0] if isinstance(sid, list) and sid else sid
    if not isinstance(sid, (bytes, bytearray)) or len(sid) < 8:
        raise BereichFehler("ldap", f"SID von {dn} nicht lesbar")
    return bytes(sid)


def _delegieren(v: Verzeichnis, gruppe_dn: str, konto_dn: str) -> bool:
    sid = _sid_von(v, konto_dn)
    steuerung = v.sd_steuerung()
    e = v.lesen(gruppe_dn, ["nTSecurityDescriptor"], steuerung=steuerung)
    roh = (e or {}).get("nTSecurityDescriptor")
    roh = roh[0] if isinstance(roh, list) and roh else roh
    if not roh:
        raise BereichFehler("sd", "Sicherheitsbeschreibung der Gruppe nicht lesbar")
    sd = sd_lesen(bytes(roh))
    if not delegation_ergaenzen(sd, sid):
        return False
    v.ersetzen(gruppe_dn, {"nTSecurityDescriptor": sd_schreiben(sd)}, steuerung=steuerung)
    return True


def einrichten(daten: dict, admin_konto: str, admin_kennwort: str, wer: str) -> dict:
    """AD-Teil des Plans ausführen, mit der EINMALIGEN Admin-Anmeldung.

    Das Admin-Kennwort lebt nur in dieser Funktion und der Verbindung. Jeder
    Fehlertext geht durch `_schwaerzen`. Bestehende Objekte werden nicht
    ersetzt: Ein vorhandenes Konto bricht ab (sonst stünde ein neues Kennwort
    an einem fremden Konto), eine vorhandene Gruppe wird nur übernommen, wenn
    sie eine universelle Sicherheitsgruppe ist.

    → {ok, schritte: [{kennung, ergebnis}], befehle, plan}"""
    import ews_onprem
    if not admin_konto or not admin_kennwort:
        return {"ok": False, "error": "Admin-Konto und Kennwort angeben"}
    erledigt: list[dict] = []
    v = None
    try:
        v = _verbinden(admin_konto, admin_kennwort)
        p = parameter(daten, basis=v.basis)
        schritte = plan(p)
        neues_kennwort = ""
        konto_dn = p.konto_dn
        for s in schritte:
            if s.art != "ad":
                continue
            if s.kennung == "konto":
                if konto_finden(v, p.konto_sam):
                    raise BereichFehler("bestand", f"Konto {p.konto_sam} gibt es schon — nicht "
                                        "überschrieben. Anderen Namen wählen oder „vorhandenes "
                                        "Dienstkonto“ nehmen.")
                v.anlegen(konto_dn, ["top", "person", "organizationalPerson", "user"],
                          {"sAMAccountName": p.konto_sam, "userPrincipalName": p.konto_upn,
                           "displayName": p.konto_sam,
                           "description": "EWS-Dienstkonto des EXO Signature Gateway"})
                neues_kennwort = zufallskennwort()
                v.kennwort_setzen(konto_dn, neues_kennwort)
                v.ersetzen(konto_dn, {"userAccountControl": str(UAC_NORMAL_ACCOUNT
                                                                | UAC_DONT_EXPIRE_PASSWORD)})
                _audit("ews_bereich_konto_angelegt", p.konto_upn or p.konto_sam,
                       f"Dienstkonto {konto_dn} angelegt durch {wer}")
                erledigt.append({"kennung": s.kennung, "ergebnis": "angelegt"})
            elif s.kennung == "gruppe":
                g = gruppe_finden(v, p.gruppe)
                if g:
                    typ = int(_text(g.get("groupType")) or 0)
                    if typ != GRUPPE_UNIVERSAL_SICHERHEIT or _text(g.get("mail")):
                        raise BereichFehler("bestand", f"Gruppe {p.gruppe} gibt es schon, aber nicht "
                                            "als universelle Sicherheitsgruppe ohne Mailadresse — "
                                            "nicht verändert. Anderen Namen wählen.")
                    p_dn = g["dn"]
                    erledigt.append({"kennung": s.kennung, "ergebnis": f"vorhanden ({p_dn})"})
                    _stand_setzen(gruppe_dn=p_dn)
                    continue
                v.anlegen(p.gruppe_dn, ["top", "group"],
                          {"sAMAccountName": p.gruppe, "groupType": str(GRUPPE_UNIVERSAL_SICHERHEIT),
                           "description": "Postfächer, die das EXO Signature Gateway per EWS bedient"})
                _audit("ews_bereich_gruppe_angelegt", p.gruppe, f"Gruppe {p.gruppe_dn} angelegt durch {wer}")
                _stand_setzen(gruppe_dn=p.gruppe_dn)
                erledigt.append({"kennung": s.kennung, "ergebnis": "angelegt"})
            elif s.kennung == "delegation":
                gdn = stand().get("gruppe_dn") or p.gruppe_dn
                if not p.konto_anlegen:
                    k = konto_finden(v, p.konto_exchange)
                    if not k:
                        raise BereichFehler("bestand", f"Dienstkonto {p.konto_exchange} im AD nicht gefunden")
                    konto_dn = k["dn"]
                geaendert = _delegieren(v, gdn, konto_dn)
                if geaendert:
                    _audit("ews_bereich_delegation", p.konto_exchange,
                           f"WriteProperty member auf {gdn} erteilt durch {wer}")
                erledigt.append({"kennung": s.kennung,
                                 "ergebnis": "erteilt" if geaendert else "war schon vorhanden"})
            elif s.kennung == "uebernahme":
                gdn = stand().get("gruppe_dn") or p.gruppe_dn
                dns, fehlt = adressen_aufloesen(v, set(s.werte["adressen"]))
                vorhanden = {d.lower() for d in mitglieder_lesen(v, gdn)}
                for a, dn in sorted(dns.items()):
                    if dn.lower() in vorhanden:
                        continue
                    v.mitglied(gdn, dn, True)
                    _audit("ews_bereich_mitglied_hinzu", a, f"aus der bisherigen Liste übernommen ({wer})")
                erledigt.append({"kennung": s.kennung,
                                 "ergebnis": f"{len(dns)} übernommen, {len(fehlt)} nicht im AD"})
        felder = {KEY_GRUPPE: p.gruppe}
        if not _wert(KEY_BASIS):
            felder[KEY_BASIS] = p.basis
        if not _wert(KEY_OU):
            felder[KEY_OU] = p.ou
        wirksam_neu = False
        if neues_kennwort:
            neu = (p.konto_upn or p.konto_sam, neues_kennwort)
            if _wert(ews_onprem.KEY_KONTO) and _wert(ews_onprem.KEY_KENNWORT):
                # Es gibt ein bisheriges Konto, mit dem EWS (vielleicht) läuft.
                # Das neue hat bis zu den Exchange-Befehlen keine Impersonation —
                # sofort wirksam, fiele EWS für ALLE Hybrid-Nutzer aus. Also
                # ausstehend, bis `pruefen` es bestätigt.
                vorher = ausstehend()
                _zugang_schreiben(ausstehend_neu=neu, weitere=felder)
                _audit("ews_bereich_konto_ausstehend", neu[0],
                       f"Neues Dienstkonto {neu[0]} wartet auf Exchange-Befehle und Prüfung; "
                       f"weiter wirksam: {wirksames_konto()}"
                       + (f"; ersetzt das ausstehende {vorher}" if vorher else "") + f" ({wer})")
            else:
                # Ersteinrichtung: kein bisheriges Konto, also nichts, was
                # ausfallen könnte — und EWS braucht ein gespeichertes Konto, damit
                # „Verbindung testen“ und die Postfachprüfung überhaupt laufen.
                _zugang_schreiben(wirksam=neu, weitere=felder)
                wirksam_neu = True
        else:
            settings_store.update(felder)
        _stand_setzen(konto_dn=konto_dn, eingerichtet=_iso(), eingerichtet_von=wer,
                      scope=scope_name(p.gruppe), zuweisung=zuweisung_name(p.gruppe))
        offen = [s for s in schritte if s.art == "exchange"]
        return {"ok": True, "schritte": erledigt, "konto_neu": bool(neues_kennwort),
                "konto_ausstehend": ausstehend(), "zugang_gewechselt": wirksam_neu,
                "befehle": befehle_text(offen),
                "plan": [s.__dict__ for s in schritte]}
    except BereichFehler as exc:
        detail = _schwaerzen(exc.detail, admin_kennwort)
        log.warning("EWS-Bereich: Einrichtung durch %s abgebrochen nach %d Schritten — %s",
                    wer, len(erledigt), detail)
        if erledigt:
            _audit("ews_bereich_einrichtung_abgebrochen", wer,
                   f"nach {', '.join(e['kennung'] for e in erledigt)}: {detail}")
        return {"ok": False, "code": exc.code, "error": detail, "schritte": erledigt}
    except Exception as exc:                                 # noqa: BLE001
        detail = _schwaerzen(f"{type(exc).__name__}: {exc}", admin_kennwort)
        log.warning("EWS-Bereich: Einrichtung durch %s gescheitert — %s", wer, detail)
        return {"ok": False, "code": "intern", "error": detail, "schritte": erledigt}
    finally:
        if v is not None:
            v.schliessen()


# ── Prüfen (nach den Exchange-Befehlen) ───────────────────────────────────────

def pruefen(wer: str) -> dict:
    """Mit dem Dienstkonto: Gruppe da? Delegation da? Abweichung? Und EWS:
    Darf das Konto ein Mitglied übernehmen (belegt Scope und Zuweisung)?
    Ändert nichts im AD. Ergebnis im Stand (`pruefung`).

    Steht ein Konto AUS (`ausstehend`), prüft alles mit DESSEN Zugangsdaten.
    Nur wenn jeder Punkt bestanden ist — auch die EWS-Probe, „nicht prüfbar“
    reicht nicht —, wird es wirksam (`zugang_gewechselt`). Sonst bleibt das
    bisherige Konto, und der Punkt „Kontowechsel“ nennt den Grund."""
    konto, kw, neu = dienst_zugang()
    punkte = _pruefpunkte(wer, konto, kw, neu)
    if not neu:
        return _pruefung_merken(punkte)
    alt = wirksames_konto() or "—"
    offen = [p for p in punkte if p["ok"] is not True]
    if offen:
        grund = "; ".join(f"{p['name']}: {p['detail']}" for p in offen)
        punkte.append({"name": "Kontowechsel", "ok": False,
                       "detail": _schwaerzen(f"{konto} nicht übernommen ({grund}) — das Gateway "
                                             f"arbeitet weiter mit {alt}", kw)})
        return _pruefung_merken(punkte)
    with settings_store._lock:
        # Zwischen Prüfung und Übernahme verworfen oder neu angelegt? Dann nicht.
        if not (ausstehend() == konto and settings_store.get(KEY_NEU_KENNWORT) == kw):
            punkte.append({"name": "Kontowechsel", "ok": False,
                           "detail": "Ausstehendes Konto während der Prüfung geändert — erneut prüfen"})
            return _pruefung_merken(punkte)
        _zugang_schreiben(wirksam=(konto, kw))
    _audit("ews_bereich_konto_gewechselt", konto,
           f"Dienstkonto gewechselt von {alt} auf {konto} nach bestandener Prüfung ({wer})")
    punkte.append({"name": "Kontowechsel", "ok": True,
                   "detail": f"{konto} ist jetzt wirksam (vorher {alt})"})
    return {**_pruefung_merken(punkte), "zugang_gewechselt": True}


def _pruefpunkte(wer: str, konto: str, kw: str, neu: bool) -> list[dict]:
    """Die Punkte von `pruefen`, mit genau diesen Zugangsdaten."""
    import ews_onprem
    punkte: list[dict] = []
    v = None
    try:
        if not (konto and kw):
            raise BereichFehler("konfiguration", "Dienstkonto oder Kennwort nicht gespeichert")
        v = _verbinden(konto, kw)
        g = gruppe_finden(v, gruppe_name())
        if not g:
            punkte.append({"name": "Gruppe", "ok": False, "detail": f"{gruppe_name()} nicht gefunden"})
            return punkte
        if stand().get("gruppe_dn", "").lower() != g["dn"].lower():
            _stand_setzen(gruppe_dn=g["dn"])
            _audit("ews_bereich_gruppe_erkannt", gruppe_name(), f"Gruppe {g['dn']} übernommen ({wer})")
        punkte.append({"name": "Gruppe", "ok": True, "detail": g["dn"]})
        k = konto_finden(v, konto)
        if not k:
            punkte.append({"name": "Delegation", "ok": False, "detail": "Dienstkonto im AD nicht gefunden"})
        else:
            e = v.lesen(g["dn"], ["nTSecurityDescriptor"], steuerung=v.sd_steuerung())
            roh = (e or {}).get("nTSecurityDescriptor")
            roh = roh[0] if isinstance(roh, list) and roh else roh
            sid = _sid_von(v, k["dn"])
            ok = bool(roh) and darf_member_schreiben(sd_lesen(bytes(roh)), sid)
            punkte.append({"name": "Delegation", "ok": ok,
                           "detail": "Dienstkonto darf die Mitglieder ändern" if ok else
                           "Dienstkonto darf die Mitglieder NICHT ändern — Einrichtung oder "
                           "Add-ADPermission ausführen"})
    except BereichFehler as exc:
        punkte.append({"name": "Verzeichnis", "ok": False, "detail": _schwaerzen(exc.detail, kw)})
        return punkte
    finally:
        if v is not None:
            v.schliessen()
    erg = abgleichen(f"Prüfung durch {wer}", aendern=False)
    punkte.append({"name": "Mitglieder", "ok": bool(erg.get("ok")) and not erg.get("fehlen")
                   and not erg.get("zuviel"),
                   "detail": erg.get("error") or (f"Soll {erg.get('soll', 0)}, es fehlen "
                                                  f"{len(erg.get('fehlen') or [])}, zu viel "
                                                  f"{len(erg.get('zuviel') or [])}")})
    alle = list(stand().get("mitglieder") or [])
    mitglieder = [a for a in alle if not wartet(a)]
    name = "EWS-Zugriff" + (f" ({konto})" if neu else "")
    # Ausstehendes Konto: notfalls auch ein frisches Mitglied versuchen — gelingt
    # es, ist der Wechsel belegt; scheitert es, gilt das in der Wartezeit nicht
    # als Fehler (Exchange übernimmt verzögert), übernommen wird trotzdem nicht.
    if not mitglieder and neu and alle:
        probe = alle[0]
        t = ews_onprem.verbindung_testen(probe, sperre_umgehen=True, zugang=(konto, kw))
        punkte.append({"name": name, "ok": True if t.get("ok") else None,
                       "detail": _schwaerzen(f"{probe}: " + (
                           t.get("detail") if t.get("ok") else
                           f"wartet auf Exchange (bis {WARTEZEIT_S // 3600} h) — {t.get('detail') or ''}"),
                           kw)})
    elif not mitglieder:
        punkte.append({"name": name, "ok": None,
                       "detail": "Kein Mitglied außerhalb der Wartezeit — noch nicht prüfbar"})
    else:
        probe = mitglieder[0]
        t = ews_onprem.verbindung_testen(probe, sperre_umgehen=True,
                                         zugang=(konto, kw) if neu else None)
        punkte.append({"name": name, "ok": bool(t.get("ok")),
                       "detail": _schwaerzen(f"{probe}: " + (t.get("detail") or ""), kw)})
    return punkte


def _pruefung_merken(punkte: list[dict]) -> dict:
    ok = all(p["ok"] is not False for p in punkte)
    _stand_setzen(pruefung={"ts": _iso(), "ok": ok, "punkte": punkte})
    return {"ok": ok, "punkte": punkte}


def uebersicht() -> dict:
    """Für die Karte — nur lokal."""
    s = stand()
    return {"eingerichtet": bool(s.get("gruppe_dn")), "gruppe": gruppe_name(),
            "gruppe_dn": s.get("gruppe_dn", ""), "auto": auto_an(),
            "mitglieder": len(s.get("mitglieder") or []),
            "soll": len(soll_mitglieder()),
            "messung": s.get("messung") or {}, "pruefung": s.get("pruefung") or {},
            "wartend": sorted(a for a in (s.get("wartet") or {}) if wartet(a)),
            "abweichung": abweichung(), "aenderungen": letzte_aenderungen(),
            "eingerichtet_am": s.get("eingerichtet", ""), "wartezeit_h": WARTEZEIT_S // 3600,
            # Nur Namen — das Kennwort verlässt das Modul nie.
            "ausstehend": ausstehend(), "ausstehend_seit": s.get("ausstehend_seit", ""),
            "wirksam": wirksames_konto()}
