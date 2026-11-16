"""Notbetrieb: Post von außen annehmen, solange der Notfall ausgerufen ist.

Im Normalbetrieb nimmt Port 25 nur Exchange Online an (smtp_acl /
exo_erkennung), und davor filtert die Firewall auf Microsofts Bereiche. Fällt
Exchange aus, zeigt der MX auf das Gateway: Dann kommt Post aus dem ganzen
Internet — und von anderen Microsoft-365-Firmen aus Microsofts Bereichen.

DREI QUELLEN
------------
`connector`  Microsoft-Bereich mit der EIGENEN Tenant-Kennung — eigener Exchange,
             wie bisher (Tenant-Gate, Signatur …).
`microsoft`  Microsoft-Bereich, fremde oder keine Kennung — eine andere M365-Firma
             schreibt uns. Nur interne Empfänger, kein Greylisting (Microsoft
             wiederholt verlässlich, und ein Aufschub hälfe nichts).
`internet`   alles andere. Empfängerprüfung, Greylisting, Begrenzungen.
Relay-Geräte und vorgelagerte Gateways bleiben, was sie sind.

Die Quelle entscheidet sich zweistufig: beim Empfänger (RCPT) an der Adresse —
die Kopfzeilen kennt das Gateway erst nach DATA —, beim Inhalt endgültig an der
Tenant-Kennung (`quelle_nach_kopf`).

EMPFÄNGERPRÜFUNG
----------------
Nur Postfächer aus dem Rechte-Abbild, Aliase über die gesicherten Profile
(`notbetrieb.postfach_fuer`). Alles andere → 550 schon beim Empfänger: kein
offenes Relay, und Spam an erfundene Adressen kostet nichts. Nach
`MAX_FEHLVERSUCHE` unbekannten Empfängern einer Sitzung → 421.

GREYLISTING
-----------
Erster Versuch einer neuen Kombination (Absendernetz, Absender, Empfänger) →
451; ein echter Mailserver wiederholt und kommt nach `GREY_WARTEN_S` durch.
Netz statt Adresse (/24, /64), weil große Anbieter von wechselnden Adressen
wiederholen. Ein Netz, das einmal durchkam, gilt `GREY_NETZ_TAGE` lang als
bekannt.

Echtheit (SPF/DKIM/DMARC) danach in `notbetrieb_echtheit`. Noch NICHT:
Inhaltsfilter.
"""
from __future__ import annotations

import ipaddress
import logging
import threading
import time

import settings_store

log = logging.getLogger(__name__)

CONNECTOR, MICROSOFT, INTERNET = "connector", "microsoft", "internet"

MAX_FEHLVERSUCHE = 3
GREY_WARTEN_S = 300
GREY_FENSTER_S = 24 * 3600
GREY_NETZ_TAGE = 30
VORGABEN = {"NOTBETRIEB_EINGANG_GREYLIST": True, "NOTBETRIEB_EINGANG_MAX_EMPF": 50,
            "NOTBETRIEB_EINGANG_MAX_MB": 25, "NOTBETRIEB_EINGANG_JE_STUNDE": 30}

_lock = threading.Lock()
_takt: dict[str, list[float]] = {}          # Netz → Zeitpunkte angenommener Mails


def _zahl(schluessel: str) -> int:
    try:
        return max(1, int(settings_store.get(schluessel) or VORGABEN[schluessel]))
    except (TypeError, ValueError):
        return VORGABEN[schluessel]


def greylisting_an() -> bool:
    return settings_store.get("NOTBETRIEB_EINGANG_GREYLIST") is not False


def max_empfaenger() -> int:
    return _zahl("NOTBETRIEB_EINGANG_MAX_EMPF")


def max_bytes() -> int:
    return _zahl("NOTBETRIEB_EINGANG_MAX_MB") * 1024 * 1024


def je_stunde() -> int:
    return _zahl("NOTBETRIEB_EINGANG_JE_STUNDE")


