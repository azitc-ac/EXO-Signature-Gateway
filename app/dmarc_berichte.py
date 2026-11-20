"""DMARC-Sammelberichte (RUA) aus einem Postfach oder einer Gruppe lesen und auswerten.

WARUM
-----
Ob die eigene Post SPF/DKIM/DMARC besteht, sieht der Absender nicht — nur die
Empfänger. Deren Sammelberichte (RFC 7489, Abschnitt 7.2) landen an der
`rua=`-Adresse des DMARC-Eintrags und bleiben dort meist ungelesen. Ein
gebrochener Rückweg (etwa eine veraltete DKIM-Signatur, die mitreist) zeigt sich
dann erst, wenn Post im Junk landet.

WIE
---
* Hintergrundlauf (scheduler, alle TAKT_H Stunden), NICHT im Mailpfad.
* Ziel = DMARC_POSTFACH. Was es ist, löst `ziel_aufloesen` per Graph auf:
  - Benutzer-/geteiltes Postfach → `/users/{adresse}/messages` (Mail.ReadWrite der
    Gateway-App genügt; keine neue Berechtigung).
  - Microsoft-365-Gruppe → `/groups/{id}/threads/…/posts`. Der Postfachweg liefert
    dort `ErrorInvalidUser` (gemessen 20.11.2026). Braucht
    `Group-Conversation.Read.All` (Application, laut Graph-Doku das schmalste
    Recht dafür; nicht über RBAC for Applications eingrenzbar). Die Rolle wird
    nur verlangt, wenn eine Gruppe eingetragen ist (`_DMARC_GRUPPE`,
    setup_wizard._GRAPH_PERMISSION_SPECS „nur_wenn“).
* Nur lesend: Berichte werden weder gelöscht noch als gelesen markiert.
* Doppelte Berichte (dieselbe Mail zweimal gelesen, Überlappung des Zeitfensters)
  fallen über die Kennung Absender-Organisation + Report-ID heraus.

FREMDDATEN
----------
Berichte kann jeder schicken. Darum:
* Anhang höchstens MAX_ANHANG Byte (vor dem Herunterladen nach `size` geprüft),
  entpackt höchstens MAX_ENTPACKT, im ZIP höchstens MAX_ZIP_TEILE Dateien.
  Die Grenze gilt für die tatsächlich entpackten Bytes, nicht für die Angabe im
  ZIP-Verzeichnis (die kann lügen).
* XML mit DOCTYPE/ENTITY wird verworfen (Entitäten-Expansion, externe Bezüge).
* Zahlen und Adressen werden geprüft und begrenzt; die Oberfläche maskiert alles.

BEFUNDE
-------
Je (Domäne, Quell-IP) über die Berichte der letzten AUSWERTUNG_TAGE:
* hoch   „fail_zugestellt“ — DMARC nicht bestanden und trotzdem zugestellt
         (Richtlinie p=none bzw. Abweichung des Empfängers).
* mittel „teilweise“       — dieselbe Quelle besteht mal, mal nicht (eigener
         Versender mit Lücken: zweiter Ausgang, Weiterleitung, Fremdsignatur).
* mittel „nur_spf“         — DMARC hängt allein an SPF; DKIM fehlt oder ist nicht
         ausgerichtet. Bricht, sobald weitergeleitet wird.
* mittel „dkim_zusatz_kaputt“ — neben einer gültigen DKIM-Signatur der Domäne
         reist eine ungültige mit (Gateway-Rückweg ohne Bereinigung, v1.12.137).
* mittel „richtlinie_none“ — p=none: Fälschungen werden zugestellt.
* info   „abgewiesen“      — nur Fehlschläge, vom Empfänger abgewiesen/isoliert:
         die Richtlinie wirkt.
* info   „pct“             — Richtlinie gilt nur für einen Teil (pct < 100).
Neue Befunde der Stufen hoch/mittel gehen einmal per Mail an die Verwaltung
(NOTIFY_DMARC); der Tagesbericht zeigt die Zahl immer, sobald ein Ziel
eingetragen ist.
"""
from __future__ import annotations

import base64
import gzip
import io
import ipaddress
import json
import logging
import re
import socket
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx

import config
import settings_store

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
ZIEL_KEY = "_DMARC_ZIEL"          # {adresse, art, id, ts, meldung}
GRUPPE_KEY = "_DMARC_GRUPPE"      # bool — steuert, ob Group-Conversation.Read.All verlangt wird
GRUPPEN_ROLLE = "Group-Conversation.Read.All"

