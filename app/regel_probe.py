"""Regelprobe: Greift die Exchange-Transportregel schon für dieses Postfach?

WOFÜR
-----
Ein neues Mitglied der Verteilerliste erfasst die FromMemberOf-Regel erst nach
bis zu ~3 Stunden (gemessen 17.11.2026, siehe webui/routen/mailboxes.py
`REGEL_VERZOEGERUNG`). Ob es schon so weit ist, zeigt nur ein echter Durchlauf.
Diese Probe liefert ihn auf Knopfdruck — nie von selbst (kein Takt, kein
Aufruf aus der Gesundheitsprüfung).

ABLAUF
------
1. Das Gateway sendet per Graph `sendMail` (JSON, saveToSentItems=false) eine
   Mail ALS das Postfach AN das Postfach selbst. Sie trägt eine einmalige
   Kennung `<probe>.<geheimnis>` im Kopf `X-GW-Regelprobe` UND im Betreff.
   Kein Auto-Submitted (die Routing-Regeln nehmen solche Post aus) und kein
   X-Sig-Applied (sonst ginge sie gar nicht erst zum Gateway).
2. Greift die Regel, kommt die Mail am Gateway an. `erkennen()` (aus
   handler.handle_DATA, früh, vor jeder Zustellung) prüft die Kennung; ist sie
   gültig, verwirft der Handler die Mail mit 250 — keine Zustellung.
3. Greift sie nicht, stellt Exchange die Mail in den eigenen Posteingang zu.
   Nach `ANKUNFT_FRIST_S` sucht das Gateway sie dort (Graph, Betreff mit der
   Probe-Kennung) und löscht sie (DELETE, Recht Mail.ReadWrite — gehört zum
   Standardumfang der App, setup_wizard._GRAPH_PERMISSION_SPECS).

WARUM DIE KENNUNG AUCH IM BETREFF STEHT
---------------------------------------
Der Kopf allein ist nicht belegt: Gemessen (18.11.2026, Raspi) ist nur, dass
die selbstadressierte Mail am Gateway ankommt — die Kopfzeilen protokolliert
das Gateway erst auf Stufe DEBUG. Für Graph-sendMail aus geteilten Postfächern
sind verlorene X-Köpfe sogar gemessen (CLAUDE.md, „Rücklauf“). Ginge der Kopf
verloren, verarbeitete das Gateway die Probe wie jede Mail, stellte sie
signiert zu, und die Suche im Posteingang meldete fälschlich „greift nicht“.
Den Betreff verändert Exchange nicht.

SICHERHEIT
----------
Nur eine vom Gateway ausgestellte, noch offene, unverbrauchte Kennung führt
zum Verwerfen — und nur, wenn Umschlag-Absender UND einziger Empfänger das
Postfach sind, für das sie ausgestellt wurde. Bei Hybrid-Nutzern ist der
einzige zulässige Empfänger das Prüfpostfach: gebunden an die Adressen GENAU
dieses einen Postfachs (Absender: die des Hybrid-Nutzers), festgelegt beim
Ausstellen. Für EXO-Postfächer bleibt es bei der einen Adresse, an die die
Probe ging — Absender und Empfänger identisch. Das Geheimnis ist zufällig
(secrets, 192 Bit) und liegt nur als SHA-256 im Speicher; verglichen wird mit
hmac.compare_digest. Ein HMAC über einen Schlüssel brächte nichts hinzu: Es
gibt keinen Zustand ausserhalb dieses Prozesses, den er schützen müsste.
Unbekannte, abgelaufene, verbrauchte oder fremd gebundene Kennungen werden
protokolliert und IGNORIERT — die Mail läuft normal weiter. Sonst könnte jeder,
der den Kopf setzt, Post verschwinden lassen.

GRENZEN
-------
* Offene Kennungen liegen nur im Speicher. Startet das Gateway während einer
  Probe neu, verarbeitet es eine danach eintreffende Probe wie jede andere
  Mail (signiert zugestellt) — sichtbar, nicht verloren.
* Hybrid-Nutzer (seit v1.12.67): Eine Mail an sich selbst stellte die LOKALE
  Exchange direkt zu; sie erreichte Exchange Online und die Regel nie. Die
  Probe geht deshalb per EWS (`ews_onprem.regelprobe_senden`, CreateItem
  SendOnly mit Impersonation) aus dem lokalen Postfach an das
  Benachrichtigungs-Absenderpostfach (NOTIFICATION_MAILBOX, ein Postfach in
  Exchange Online): lokal → Hybrid-Connector → EXO → FromMemberOf-Regel →
  Gateway. Greift die Regel nicht, liegt sie in DIESEM Postfach und wird dort
  per Graph gesucht und gelöscht. Ohne Anbindung an Exchange lokal, ohne
  Prüfpostfach oder ohne wirksame Hybrid-Option: „entfällt“ mit Grund.
* Webserver (uvicorn) und SMTP-Listener laufen in verschiedenen Threads mit
  eigenen Event-Loops: Der Zustand ist deshalb mit threading.Lock geschützt,
  nicht mit asyncio-Mitteln.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import httpx

import settings_store

log = logging.getLogger("regel_probe")

KOPF = "X-GW-Regelprobe"
BETREFF = "GW-Regelprobe"

# Fristen. Gemessen 18.11.2026: Probe 6 s nach dem Senden am Gateway.
ANKUNFT_FRIST_S = 150     # so lange nur auf die Ankunft am Gateway warten
GESAMT_FRIST_S = 600      # danach zusätzlich im Posteingang suchen, bis hierher
TAKT_S = 3.0              # Abfrage der Ankunft (nur Speicher, kein Netz)
SUCHE_TAKT_S = 30.0       # Abstand der Posteingangssuchen (Graph)
NACHLAUF_S = 60           # Kennung bleibt so lange über GESAMT_FRIST_S gültig
SAMMEL_PARALLEL = 3       # Massenprüfung: so viele Proben gleichzeitig
SAMMEL_ABSTAND_S = 2.0    # Massenprüfung: Abstand zwischen zwei Starts
SAMMEL_MAX = 500

_KENNUNG = re.compile(r"([0-9a-f]{12})\.([A-Za-z0-9_-]{32})")

_lock = threading.Lock()
_offen: dict[str, dict] = {}       # Probe-Kennung → {adresse, geheim_sha, ablauf, ...}
_laufend: dict[str, float] = {}    # Adresse → Startzeit (time.time) — auch wartende
_lauf: dict = {"gesamt": 0, "fertig": 0, "ergebnisse": {}, "fehler": {}}
_aufgaben: set = set()             # Referenzen auf Hintergrund-Tasks (gegen GC)

GRAPH = "https://graph.microsoft.com/v1.0"


# ── Kennung: ausstellen, erkennen, schliessen ────────────────────────────────

def _sha(geheim: str) -> str:
    return hashlib.sha256(geheim.encode("ascii")).hexdigest()


def _aufraeumen(jetzt: float) -> None:
    for k in [k for k, e in _offen.items() if e["ablauf"] <= jetzt]:
        del _offen[k]


def ausstellen(adresse: str, absender=None, empfaenger=None) -> tuple[str, str]:
    """Neue Kennung für `adresse` → (probe_id, geheimnis).

    Bindung: Umschlag-Absender ∈ `absender`, einziger Empfänger ∈ `empfaenger`.
    Ohne Angabe beide = {adresse} — die Probe eines EXO-Postfachs an sich selbst
    (Stand bis v1.12.66, unverändert streng)."""
    probe_id = secrets.token_hex(6)
    geheim = secrets.token_urlsafe(24)          # 32 Zeichen, 192 Bit
    jetzt = time.monotonic()
    eigen = adresse.strip().lower()
    with _lock:
        _aufraeumen(jetzt)
        _offen[probe_id] = {
            "adresse": eigen,
            "absender": frozenset(a.strip().lower() for a in (absender or [eigen]) if a),
            "empfaenger": frozenset(a.strip().lower() for a in (empfaenger or [eigen]) if a),
            "geheim_sha": _sha(geheim),
            "gesendet": jetzt,
            "ablauf": jetzt + GESAMT_FRIST_S + NACHLAUF_S,
            "angekommen": None,
        }
    return probe_id, geheim


def schliessen(probe_id: str) -> None:
    with _lock:
        _offen.pop(probe_id, None)


def angekommen(probe_id: str) -> float | None:
    """Laufzeit in Sekunden, wenn die Probe am Gateway erkannt wurde."""
    with _lock:
        e = _offen.get(probe_id)
        return e["angekommen"] if e else None


def _kandidat(kopf: str, betreff: str) -> tuple[str, str] | None:
    """Kennung aus dem Kopf (genau die Kennung) oder dem Betreff („GW-Regelprobe
    <kennung>“, mit Präfixen wie „AW:“ davor) — Kopf zuerst."""
    m = _KENNUNG.fullmatch(kopf.strip()) if kopf.strip() else None
    if m is None and BETREFF in betreff:
        m = re.search(re.escape(BETREFF) + r" " + _KENNUNG.pattern, betreff)
    return (m.group(1), m.group(2)) if m else None


def erkennen(kopf, betreff: str, absender: str, empfaenger: list[str]) -> dict | None:
    """Ist diese Mail eine offene Probe des Gateways? Aus handle_DATA, für jede
    Mail vom Exchange-Weg — der schnelle Weg (kein Kopf, kein Probe-Betreff)
    kostet zwei Zeichenkettenvergleiche.

    Gültig → Kennung wird verbraucht, Rückgabe {adresse, laufzeit_s}; die Mail
    ist zu verwerfen. Sonst None — auch bei gefälschter oder abgelaufener
    Kennung (dann mit Protokollzeile): Die Mail läuft normal weiter."""
    kopf = str(kopf or "")
    betreff = betreff or ""
    if not kopf and BETREFF not in betreff:
        return None
    kand = _kandidat(kopf, betreff)
    if kand is None:
        if kopf:
            log.warning("Regelprobe: Kopf %s ohne gültige Form von %s — Mail wird "
                        "normal verarbeitet", KOPF, absender)
        return None
    probe_id, geheim = kand
    abs_ = (absender or "").strip().lower()
    rcpt = [(r or "").strip().lower() for r in (empfaenger or [])]
    jetzt = time.monotonic()
    with _lock:
        _aufraeumen(jetzt)
        e = _offen.get(probe_id)
        if e is None:
            grund = "unbekannt oder abgelaufen"
        elif e["angekommen"] is not None:
            grund = "schon verbraucht"
        elif not hmac.compare_digest(e["geheim_sha"], _sha(geheim)):
            grund = "Geheimnis passt nicht"
        elif abs_ not in e["absender"] or len(rcpt) != 1 or rcpt[0] not in e["empfaenger"]:
            grund = "an ein anderes Postfach gebunden"
        else:
            e["angekommen"] = round(jetzt - e["gesendet"], 1)
            return {"adresse": e["adresse"], "laufzeit_s": e["angekommen"],
                    "probe_id": probe_id}
    log.warning("Regelprobe: Kennung %s %s (von %s an %s) — Mail wird normal verarbeitet",
                probe_id, grund, absender, ", ".join(empfaenger or []))
    return None


# ── Graph ────────────────────────────────────────────────────────────────────

async def _token() -> str | None:
    import graph_client
    return await graph_client._acquire_token_async()


def _betreff(probe_id: str, geheim: str) -> str:
    return f"{BETREFF} {probe_id}.{geheim}"


_TEXT = ("Prüfmail des Signatur-Gateways: Greift die Transportregel für dieses Postfach? "
         "Wird automatisch entfernt.")


async def _senden(adresse: str, probe_id: str, geheim: str) -> str:
    """sendMail als `adresse` an `adresse`. "" bei Erfolg (202), sonst Fehlertext."""
    token = await _token()
    if not token:
        return "Kein Graph-Token"
    rumpf = {
        "message": {
            "subject": _betreff(probe_id, geheim),
            "body": {"contentType": "Text",
                     "content": _TEXT},
            "toRecipients": [{"emailAddress": {"address": adresse}}],
            "internetMessageHeaders": [{"name": KOPF, "value": f"{probe_id}.{geheim}"}],
        },
        "saveToSentItems": False,
    }
    url = f"{GRAPH}/users/{urllib.parse.quote(adresse)}/sendMail"
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(url, headers={"Authorization": f"Bearer {token}"}, json=rumpf)
    except httpx.HTTPError as exc:
        return f"Graph nicht erreichbar ({type(exc).__name__})"
    if r.status_code == 202:
        return ""
    return f"sendMail HTTP {r.status_code}: {r.text[:200]}"


async def _senden_ews(adresse: str, postfach: str, probe_id: str, geheim: str) -> str:
    """Hybrid-Nutzer: aus dem LOKALEN Postfach `adresse` per EWS an das
    Prüfpostfach `postfach` (Exchange Online). "" bei Erfolg, sonst Fehlertext."""
    import ews_onprem
    try:
        await asyncio.to_thread(ews_onprem.regelprobe_senden, adresse, postfach,
                                _betreff(probe_id, geheim), KOPF, f"{probe_id}.{geheim}", _TEXT)
    except ews_onprem.EwsFehler as exc:
        return f"EWS ({exc.code}): {exc.detail}"[:240]
    return ""


async def _im_posteingang(adresse: str, probe_id: str, ab: datetime) -> dict | None:
    """Probe im Posteingang suchen → {id, ueber_gateway} oder None.
    `ueber_gateway`: Die Kopie trägt X-Sig-Applied, lief also durch ein Gateway
    (Probe dort nicht erkannt) — dann ist „greift nicht“ gerade NICHT belegt."""
    token = await _token()
    if not token:
        raise RuntimeError("Kein Graph-Token")
    auth = {"Authorization": f"Bearer {token}"}
    filt = urllib.parse.quote(f"receivedDateTime ge {ab.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    basis = f"{GRAPH}/users/{urllib.parse.quote(adresse)}"
    url = (f"{basis}/mailFolders/inbox/messages?$filter={filt}"
           "&$select=id,subject&$top=50")
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(url, headers=auth)
        if r.status_code != 200:
            raise RuntimeError(f"Posteingang HTTP {r.status_code}")
        treffer = [m for m in r.json().get("value", [])
                   if f"{BETREFF} {probe_id}." in (m.get("subject") or "")]
        if not treffer:
            return None
        mid = treffer[0]["id"]
        r2 = await c.get(f"{basis}/messages/{mid}?$select=internetMessageHeaders", headers=auth)
        koepfe = r2.json().get("internetMessageHeaders", []) if r2.status_code == 200 else []
    ueber = any((h.get("name") or "").lower() == "x-sig-applied" for h in koepfe)
    return {"id": mid, "ueber_gateway": ueber}


async def _loeschen(adresse: str, nachricht_id: str) -> str:
    """DELETE der Probe. "" bei Erfolg, sonst Fehlertext."""
    token = await _token()
    if not token:
        return "Kein Graph-Token"
    url = (f"{GRAPH}/users/{urllib.parse.quote(adresse)}/messages/"
           f"{urllib.parse.quote(nachricht_id, safe='')}")
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.delete(url, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        return f"Graph nicht erreichbar ({type(exc).__name__})"
    return "" if r.status_code in (200, 204) else f"HTTP {r.status_code}"


# ── Ergebnis ─────────────────────────────────────────────────────────────────

def _jetzt_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _regel_wartet(cfg: dict) -> bool:
    from webui.routen.mailboxes import regel_wartet
    return regel_wartet(cfg)


def _cfg(adresse: str) -> dict:
    import mailbox_match
    return mailbox_match.match_sender(settings_store.get("MAILBOX_CONFIG") or {}, adresse)


def _ergebnis(art: str, status: str, detail: str, laufzeit: float | None = None) -> dict:
    return {"ergebnis": art, "status": status, "detail": detail,
            "checked_at": _jetzt_str(), "laufzeit_s": laufzeit}


def als_pruefung(probe: dict) -> dict:
    """Zeile `regel_greift` in MAILBOX_HEALTH[…]["checks"] (Form von health_check._make_result)."""
    return {"status": probe.get("status", "skip"), "checked_at": probe.get("checked_at", ""),
            "detail": probe.get("detail", "")}


def aktuell(probe: dict | None, cfg: dict) -> bool:
    """Gilt ein gespeichertes Probe-Ergebnis noch? Nur für ein aktives Postfach,
    und nur, wenn die Probe NACH der letzten Aktivierung lief — ein „greift“
    von vor einer Deaktivierung sagt über die neue Mitgliedschaft nichts."""
    if not probe or not isinstance(cfg, dict) or not (cfg.get("sig") or cfg.get("smime")):
        return False
    seit = cfg.get("aktiviert_seit")
    if not seit:
        return True
    try:
        t_seit = datetime.fromisoformat(str(seit).replace("Z", "+00:00"))
        t_probe = datetime.fromisoformat(str(probe.get("checked_at", "")).replace("Z", "+00:00"))
    except ValueError:
        return False
    if t_seit.tzinfo is None:
        t_seit = t_seit.replace(tzinfo=timezone.utc)
    if t_probe.tzinfo is None:
        t_probe = t_probe.replace(tzinfo=timezone.utc)
    return t_probe >= t_seit


def greift(gesundheit: dict | None, cfg: dict) -> bool:
    """Belegt die letzte gültige Probe, dass die Regel greift?"""
    probe = (gesundheit or {}).get("regel_probe")
    return aktuell(probe, cfg) and probe.get("ergebnis") == "greift"


def speichern(adresse: str, probe: dict) -> None:
    """Ergebnis in MAILBOX_HEALTH[adresse] ablegen: `regel_probe` (roh) und die
    Prüfzeile `regel_greift`; der Gesamtstatus wird neu berechnet."""
    import health_check
    with settings_store._lock:          # RLock — Lesen und Schreiben als ein Schritt
        alle = dict(settings_store.get("MAILBOX_HEALTH") or {})
        eintrag = dict(alle.get(adresse) or {})
        checks = dict(eintrag.get("checks") or {})
        checks["regel_greift"] = als_pruefung(probe)
        eintrag["checks"] = checks
        eintrag["regel_probe"] = probe
        eintrag["overall"] = health_check._compute_overall(checks)
        alle[adresse] = eintrag
        settings_store.force_update({"MAILBOX_HEALTH": alle})


HYBRID_INAKTIV = ("Hybrid-Nutzer: Die Hybrid-Option wirkt nicht (ausgeschaltet oder Rückweg "
                  "nicht SMTP Port 25) — das Postfach steht nicht in der Verteilerliste.")
HYBRID_OHNE_EWS = ("Hybrid-Nutzer: Die Probe geht aus dem lokalen Postfach über Exchange lokal "
                   "(EWS) — die Anbindung ist nicht eingerichtet (Einstellungen → Allgemein → "
                   "Exchange lokal). Eine Mail an sich selbst bliebe in der lokalen Organisation "
                   "und erreichte die Regel nie.")
HYBRID_OHNE_POSTFACH = ("Hybrid-Nutzer: Die Probe braucht als Empfänger ein Postfach in Exchange "
                        "Online — das Absender-Postfach der Benachrichtigungen ist nicht gesetzt "
                        "oder kein bekanntes Postfach in Exchange Online (Einstellungen → "
                        "Benachrichtigungen → Absender; Postfachliste laden).")
# „entfällt“: Ergebnis wird gespeichert (skip), keine Mail. Alles andere aus
# nicht_moeglich ist eine Ablehnung (nichts gespeichert).
ENTFAELLT = (HYBRID_INAKTIV, HYBRID_OHNE_EWS, HYBRID_OHNE_POSTFACH)


def entfaellt(grund: str) -> bool:
    return grund in ENTFAELLT


def pruefpostfach() -> str:
    """Empfänger der Probe eines Hybrid-Nutzers: das Absender-Postfach der
    Benachrichtigungen, sofern es ein bekanntes EXO-Postfach ist (Graph liest
    und löscht dort). "" = keines."""
    import exo_mailboxes
    pf = (settings_store.get("NOTIFICATION_MAILBOX") or "").strip().lower()
    if not pf or not exo_mailboxes.adressen_von(pf, hybrid=False):
        return ""
    return pf


def _plan(adresse: str) -> tuple[str, dict | None]:
    """(Grund, Plan). Grund "" → Plan {hybrid, postfach, absender, empfaenger}:
    wohin die Probe geht (`postfach`, dort wird gesucht und gelöscht) und woran
    die Kennung gebunden wird."""
    cfg = _cfg(adresse)
    if not (cfg.get("sig") or cfg.get("smime")):
        return ("Postfach nicht aktiviert (Signatur und S/MIME aus) — die Regel gilt nur "
                "für Mitglieder der Verteilerliste"), None
    import exo_mailboxes
    if not exo_mailboxes.ist_hybrid(adresse):
        return "", {"hybrid": False, "postfach": adresse, "absender": [adresse],
                    "empfaenger": [adresse]}
    import ews_onprem
    if not exo_mailboxes.hybrid_aktiv():
        return HYBRID_INAKTIV, None
    if not ews_onprem.konfiguriert():
        return HYBRID_OHNE_EWS, None
    pf = pruefpostfach()
    if not pf:
        return HYBRID_OHNE_POSTFACH, None
    absender = set(exo_mailboxes.adressen_von(adresse, hybrid=True)) | {adresse.lower()}
    return "", {"hybrid": True, "postfach": pf, "absender": sorted(absender),
                "empfaenger": exo_mailboxes.adressen_von(pf, hybrid=False)}


def nicht_moeglich(adresse: str) -> str:
    """Grund, warum für `adresse` keine Probe laufen kann — oder ""."""
    return _plan(adresse)[0]


# ── Ablauf ───────────────────────────────────────────────────────────────────

async def _durchlauf(adresse: str, plan: dict | None = None) -> dict:
    """Eine Probe von Senden bis Ergebnis. Schliesst die Kennung immer.
    Ohne `plan`: EXO-Postfach an sich selbst (Graph)."""
    plan = plan or {"hybrid": False, "postfach": adresse}
    postfach = plan["postfach"]
    probe_id, geheim = ausstellen(adresse, plan.get("absender"), plan.get("empfaenger"))
    ab = datetime.now(timezone.utc) - timedelta(minutes=1)
    t0 = time.monotonic()
    try:
        if plan["hybrid"]:
            fehler = await _senden_ews(adresse, postfach, probe_id, geheim)
        else:
            fehler = await _senden(adresse, probe_id, geheim)
        if fehler:
            log.warning("Regelprobe %s für %s: nicht gesendet — %s", probe_id, adresse, fehler)
            return _ergebnis("fehler", "warn", f"Probe nicht gesendet: {fehler}")
        log.info("Regelprobe %s für %s: gesendet%s", probe_id, adresse,
                 f" (über Exchange lokal an {postfach})" if plan["hybrid"] else "")
        naechste_suche = t0 + ANKUNFT_FRIST_S
        suchfehler = ""
        while True:
            laufzeit = angekommen(probe_id)
            if laufzeit is not None:
                log.info("Regelprobe %s für %s: greift (%.1f s)", probe_id, adresse, laufzeit)
                return _ergebnis("greift", "ok",
                                 f"Regel greift — Probe nach {laufzeit:.0f} s am Gateway",
                                 laufzeit)
            jetzt = time.monotonic()
            if jetzt - t0 >= GESAMT_FRIST_S:
                break
            if jetzt >= naechste_suche:
                naechste_suche = jetzt + SUCHE_TAKT_S
                try:
                    fund = await _im_posteingang(postfach, probe_id, ab)
                except Exception as exc:                        # noqa: BLE001
                    suchfehler = str(exc)[:160]
                    log.warning("Regelprobe %s für %s: Suche im Posteingang gescheitert — %s",
                                probe_id, adresse, suchfehler)
                    fund = None
                if fund is not None:
                    return await _nicht_gegriffen(adresse, probe_id, fund, postfach)
            await asyncio.sleep(TAKT_S)
        log.warning("Regelprobe %s für %s: weder am Gateway noch im Posteingang (%d s)",
                    probe_id, adresse, GESAMT_FRIST_S)
        detail = (f"Probe nach {GESAMT_FRIST_S // 60} min weder am Gateway noch im Posteingang "
                  "— später erneut prüfen")
        if suchfehler:
            detail += f" (Suche im Posteingang: {suchfehler})"
        return _ergebnis("unklar", "warn", detail)
    finally:
        schliessen(probe_id)


async def _nicht_gegriffen(adresse: str, probe_id: str, fund: dict,
                           postfach: str | None = None) -> dict:
    postfach = postfach or adresse
    fehler = await _loeschen(postfach, fund["id"])
    wo = "Posteingang" if postfach == adresse else f"Posteingang von {postfach}"
    weg = (f"aus dem {wo} gelöscht" if not fehler
           else f"Löschen gescheitert ({fehler}) — die Probe liegt im {wo}")
    log.info("Regelprobe %s für %s: im Posteingang (%s)", probe_id, adresse,
             "gelöscht" if not fehler else f"Löschen gescheitert: {fehler}")
    if fund.get("ueber_gateway"):
        return _ergebnis("unklar", "warn",
                         "Probe lief über ein Gateway (X-Sig-Applied), wurde dort aber nicht "
                         f"als Probe erkannt — {weg}. Regel greift vermutlich; erneut prüfen.")
    if _regel_wartet(_cfg(adresse)):
        return _ergebnis("wartet", "info",
                         f"Regel greift noch nicht — Probe direkt zugestellt, {weg}. Exchange "
                         "übernimmt neue Mitglieder der Verteilerliste erst nach bis zu 3 h.")
    return _ergebnis("greift_nicht", "error",
                     f"Regel greift nicht — Probe direkt zugestellt, {weg}. Mitgliedschaft in "
                     "der Verteilerliste und Transportregel prüfen.")


async def pruefen(adresse: str) -> dict:
    """Eine Probe für `adresse`, Ergebnis gespeichert. Der Aufrufer hat die
    Adresse in `_laufend` eingetragen (`anmelden`); hier wird sie ausgetragen."""
    try:
        grund, plan = await asyncio.to_thread(_plan, adresse)
        if entfaellt(grund):
            probe = _ergebnis("nicht_moeglich", "skip", grund)
        elif grund:
            return {"ergebnis": "abgelehnt", "detail": grund}
        else:
            try:
                probe = await _durchlauf(adresse, plan)
            except Exception as exc:                            # noqa: BLE001
                log.exception("Regelprobe für %s: unerwarteter Fehler", adresse)
                probe = _ergebnis("fehler", "warn", f"Probe abgebrochen: {str(exc)[:160]}")
        await asyncio.to_thread(speichern, adresse, probe)
        return probe
    finally:
        with _lock:
            _laufend.pop(adresse, None)


def anmelden(adressen: list[str]) -> tuple[list[str], list[str]]:
    """Adressen für einen Lauf vormerken → (angenommen, laufen_schon)."""
    neu, schon = [], []
    with _lock:
        for a in adressen:
            if a in _laufend:
                schon.append(a)
            else:
                _laufend[a] = time.time()
                neu.append(a)
    return neu, schon


async def sammeln(adressen: list[str]) -> None:
    """Proben für mehrere Postfächer: höchstens SAMMEL_PARALLEL gleichzeitig,
    Starts im Abstand SAMMEL_ABSTAND_S (Graph-Drosselung). Ein Fehler bei einem
    Postfach bricht den Lauf nicht ab; er steht je Adresse in `_lauf`."""
    sperre = asyncio.Semaphore(SAMMEL_PARALLEL)
    with _lock:
        if _lauf["fertig"] >= _lauf["gesamt"]:
            _lauf.update({"gesamt": 0, "fertig": 0, "ergebnisse": {}, "fehler": {}})
        _lauf["gesamt"] += len(adressen)

    async def _eine(a: str) -> None:
        async with sperre:
            try:
                erg = await pruefen(a)
            except Exception as exc:                            # noqa: BLE001
                erg = {"ergebnis": "fehler", "detail": str(exc)[:160]}
            with _lock:
                _lauf["fertig"] += 1
                art = erg.get("ergebnis", "fehler")
                _lauf["ergebnisse"][art] = _lauf["ergebnisse"].get(art, 0) + 1
                if art in ("fehler", "abgelehnt", "unklar"):
                    _lauf["fehler"][a] = erg.get("detail", "")
            await asyncio.sleep(SAMMEL_ABSTAND_S)

    await asyncio.gather(*(_eine(a) for a in adressen))


def starten(adressen: list[str]) -> dict:
    """Aus der Route (im Event-Loop des Webservers): Proben im Hintergrund
    starten. Sofortige Antwort; Fortschritt über `stand()`."""
    neu, schon = anmelden(adressen)
    if neu:
        aufgabe = asyncio.get_running_loop().create_task(sammeln(neu))
        _aufgaben.add(aufgabe)
        aufgabe.add_done_callback(_aufgaben.discard)
    return {"gestartet": neu, "laufen_schon": schon}


def stand() -> dict:
    with _lock:
        return {"laufend": sorted(_laufend), "gesamt": _lauf["gesamt"],
                "fertig": _lauf["fertig"], "ergebnisse": dict(_lauf["ergebnisse"]),
                "fehler": dict(_lauf["fehler"])}