def netz(ip: str) -> str:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    praefix = 24 if a.version == 4 else 64
    return str(ipaddress.ip_network(f"{ip}/{praefix}", strict=False))


def _aus_microsoft(ip: str, session) -> bool:
    """Dieselben Merkmale wie exo_erkennung.pruefe — ohne dessen Zählung, die
    gehört zur Annahme nach DATA."""
    import exo_erkennung
    try:
        return bool(exo_erkennung._adresse_reicht(ip)
                    or exo_erkennung.zertifikat_von_exo(exo_erkennung._peercert(session)))
    except Exception:                                               # noqa: BLE001
        return False


def quelle(ip: str, session) -> str | None:
    """None = kein Notfall-Eingang (nicht ausgerufen, keine Adresse, Relay-Gerät,
    vorgelagertes Gateway). Sonst `microsoft` oder `internet` — ob `microsoft`
    in Wahrheit der eigene Connector ist, klärt erst der Kopf."""
    import notbetrieb
    if not ip or not notbetrieb.ausgerufen():
        return None
    if getattr(session, "authenticated", False):
        return None                     # Submission (587): eigene Prüfung, beliebige Ziele
    import smtp_relay
    if smtp_relay.ist_vorgelagertes_gateway(ip) or smtp_relay.ist_relay_quelle(ip):
        return None
    return MICROSOFT if _aus_microsoft(ip, session) else INTERNET


def quelle_nach_kopf(vorlaeufig: str | None, kopf_tenant: str) -> str | None:
    if vorlaeufig == MICROSOFT:
        import config
        eigen = (config.TENANT_ID or settings_store.get("TENANT_ID") or "").strip().lower()
        if eigen and (kopf_tenant or "").strip().lower() == eigen:
            return CONNECTOR
    return vorlaeufig


# ── Empfänger ───────────────────────────────────────────────────────────────

def _nur_intern(rcpt: str) -> bool:
    """Verteiler/Gruppe, die in Exchange nur Absender der Organisation annimmt
    (RequireSenderAuthenticationEnabled) — Post von außen weist auch das
    Gateway ab, sonst wäre der Notfall ein Weg an der Sperre vorbei."""
    import notbetrieb
    e = notbetrieb.empfaenger_eintrag(rcpt)
    return bool(e and e["nur_intern"])


def rcpt_pruefen(ip: str, session, mail_from: str, rcpt: str, bisher: int) -> str | None:
    """Antwort für RCPT, oder None = annehmen. `bisher` = schon angenommene
    Empfänger dieser Mail."""
    q = quelle(ip, session)
    if q is None:
        return None
    import notbetrieb
    fehl = getattr(session, "_nb_fehlversuche", 0)
    if fehl >= MAX_FEHLVERSUCHE:
        return "421 4.7.0 Zu viele unbekannte Empfänger"
    if bisher >= max_empfaenger():
        return "452 4.5.3 Zu viele Empfänger"
    if not notbetrieb.bekannt(rcpt):
        if q == MICROSOFT:
            # Kann noch der eigene Connector sein (Post nach außen) — das
            # entscheidet der Kopf nach DATA.
            return None
        session._nb_fehlversuche = fehl + 1
        _zaehlen("unbekannt")
        log.info("NOTFALL-EINGANG: %s → %s unbekannt (von %s)", mail_from, rcpt, ip)
        return "550 5.1.1 Empfänger unbekannt"
    if q == INTERNET and _nur_intern(rcpt):
        _zaehlen("unbekannt")
        return "550 5.7.1 Nur für Absender aus der Organisation"
    if q == INTERNET and greylisting_an() and not _grau_durch(ip, mail_from, rcpt):
        _zaehlen("greylist")
        return "451 4.7.1 Bitte später erneut versuchen"
    return None