TAKT_H = 6
ERSTER_ABRUF_TAGE = 30            # beim ersten Lauf so weit zurück
UEBERLAPPUNG_H = 24               # Fenster überlappt, Doppelte fallen über die Kennung heraus
AUFBEWAHRUNG_TAGE = 90
AUSWERTUNG_TAGE = 30
MAX_BERICHTE = 3000
MAX_ZEILEN_JE_BERICHT = 5000
MAX_ANHANG = 10 * 1024 * 1024
MAX_ENTPACKT = 50 * 1024 * 1024
MAX_ZIP_TEILE = 5
MAX_SEITEN = 40                   # Graph-Seiten je Lauf (je 50 Einträge)
MAX_ANZAHL = 10_000_000           # Plausibilitätsgrenze je Zeile

STUFEN = ("hoch", "mittel", "info")

_lock = threading.Lock()
_rdns_cache: dict[str, tuple[float, str]] = {}


# ── Einstellung und Ziel ─────────────────────────────────────────────────────

def adresse() -> str:
    return str(settings_store.get("DMARC_POSTFACH") or "").strip().lower()


def ziel() -> dict:
    """Zuletzt aufgelöstes Ziel — nur gültig, wenn es zur eingetragenen Adresse passt."""
    z = dict(settings_store.get(ZIEL_KEY) or {})
    return z if z.get("adresse") == adresse() and adresse() else {}


def _get(client, url: str, token: str, params: dict | None = None):
    return client.get(url, headers={"Authorization": f"Bearer {token}"}, params=params)


def ziel_aufloesen(client=None, token: str | None = None) -> dict:
    """Ist DMARC_POSTFACH ein Postfach oder eine Microsoft-365-Gruppe?

    Postfach zuerst (`/users/{adresse}`); 404 → Gruppe über `mail` oder eine
    Proxy-Adresse. Merkt das Ergebnis samt `_DMARC_GRUPPE` (Rollen-Soll)."""
    import graph_client
    adr = adresse()
    if not adr:
        return {}
    token = token or graph_client._acquire_token()
    if not token:
        return {"adresse": adr, "art": "unbekannt", "meldung": "Kein Graph-Token (Anbindung fehlt)."}
    eigen = client is None
    client = client or httpx.Client(timeout=30)
    try:
        z = {"adresse": adr, "art": "unbekannt", "id": "", "meldung": "",
             "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        r = _get(client, f"{GRAPH}/users/{adr}", token, {"$select": "id"})
        if r.status_code == 200:
            z.update(art="postfach", id=r.json().get("id", ""))
        elif r.status_code == 404:
            q = adr.replace("'", "''")
            g = _get(client, f"{GRAPH}/groups", token, {
                "$filter": f"mail eq '{q}' or proxyAddresses/any(x:x eq 'smtp:{q}')",
                "$select": "id,mail,groupTypes"})
            if g.status_code != 200:
                g = _get(client, f"{GRAPH}/groups", token,
                         {"$filter": f"mail eq '{q}'", "$select": "id,mail,groupTypes"})
            treffer = (g.json().get("value") or []) if g.status_code == 200 else []
            if treffer and "Unified" in (treffer[0].get("groupTypes") or []):
                z.update(art="gruppe", id=treffer[0]["id"])
            elif treffer:
                z["meldung"] = ("Die Adresse gehört einer Verteiler- oder Sicherheitsgruppe ohne "
                                "eigenes Postfach. Berichte lassen sich nur aus einem Postfach "
                                "oder einer Microsoft-365-Gruppe lesen.")
            else:
                z["meldung"] = "Weder Postfach noch Microsoft-365-Gruppe mit dieser Adresse gefunden."
        else:
            z["meldung"] = f"Graph antwortete mit HTTP {r.status_code}."
    finally:
        if eigen:
            client.close()
    alt_gruppe = settings_store.get(GRUPPE_KEY) is True
    settings_store.force_update({ZIEL_KEY: z, GRUPPE_KEY: z["art"] == "gruppe"})
    if alt_gruppe != (z["art"] == "gruppe"):
        log.info("DMARC: Ziel %s ist %s — %s wird %s", adr, z["art"], GRUPPEN_ROLLE,
                 "benötigt" if z["art"] == "gruppe" else "nicht benötigt")
    return z


# ── Ablage ───────────────────────────────────────────────────────────────────

def _datei() -> Path:
    return Path(config.DATA_DIR) / "dmarc" / "berichte.json"


def _laden() -> dict:
    try:
        d = json.loads(_datei().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _speichern(d: dict) -> None:
    import secure_io
    secure_io.ensure_dir(_datei().parent)
    secure_io.write_secret_json(_datei(), d, indent=None)


def stand() -> dict:
    with _lock:
        return _laden()


def _aufraeumen(berichte: dict) -> dict:
    grenze = time.time() - AUFBEWAHRUNG_TAGE * 86400
    behalten = {k: b for k, b in berichte.items() if (b.get("ende") or 0) >= grenze}
    if len(behalten) > MAX_BERICHTE:
        neueste = sorted(behalten.items(), key=lambda kv: kv[1].get("ende") or 0)[-MAX_BERICHTE:]
        behalten = dict(neueste)
    return behalten


# ── Entpacken und Lesen (Fremddaten!) ────────────────────────────────────────

class BerichtFehler(ValueError):
    """Ein Anhang ist kein lesbarer DMARC-Bericht (oder verletzt eine Grenze)."""


def _begrenzt_lesen(datei, grenze: int) -> bytes:
    daten = datei.read(grenze + 1)
    if len(daten) > grenze:
        raise BerichtFehler(f"entpackt größer als {grenze // (1024 * 1024)} MB")
    return daten


def entpacken(daten: bytes) -> list[bytes]:
    """XML-Dokumente aus einem Anhang: roh, gzip oder zip. Wirft BerichtFehler."""
    if len(daten) > MAX_ANHANG:
        raise BerichtFehler("Anhang zu groß")
    if daten[:2] == b"\x1f\x8b":
        with gzip.GzipFile(fileobj=io.BytesIO(daten)) as gz:
            try:
                return [_begrenzt_lesen(gz, MAX_ENTPACKT)]
            except (OSError, EOFError) as exc:
                raise BerichtFehler(f"gzip defekt: {exc}") from exc
    if daten[:4] == b"PK\x03\x04":
        try:
            zf = zipfile.ZipFile(io.BytesIO(daten))
        except zipfile.BadZipFile as exc:
            raise BerichtFehler(f"zip defekt: {exc}") from exc
        teile = [i for i in zf.infolist() if not i.is_dir()]
        if len(teile) > MAX_ZIP_TEILE:
            raise BerichtFehler(f"zip mit {len(teile)} Dateien")
        aus, summe = [], 0
        for info in teile:
            if not info.filename.lower().endswith(".xml"):
                continue
            try:
                with zf.open(info) as f:
                    stueck = _begrenzt_lesen(f, MAX_ENTPACKT - summe)
            except (zipfile.BadZipFile, OSError, RuntimeError) as exc:   # RuntimeError: verschlüsselt
                raise BerichtFehler(f"zip-Eintrag nicht lesbar: {exc}") from exc
            summe += len(stueck)
            aus.append(stueck)
        if not aus:
            raise BerichtFehler("zip ohne XML-Datei")
        return aus
    if daten.lstrip()[:1] == b"<" or daten[:3] == b"\xef\xbb\xbf":
        return [daten]
    raise BerichtFehler("weder XML noch gzip noch zip")


_VERBOTEN = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


def _text(el, pfad: str, n: int = 255) -> str:
    x = el.find(pfad) if el is not None else None
    return (x.text or "").strip()[:n] if x is not None and x.text else ""


def _zahl(text: str, grenze: int = MAX_ANZAHL) -> int:
    try:
        return max(0, min(int(str(text).strip()), grenze))
    except (TypeError, ValueError):
        return 0


_DOMAENE = re.compile(r"^[a-z0-9._-]{1,253}$")


def _domaene(text: str) -> str:
    d = (text or "").strip().lower().rstrip(".")
    return d if _DOMAENE.match(d) else ""


def bericht_lesen(xml: bytes) -> dict:
    """Ein Sammelbericht (RFC 7489, Anhang C) als dict. Wirft BerichtFehler."""
    if _VERBOTEN.search(xml):
        raise BerichtFehler("XML mit DOCTYPE/ENTITY verworfen")
    try:
        wurzel = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise BerichtFehler(f"kein gültiges XML: {exc}") from exc
    for el in wurzel.iter():                       # Namensräume (DMARCbis) abstreifen
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    if wurzel.tag != "feedback":
        raise BerichtFehler("kein DMARC-Sammelbericht (Wurzel ist nicht <feedback>)")
    meta, pol = wurzel.find("report_metadata"), wurzel.find("policy_published")
    org, rid = _text(meta, "org_name", 120), _text(meta, "report_id", 200)
    domain = _domaene(_text(pol, "domain"))
    if not (org and rid and domain):
        raise BerichtFehler("Bericht ohne Absender, Kennung oder Domäne")
    pct = _text(pol, "pct", 5)
    bericht = {
        "org": org, "report_id": rid, "domain": domain,
        "beginn": _zahl(_text(meta, "date_range/begin", 12), 2**40),
        "ende": _zahl(_text(meta, "date_range/end", 12), 2**40),
        "richtlinie": {"p": _text(pol, "p", 12).lower(), "sp": _text(pol, "sp", 12).lower(),
                       "pct": _zahl(pct, 100) if pct else 100,
                       "adkim": _text(pol, "adkim", 2).lower(), "aspf": _text(pol, "aspf", 2).lower()},
        "zeilen": [],
    }
    for rec in wurzel.findall("record")[:MAX_ZEILEN_JE_BERICHT]:
        ip = _text(rec, "row/source_ip", 45)
        try:
            ip = str(ipaddress.ip_address(ip))
        except ValueError:
            continue
        auth = rec.find("auth_results")
        bericht["zeilen"].append({
            "ip": ip,
            "anzahl": _zahl(_text(rec, "row/count", 12)),
            "disposition": _text(rec, "row/policy_evaluated/disposition", 20).lower(),
            "dkim": _text(rec, "row/policy_evaluated/dkim", 10).lower(),
            "spf": _text(rec, "row/policy_evaluated/spf", 10).lower(),
            "header_from": _domaene(_text(rec, "identifiers/header_from")) or domain,
            "dkim_auth": [{"d": _domaene(_text(d, "domain")), "s": _text(d, "selector", 63),
                           "r": _text(d, "result", 20).lower()}
                          for d in (auth.findall("dkim") if auth is not None else [])][:10],
            "spf_auth": [{"d": _domaene(_text(s, "domain")), "r": _text(s, "result", 20).lower()}
                         for s in (auth.findall("spf") if auth is not None else [])][:5],
        })
    return bericht


def _kennung(b: dict) -> str:
    return f"{b['org'].lower()}|{b['report_id']}"


def anhang_verarbeiten(daten: bytes, berichte: dict) -> tuple[int, int]:
    """(neu, doppelt) — Berichte aus einem Anhang in `berichte` übernehmen."""
    neu = doppelt = 0
    for xml in entpacken(daten):
        b = bericht_lesen(xml)
        k = _kennung(b)
        if k in berichte:
            doppelt += 1
            continue
        b["eingelesen"] = int(time.time())
        berichte[k] = b
        neu += 1
    return neu, doppelt


# ── Abruf per Graph ──────────────────────────────────────────────────────────

def _seiten(client, url: str, token: str, params: dict | None):
    n = 0
    while url and n < MAX_SEITEN:
        r = _get(client, url, token, params)
        if r.status_code != 200:
            raise _GraphFehler(r.status_code, r.text[:300])
        d = r.json()
        yield from d.get("value") or []
        url, params, n = d.get("@odata.nextLink"), None, n + 1


class _GraphFehler(RuntimeError):
    def __init__(self, status: int, text: str):
        super().__init__(f"HTTP {status}: {text}")
        self.status = status


def _anhaenge(client, basis: str, token: str) -> list[tuple[str, bytes]]:
    """[(name, bytes)] der Dateianhänge unter `basis` (…/messages/{id} oder …/posts/{id}).
    Größe vor dem Herunterladen geprüft."""
    aus = []
    for a in _seiten(client, f"{basis}/attachments", token,
                     {"$select": "id,name,size,contentType"}):
        if a.get("@odata.type", "#microsoft.graph.fileAttachment") != "#microsoft.graph.fileAttachment":
            continue
        if (a.get("size") or 0) > MAX_ANHANG + 64 * 1024:
            aus.append((a.get("name") or "?", b""))       # zu groß: als Fehler zählen
            continue
        r = _get(client, f"{basis}/attachments/{a['id']}", token)
        if r.status_code == 200 and r.json().get("contentBytes"):
            aus.append((a.get("name") or "?", base64.b64decode(r.json()["contentBytes"])))
    return aus


def _quellen(client, z: dict, token: str, seit: datetime):
    """Je Bericht-Mail ein Basis-Pfad für die Anhänge — Postfach oder Gruppe."""
    seit_iso = seit.strftime("%Y-%m-%dT%H:%M:%SZ")
    if z["art"] == "postfach":
        for m in _seiten(client, f"{GRAPH}/users/{z['id']}/messages", token, {
                "$filter": f"receivedDateTime ge {seit_iso} and hasAttachments eq true",
                "$select": "id,subject,receivedDateTime", "$top": "50",
                "$orderby": "receivedDateTime desc"}):
            yield m.get("subject") or "", f"{GRAPH}/users/{z['id']}/messages/{m['id']}"
        return
    gid = z["id"]
    try:
        threads = list(_seiten(client, f"{GRAPH}/groups/{gid}/threads", token, {
            "$filter": f"lastDeliveredDateTime gt {seit_iso}",
            "$select": "id,topic,lastDeliveredDateTime,hasAttachments", "$top": "50"}))
    except _GraphFehler as exc:
        if exc.status != 400:
            raise
        # Filter nicht angenommen → ohne Filter, Zeit selbst prüfen.
        threads = list(_seiten(client, f"{GRAPH}/groups/{gid}/threads", token,
                               {"$select": "id,topic,lastDeliveredDateTime,hasAttachments", "$top": "50"}))
    for t in threads:
        if (t.get("lastDeliveredDateTime") or "") < seit_iso or t.get("hasAttachments") is False:
            continue
        for p in _seiten(client, f"{GRAPH}/groups/{gid}/threads/{t['id']}/posts", token,
                         {"$select": "id,receivedDateTime,hasAttachments"}):
            if p.get("hasAttachments") and (p.get("receivedDateTime") or "") >= seit_iso:
                yield t.get("topic") or "", f"{GRAPH}/groups/{gid}/threads/{t['id']}/posts/{p['id']}"


_RUF = re.compile(r"(forensic|failure|ruf)", re.IGNORECASE)


def abrufen() -> dict:
    """Neue Berichte holen, auswerten, neue Befunde melden. Liefert den Abrufstand."""
    import graph_client
    jetzt = datetime.now(timezone.utc)
    abruf = {"ts": jetzt.isoformat(timespec="seconds"), "ok": False, "neu": 0, "doppelt": 0,
             "mails": 0, "fehler": 0, "fehlertexte": [], "meldung": ""}
    if not adresse():
        abruf["meldung"] = "Kein Ziel eingetragen."
        return abruf
    token = graph_client._acquire_token()
    if not token:
        abruf["meldung"] = "Kein Graph-Token (Anbindung fehlt)."
        return _abruf_merken(abruf)
    with httpx.Client(timeout=60) as client:
        z = ziel() or ziel_aufloesen(client, token)
        if z.get("art") not in ("postfach", "gruppe"):
            abruf["meldung"] = z.get("meldung") or "Ziel nicht auflösbar."
            return _abruf_merken(abruf)
        with _lock:
            d = _laden()
        berichte = d.get("berichte") or {}
        try:
            if d.get("bis_ziel") != z["adresse"]:
                raise KeyError("anderes Ziel")       # neues Ziel: wieder 30 Tage zurück
            bis = datetime.fromisoformat(d["bis"])
        except (KeyError, TypeError, ValueError):
            bis = jetzt - timedelta(days=ERSTER_ABRUF_TAGE - UEBERLAPPUNG_H / 24)
        seit = bis - timedelta(hours=UEBERLAPPUNG_H)
        try:
            for betreff, basis in _quellen(client, z, token, seit):
                abruf["mails"] += 1
                if _RUF.search(betreff) and "report domain" not in betreff.lower():
                    # Einzelfallbericht (ruf): nur zählen — er enthält die Mail selbst.
                    abruf["ruf"] = abruf.get("ruf", 0) + 1
                    continue
                for name, daten in _anhaenge(client, basis, token):
                    try:
                        if not daten:
                            raise BerichtFehler("Anhang zu groß")
                        n, dop = anhang_verarbeiten(daten, berichte)
                        abruf["neu"] += n
                        abruf["doppelt"] += dop
                    except BerichtFehler as exc:
                        abruf["fehler"] += 1
                        if len(abruf["fehlertexte"]) < 5:
                            abruf["fehlertexte"].append(f"{name[:80]}: {exc}")
        except _GraphFehler as exc:
            if exc.status == 403 and z["art"] == "gruppe":
                abruf["meldung"] = (f"Kein Zugriff auf die Gruppe — die Berechtigung {GRUPPEN_ROLLE} "
                                    "fehlt oder ist noch nicht wirksam.")
            elif exc.status == 403:
                abruf["meldung"] = ("Kein Zugriff auf das Postfach (HTTP 403) — liegt es außerhalb "
                                    "des RBAC-Bereichs der Gateway-App?")
            elif exc.status == 404 and z["art"] == "postfach":
                abruf["meldung"] = "Postfach nicht gefunden (HTTP 404)."
                settings_store.force_update({ZIEL_KEY: {}})
            else:
                abruf["meldung"] = f"Abruf gescheitert: {exc}"[:300]
            log.warning("DMARC: %s", abruf["meldung"])
            return _abruf_merken(abruf)
    with _lock:
        d = _laden()
        alt = d.get("berichte") or {}
        alt.update({k: v for k, v in berichte.items() if k not in alt})
        d["berichte"] = _aufraeumen(alt)
        d["bis"] = jetzt.isoformat(timespec="seconds")
        d["bis_ziel"] = z["adresse"]
        abruf["ok"] = True
        d["abruf"] = abruf
        neue = _neue_befunde_merken(d)
        _speichern(d)
    log.info("DMARC: %s Mails gelesen, %s neue Berichte, %s doppelt, %s nicht lesbar, %s neue Befunde",
             abruf["mails"], abruf["neu"], abruf["doppelt"], abruf["fehler"], len(neue))
    if neue:
        try:
            import notification
            notification.send_dmarc_befunde(neue)
        except Exception as exc:                                    # noqa: BLE001
            log.warning("DMARC: Benachrichtigung fehlgeschlagen: %s", exc)
    return abruf


def _abruf_merken(abruf: dict) -> dict:
    with _lock:
        d = _laden()
        d["abruf"] = abruf
        _speichern(d)
    return abruf


def faellig() -> bool:
    if not adresse():
        return False
    a = (stand().get("abruf") or {}).get("ts") or ""
    try:
        return datetime.now(timezone.utc) - datetime.fromisoformat(a) > timedelta(hours=TAKT_H)
    except ValueError:
        return True


# ── Auswertung ───────────────────────────────────────────────────────────────

def _bestanden(z: dict) -> bool:
    return z.get("dkim") == "pass" or z.get("spf") == "pass"


def quellen(berichte: dict, tage: int = AUSWERTUNG_TAGE, jetzt: float | None = None) -> list[dict]:
    """Je (Domäne, IP): Mails, bestanden, nur über SPF, Dispositionen, Melder."""
    grenze = (jetzt or time.time()) - tage * 86400
    q: dict[tuple, dict] = {}
    for b in berichte.values():
        if (b.get("ende") or 0) < grenze:
            continue
        for z in b.get("zeilen") or []:
            dom = z.get("header_from") or b["domain"]
            e = q.setdefault((dom, z["ip"]), {
                "domain": dom, "ip": z["ip"], "mails": 0, "bestanden": 0, "nur_spf": 0,
                "fail_zugestellt": 0, "fail_abgewiesen": 0, "dkim_zusatz_kaputt": 0,
                "melder": set(), "selektoren": set()})
            n = z.get("anzahl") or 0
            e["mails"] += n
            e["melder"].add(b["org"])
            if _bestanden(z):
                e["bestanden"] += n
                if z.get("dkim") != "pass":
                    e["nur_spf"] += n
            elif z.get("disposition") in ("quarantine", "reject"):
                e["fail_abgewiesen"] += n
            else:
                e["fail_zugestellt"] += n
            eigene = [a for a in z.get("dkim_auth") or [] if a.get("d") == dom]
            for a in eigene:
                if a.get("s"):
                    e["selektoren"].add(a["s"])
            if any(a["r"] == "pass" for a in eigene) and any(a["r"] not in ("pass", "") for a in eigene):
                e["dkim_zusatz_kaputt"] += n
    aus = []
    for e in q.values():
        e["melder"] = sorted(e["melder"])
        e["selektoren"] = sorted(e["selektoren"])
        aus.append(e)
    return sorted(aus, key=lambda e: (-e["mails"], e["domain"], e["ip"]))


def _richtlinien(berichte: dict) -> dict[str, dict]:
    """Je Domäne die zuletzt gemeldete Richtlinie."""
    r: dict[str, tuple[int, dict]] = {}
    for b in berichte.values():
        if b["domain"] not in r or (b.get("ende") or 0) > r[b["domain"]][0]:
            r[b["domain"]] = (b.get("ende") or 0, b.get("richtlinie") or {})
    return {d: v[1] for d, v in r.items()}


def befunde(berichte: dict, jetzt: float | None = None) -> list[dict]:
    """[{kennung, stufe, art, domain, ip, mails, text, empfehlung}] — hoch zuerst."""
    aus = []

    def neu(stufe, art, domain, ip, mails, text, empfehlung):
        aus.append({"kennung": f"{art}|{domain}|{ip}", "stufe": stufe, "art": art,
                    "domain": domain, "ip": ip, "mails": mails, "text": text,
                    "empfehlung": empfehlung})

    for e in quellen(berichte, jetzt=jetzt):
        dom, ip = e["domain"], e["ip"]
        fail = e["fail_zugestellt"] + e["fail_abgewiesen"]
        if e["fail_zugestellt"]:
            neu("hoch", "fail_zugestellt", dom, ip, e["fail_zugestellt"],
                f"{e['fail_zugestellt']} Mails von {ip} im Namen von {dom} bestanden DMARC nicht "
                "und wurden trotzdem zugestellt.",
                "Gehört die Quelle zu dir (Newsletter, CRM, Drucker, Fremddienst)? Dann SPF bzw. "
                "DKIM dort einrichten. Sonst ist es eine Fälschung — die Richtlinie auf "
                "quarantine oder reject anheben.")
        if e["bestanden"] and fail:
            neu("mittel", "teilweise", dom, ip, fail,
                f"{ip} besteht DMARC für {dom} nur teilweise: {e['bestanden']} bestanden, "
                f"{fail} nicht.",
                "Eine eigene Quelle mit Lücken — etwa ein zweiter Ausgang ohne DKIM oder "
                "Weiterleitungen. Die Kopfzeilen einer betroffenen Mail zeigen den Weg.")
        if e["nur_spf"]:
            neu("mittel", "nur_spf", dom, ip, e["nur_spf"],
                f"{e['nur_spf']} Mails von {ip} bestanden DMARC nur über SPF; DKIM fehlte oder "
                f"passte nicht zu {dom}.",
                "DKIM für diesen Versender einrichten. Ohne DKIM fällt DMARC bei jeder "
                "Weiterleitung durch.")
        if e["dkim_zusatz_kaputt"]:
            neu("mittel", "dkim_zusatz_kaputt", dom, ip, e["dkim_zusatz_kaputt"],
                f"{e['dkim_zusatz_kaputt']} Mails von {ip} trugen neben einer gültigen auch eine "
                f"ungültige DKIM-Signatur von {dom}.",
                "Ein Weg reicht eine veraltete Signatur weiter. Das Gateway entfernt sie seit "
                "v1.12.137 auf allen Rückwegen; bleibt der Befund, liegt die Quelle woanders.")
        if fail and not e["bestanden"] and not e["fail_zugestellt"]:
            neu("info", "abgewiesen", dom, ip, fail,
                f"{fail} Mails von {ip} im Namen von {dom} bestanden DMARC nicht und wurden "
                "abgewiesen oder isoliert.",
                "Die Richtlinie wirkt. Nur handeln, wenn die Quelle doch zu dir gehört.")
    for dom, r in sorted(_richtlinien(berichte).items()):
        if r.get("p") == "none":
            neu("mittel", "richtlinie_none", dom, "", 0,
                f"Die DMARC-Richtlinie von {dom} steht auf p=none — Fälschungen werden zugestellt.",
                "Sobald alle eigenen Quellen bestehen (keine Befunde „nur teilweise“), auf "
                "p=quarantine und später p=reject anheben.")
        elif r.get("p") in ("quarantine", "reject") and (r.get("pct") or 100) < 100:
            neu("info", "pct", dom, "", 0,
                f"Die Richtlinie von {dom} gilt nur für {r['pct']} % der Mails (pct).",
                "Wenn keine eigenen Quellen mehr scheitern, pct auf 100 setzen.")
    aus.sort(key=lambda b: (STUFEN.index(b["stufe"]), -b["mails"], b["domain"], b["ip"]))
    return aus


def _neue_befunde_merken(d: dict) -> list[dict]:
    """Befunde hoch/mittel, die bisher nicht bekannt waren — und merken."""
    bekannt = d.setdefault("bekannt", {})
    neu = []
    for b in befunde(d.get("berichte") or {}):
        if b["stufe"] in ("hoch", "mittel") and b["kennung"] not in bekannt:
            bekannt[b["kennung"]] = int(time.time())
            neu.append(b)
    if len(bekannt) > 5000:
        d["bekannt"] = dict(sorted(bekannt.items(), key=lambda kv: kv[1])[-5000:])
    return neu


def rdns(ips: list[str], frist_s: float = 3.0) -> dict[str, str]:
    """Rückwärtsnamen (PTR), zwischengespeichert einen Tag; nicht auflösbar = ""."""
    jetzt = time.time()
    offen = [ip for ip in ips if ip not in _rdns_cache or jetzt - _rdns_cache[ip][0] > 86400]

    def eins(ip):
        try:
            return ip, socket.gethostbyaddr(ip)[0][:253]
        except (OSError, UnicodeError):
            return ip, ""

    if offen:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = [pool.submit(eins, ip) for ip in offen[:200]]
            for f in futs:
                try:
                    ip, name = f.result(timeout=frist_s)
                    _rdns_cache[ip] = (jetzt, name)
                except Exception:                                   # noqa: BLE001
                    pass
    return {ip: _rdns_cache.get(ip, (0, ""))[1] for ip in ips}


def uebersicht() -> dict:
    """Alles für die Oberfläche: Ziel, Abruf, Domänen, Quellen, Befunde."""
    d = stand()
    berichte = d.get("berichte") or {}
    q = quellen(berichte)
    namen = rdns([e["ip"] for e in q[:100]])
    for e in q:
        e["rdns"] = namen.get(e["ip"], "")
    grenze = time.time() - AUSWERTUNG_TAGE * 86400
    aktuell = [b for b in berichte.values() if (b.get("ende") or 0) >= grenze]
    domaenen = []
    for dom, r in sorted(_richtlinien(berichte).items()):
        es = [e for e in q if e["domain"] == dom]
        mails = sum(e["mails"] for e in es)
        domaenen.append({"domain": dom, "richtlinie": r, "mails": mails,
                         "bestanden": sum(e["bestanden"] for e in es),
                         "melder": len({m for e in es for m in e["melder"]})})
    return {"adresse": adresse(), "ziel": ziel(), "abruf": d.get("abruf") or {},
            "berichte": len(aktuell), "berichte_gesamt": len(berichte), "tage": AUSWERTUNG_TAGE,
            "domaenen": domaenen, "quellen": q[:300], "befunde": befunde(berichte)}


def bewertung() -> tuple[str, str]:
    """(ampel, text) für Tagesbericht und Abnahme. ampel: ok|warnung|fehler|keine."""
    if not adresse():
        return "keine", "kein Ziel für DMARC-Berichte eingetragen"
    d = stand()
    a = d.get("abruf") or {}
    if not a:
        return "warnung", "noch kein Abruf gelaufen"
    if not a.get("ok"):
        return "fehler", a.get("meldung") or "Abruf gescheitert"
    berichte = d.get("berichte") or {}
    bf = befunde(berichte)
    hoch = sum(b["stufe"] == "hoch" for b in bf)
    mittel = sum(b["stufe"] == "mittel" for b in bf)
    grenze = time.time() - AUSWERTUNG_TAGE * 86400
    n = sum((b.get("ende") or 0) >= grenze for b in berichte.values())
    text = f"{n} Berichte in {AUSWERTUNG_TAGE} Tagen · Befunde: {hoch} hoch, {mittel} mittel"
    if not n:
        return "warnung", f"seit {AUSWERTUNG_TAGE} Tagen kein Bericht angekommen — stimmt die rua-Adresse?"
    return ("fehler" if hoch else "warnung" if mittel else "ok"), text