def data_pruefen(q: str, ip: str, empfaenger: list[str], groesse: int) -> str | None:
    """Antwort für DATA, oder None = annehmen. Für `microsoft`/`internet`."""
    import notbetrieb
    fremd = [e for e in empfaenger if not notbetrieb.bekannt(e)]
    if fremd:
        # Bei `microsoft` hier zum ersten Mal sicher: kein eigener Connector,
        # also gilt die Empfängerprüfung jetzt.
        _zaehlen("unbekannt")
        return "550 5.1.1 Empfänger unbekannt: " + ", ".join(fremd[:3])
    geschuetzt = [e for e in empfaenger if _nur_intern(e)]
    if geschuetzt:
        _zaehlen("unbekannt")
        return "550 5.7.1 Nur für Absender aus der Organisation: " + ", ".join(geschuetzt[:3])
    if len(empfaenger) > max_empfaenger():
        return "452 4.5.3 Zu viele Empfänger"
    if groesse > max_bytes():
        return "552 5.3.4 Nachricht zu groß"
    if q == INTERNET:
        n = netz(ip)
        jetzt = time.time()
        with _lock:
            liste = [t for t in _takt.get(n, []) if jetzt - t < 3600]
            gedrosselt = len(liste) >= je_stunde()
            if not gedrosselt:
                liste.append(jetzt)
                _takt[n] = liste
        # Zählen erst NACH der Sperre: _zaehlen nimmt dieselbe (nicht
        # wiedereintrittsfähige) Sperre — darin gerufen, stünde der Prozess.
        if gedrosselt:
            _zaehlen("gedrosselt")
            return "451 4.7.1 Zu viele Nachrichten — bitte später erneut"
    _zaehlen("angenommen")
    return None


# ── Greylisting ─────────────────────────────────────────────────────────────

def _grau_durch(ip: str, mail_from: str, rcpt: str, jetzt: float | None = None) -> bool:
    import notbetrieb
    jetzt = time.time() if jetzt is None else jetzt
    n = netz(ip)
    schluessel = f"{n}|{(mail_from or '').lower()}|{rcpt.lower()}"
    with notbetrieb._lock, notbetrieb._conn() as conn:
        _tabellen(conn)
        bekannt = conn.execute("SELECT bis FROM grau_netz WHERE netz=?", (n,)).fetchone()
        if bekannt and bekannt["bis"] > jetzt:
            return True
        z = conn.execute("SELECT erst FROM grau WHERE schluessel=?", (schluessel,)).fetchone()
        if z is None or jetzt - z["erst"] > GREY_FENSTER_S:
            conn.execute("INSERT OR REPLACE INTO grau (schluessel, erst) VALUES (?,?)", (schluessel, jetzt))
            return False
        if jetzt - z["erst"] < GREY_WARTEN_S:
            return False
        conn.execute("INSERT OR REPLACE INTO grau_netz (netz, bis) VALUES (?,?)",
                     (n, jetzt + GREY_NETZ_TAGE * 86400))
        conn.execute("DELETE FROM grau WHERE erst < ?", (jetzt - GREY_FENSTER_S,))
        return True


def _tabellen(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS grau (schluessel TEXT PRIMARY KEY, erst REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS grau_netz (netz TEXT PRIMARY KEY, bis REAL NOT NULL);
    """)


# ── Sichtbarkeit ────────────────────────────────────────────────────────────

_zaehler: dict[str, int] = {}


def _zaehlen(was: str) -> None:
    with _lock:
        _zaehler[was] = _zaehler.get(was, 0) + 1
    # Dauerhaft und je Tag für den Tagesbericht — außerhalb der Sperre.
    try:
        import stats
        stats.increment("notfall_" + was)
    except Exception:                                               # noqa: BLE001
        pass


def zaehler() -> dict:
    """Seit dem Start: angenommen, unbekannt, greylist, gedrosselt — die Null
    ist hier die Meldung (Bezugsgröße: angenommen)."""
    with _lock:
        return {k: _zaehler.get(k, 0) for k in ("angenommen", "unbekannt", "greylist", "gedrosselt",
                                                 "unecht", "junk")}
