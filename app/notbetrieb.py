"""Notbetrieb „Exchange-Schatten“: laufende Kopie der jüngsten Post je Postfach.

WOZU
----
Fällt Exchange Online aus, sollen die Nutzer über das Gateway weiterarbeiten
können — mit dem Kontext der letzten Tage, nicht mit einem leeren Postfach.
Dafür hält das Gateway einen Schatten: Posteingang und Gesendete Elemente der
letzten `MAIL_FENSTER_TAGE` Tage als MIME-Original. Nach der Störung wird, was
das Gateway im Notbetrieb angenommen hat, nach Exchange zurückgespielt.

Diese Stufe enthält Abgleich, Aufbewahrung, Rückspielen und Löschen. Die Zugänge
für die Clients (IMAP, EAS, Webmail) und das Annehmen von Post im Störfall folgen.

ABLAGE
------
`DATA_DIR/notbetrieb/schatten.db` (Verzeichnis) plus je Mail eine `.eml`-Datei
unter `mime/<postfach-kennung>/`. Alles 600/700 über `secure_io` — der Schatten
enthält vollständige Postfachinhalte.

Nicht im Backup (`backup_manager._EXCLUDE_DATA_SUBDIRS`): Die Kopien von Exchange
lassen sich jederzeit neu abgleichen, und drei Tage Post aller Postfächer
würden jedes Backup sprengen.

DIE ZWEI HERKÜNFTE
------------------
`exo`    Abgleich von Exchange. Darf jederzeit verworfen werden — sie liegt
         im Original noch bei Microsoft.
`lokal`  Im Notbetrieb entstanden. Liegt NUR hier, bis sie zurückgespielt
         ist. ⚠️ Weder Aufbewahrung noch Löschen dürfen sie vorher anfassen —
         das ist die eine Zusage, an der der ganze Notbetrieb hängt.

RÜCKSPIELEN
-----------
Idempotent über die Message-ID: Steht eine Nachricht mit derselben
`internetMessageId` schon im Postfach, wird sie nur als erledigt vermerkt.
Ein zweiter Lauf nach einem Abbruch spielt deshalb nichts doppelt ein. Eine
lokale Mail ohne Message-ID bekommt beim Ablegen eine, sonst gäbe es für sie
keinen Abgleich.

Das Rückspielen löst bewusst KEINE Abwesenheitsantwort aus (anders als die
Ablage-Wege in `tests/test_oof_ablage.py`): Die Post wurde während der Störung
angenommen; eine Antwort Tage später wäre falsch adressiert.
"""
from __future__ import annotations

import base64
import email
import email.policy
import email.utils
import hashlib
import logging
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

import config
import graph_client
import secure_io
import settings_store

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"

# Posteingang und Gesendete Elemente — mit dem Gesendeten sieht ein Nutzer im
# Notbetrieb auch, was er zuletzt selbst geschrieben hat. Bekannte Ordnernamen
# laut Graph (mailFolder: well-known folder names).
ORDNER = ("inbox", "sentitems")
MAIL_FENSTER_TAGE = 3

_LAST_KEY = "_NOTBETRIEB_LAST"      # Zählung des letzten Abgleichs (für die Oberfläche)
EXO, LOKAL = "exo", "lokal"

_lock = threading.Lock()


def _wurzel() -> Path:
    return Path(config.DATA_DIR) / "notbetrieb"


def _db_pfad() -> Path:
    return _wurzel() / "schatten.db"


def funktion_aktiv() -> bool:
    """EINE Prüfung für Scheduler, Endpunkte und Oberfläche."""
    return bool(settings_store.get("NOTBETRIEB_AKTIV"))


# ── Ablage ──────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    secure_io.ensure_dir(_wurzel())
    conn = sqlite3.connect(str(_db_pfad()), check_same_thread=False)
    # Wie mail_audit: sqlite legt mit der umask an; zur Laufzeit entstehende
    # Datei sofort härten, nicht erst beim nächsten Start.
    secure_io.harden_file(_db_pfad())
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mails (
            postfach  TEXT NOT NULL,
            graph_id  TEXT NOT NULL,
            ordner    TEXT NOT NULL,
            imid      TEXT,
            empfangen TEXT NOT NULL,
            gelesen   INTEGER NOT NULL DEFAULT 0,
            datei     TEXT NOT NULL,
            herkunft  TEXT NOT NULL,
            zurueck   TEXT,
            PRIMARY KEY (postfach, graph_id)
        );
        CREATE TABLE IF NOT EXISTS delta (
            postfach TEXT NOT NULL,
            ordner   TEXT NOT NULL,
            link     TEXT NOT NULL,
            PRIMARY KEY (postfach, ordner)
        );
        """)
    return conn


def _kennung(text: str) -> str:
    return hashlib.sha256(text.lower().encode()).hexdigest()[:16]


def _mime_pfad(postfach: str, graph_id: str) -> Path:
    return _wurzel() / "mime" / _kennung(postfach) / (hashlib.sha256(graph_id.encode()).hexdigest() + ".eml")


def _schreiben(postfach: str, graph_id: str, mime: bytes) -> Path:
    pfad = _mime_pfad(postfach, graph_id)
    secure_io.ensure_dir(pfad.parent.parent)
    secure_io.ensure_dir(pfad.parent)
    secure_io.write_secret_bytes(pfad, mime)
    return pfad


def _datei_weg(datei: str) -> None:
    try:
        Path(datei).unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Notbetrieb: Schattendatei nicht löschbar (%s): %s", datei, exc)


def _grenze(jetzt: datetime | None = None) -> datetime:
    return (jetzt or datetime.now(timezone.utc)) - timedelta(days=MAIL_FENSTER_TAGE)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _zeit(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat((text or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def lokal_ablegen(postfach: str, ordner: str, mime: bytes,
                  empfangen: datetime | None = None) -> str:
    """Im Notbetrieb entstandene Post ablegen. Gibt die Message-ID zurück.

    Fehlt die Message-ID, wird eine gesetzt — ohne sie wäre das Rückspielen
    nicht idempotent (Abgleich über internetMessageId)."""
    msg = email.message_from_bytes(mime, policy=email.policy.compat32)
    imid = (msg.get("Message-ID") or "").strip()
    if not imid:
        domain = postfach.split("@", 1)[-1] or "gateway.invalid"
        imid = email.utils.make_msgid(domain=domain)
        mime = f"Message-ID: {imid}\r\n".encode() + mime
    graph_id = "lokal:" + uuid.uuid4().hex
    pfad = _schreiben(postfach, graph_id, mime)
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO mails (postfach, graph_id, ordner, imid, empfangen, gelesen, datei, herkunft)"
            " VALUES (?,?,?,?,?,0,?,?)",
            (postfach.lower(), graph_id, ordner, imid,
             _iso(empfangen or datetime.now(timezone.utc)), str(pfad), LOKAL))
    return imid


# ── Abgleich von Exchange ───────────────────────────────────────────────────

def _postfaecher() -> list[str]:
    """Aktivierte Postfächer (sig oder smime), wie der Abwesenheits-Abgleich."""
    import abwesenheit
    return [upn.lower() for upn, _ in
            abwesenheit._aktive_postfaecher(settings_store.get("MAILBOX_CONFIG") or {})]


def _abgleich_ordner(client: httpx.Client, token: str, postfach: str, ordner: str,
                     zaehlung: dict) -> None:
    hdr = {"Authorization": f"Bearer {token}", "Prefer": "odata.maxpagesize=50"}
    with _lock, _conn() as conn:
        zeile = conn.execute("SELECT link FROM delta WHERE postfach=? AND ordner=?",
                             (postfach, ordner)).fetchone()
    grenze = _grenze()
    url = zeile["link"] if zeile else (
        f"{GRAPH}/users/{postfach}/mailFolders/{ordner}/messages/delta"
        f"?$select=id,internetMessageId,receivedDateTime,isRead"
        f"&$filter=receivedDateTime+ge+{_iso(grenze)}")
    while url:
        resp = client.get(url, headers=hdr)
        if resp.status_code == 410 and zeile:
            # Delta-Token verfallen: neu aufsetzen. Vorhandene Kopien bleiben,
            # die Einfügungen unten sind Upserts.
            log.info("Notbetrieb: Delta-Token für %s/%s verfallen — Neuabgleich", postfach, ordner)
            with _lock, _conn() as conn:
                conn.execute("DELETE FROM delta WHERE postfach=? AND ordner=?", (postfach, ordner))
            return _abgleich_ordner(client, token, postfach, ordner, zaehlung)
        if resp.status_code != 200:
            raise RuntimeError(f"Delta {ordner}: HTTP {resp.status_code} — {resp.text[:200]}")
        daten = resp.json()
        for m in daten.get("value") or []:
            gid = m.get("id") or ""
            if not gid:
                continue
            if "@removed" in m:
                with _lock, _conn() as conn:
                    alt = conn.execute(
                        "SELECT datei FROM mails WHERE postfach=? AND graph_id=? AND herkunft=?",
                        (postfach, gid, EXO)).fetchone()
                    if alt:
                        conn.execute("DELETE FROM mails WHERE postfach=? AND graph_id=?", (postfach, gid))
                if alt:
                    _datei_weg(alt["datei"])
                    zaehlung["entfernt"] += 1
                continue
            empfangen = _zeit(m.get("receivedDateTime") or "")
            with _lock, _conn() as conn:
                da = conn.execute("SELECT 1 FROM mails WHERE postfach=? AND graph_id=?",
                                  (postfach, gid)).fetchone()
                if da:
                    conn.execute("UPDATE mails SET gelesen=? WHERE postfach=? AND graph_id=?",
                                 (1 if m.get("isRead") else 0, postfach, gid))
                    continue
            # Neu — aber nur innerhalb des Fensters. Der Filter wirkt nur in der
            # ersten Runde; spätere Runden melden auch hineinverschobene Altpost.
            if empfangen is None or empfangen < grenze:
                continue
            roh = client.get(f"{GRAPH}/users/{postfach}/messages/{gid}/$value",
                             headers={"Authorization": f"Bearer {token}"})
            if roh.status_code != 200:
                zaehlung["fehler"] += 1
                log.warning("Notbetrieb: MIME von %s nicht lesbar: HTTP %s", postfach, roh.status_code)
                continue
            pfad = _schreiben(postfach, gid, roh.content)
            with _lock, _conn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO mails (postfach, graph_id, ordner, imid, empfangen,"
                    " gelesen, datei, herkunft) VALUES (?,?,?,?,?,?,?,?)",
                    (postfach, gid, ordner, m.get("internetMessageId") or "", _iso(empfangen),
                     1 if m.get("isRead") else 0, str(pfad), EXO))
            zaehlung["neu"] += 1
        url = daten.get("@odata.nextLink") or ""
        ende = daten.get("@odata.deltaLink") or ""
        if ende:
            with _lock, _conn() as conn:
                conn.execute("INSERT OR REPLACE INTO delta (postfach, ordner, link) VALUES (?,?,?)",
                             (postfach, ordner, ende))


def aufbewahrung_durchsetzen(jetzt: datetime | None = None) -> int:
    """Kopien älter als das Fenster löschen. Lokale Post erst, wenn sie
    zurückgespielt ist (siehe Modul-Docstring). Gibt die Anzahl zurück."""
    if not _db_pfad().exists():
        return 0
    grenze = _iso(_grenze(jetzt))
    with _lock, _conn() as conn:
        alt = conn.execute(
            "SELECT postfach, graph_id, datei FROM mails WHERE empfangen < ?"
            " AND (herkunft=? OR zurueck IS NOT NULL)", (grenze, EXO)).fetchall()
        conn.executemany("DELETE FROM mails WHERE postfach=? AND graph_id=?",
                         [(z["postfach"], z["graph_id"]) for z in alt])
    for z in alt:
        _datei_weg(z["datei"])
    return len(alt)


def _persist_last(summary: dict) -> None:
    settings_store.force_update({_LAST_KEY: {**summary, "ts": datetime.now(timezone.utc).isoformat()}})


def abgleich_alle() -> dict:
    """Schatten aller aktivierten Postfächer nachführen; Zählung mit Bezugsgröße."""
    zaehlung = {"neu": 0, "entfernt": 0, "abgelaufen": 0, "fehler": 0, "postfaecher_fehler": 0}
    if not funktion_aktiv():
        return {"aktiv": False, **zaehlung, "postfaecher": 0}
    postfaecher = _postfaecher()
    token = graph_client._acquire_token() if postfaecher else None
    if postfaecher and not token:
        ergebnis = {"aktiv": True, **zaehlung, "postfaecher": len(postfaecher), "kein_token": True}
        _persist_last(ergebnis)
        return ergebnis
    with httpx.Client(timeout=30) as client:
        for pf in postfaecher:
            try:
                for ordner in ORDNER:
                    _abgleich_ordner(client, token, pf, ordner, zaehlung)
            except Exception as exc:                               # noqa: BLE001
                zaehlung["postfaecher_fehler"] += 1
                log.warning("Notbetrieb: Abgleich %s fehlgeschlagen: %s", pf, exc)
    zaehlung["abgelaufen"] = aufbewahrung_durchsetzen()
    ergebnis = {"aktiv": True, **zaehlung, "postfaecher": len(postfaecher), **_bestand()}
    log.info("Notbetrieb: Abgleich %s Postfächer — %s neu, %s entfernt, %s abgelaufen, %s Fehler",
             len(postfaecher), zaehlung["neu"], zaehlung["entfernt"], zaehlung["abgelaufen"],
             zaehlung["fehler"] + zaehlung["postfaecher_fehler"])
    _persist_last(ergebnis)
    return ergebnis


# ── Rückspielen nach der Störung ────────────────────────────────────────────

def _odata_text(wert: str) -> str:
    return wert.replace("'", "''")


def zurueckspielen() -> dict:
    """Lokale, noch nicht zurückgespielte Post nach Exchange bringen."""
    zaehlung = {"eingespielt": 0, "schon_da": 0, "fehler": 0, "offen": 0}
    if not _db_pfad().exists():
        return zaehlung
    with _lock, _conn() as conn:
        offen = conn.execute("SELECT * FROM mails WHERE herkunft=? AND zurueck IS NULL",
                             (LOKAL,)).fetchall()
    if not offen:
        return zaehlung
    token = graph_client._acquire_token()
    if not token:
        zaehlung["offen"] = len(offen)
        zaehlung["kein_token"] = True
        return zaehlung
    json_hdr = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    with httpx.Client(timeout=30) as client:
        for z in offen:
            pf, imid = z["postfach"], z["imid"]
            try:
                such = client.get(
                    f"{GRAPH}/users/{pf}/messages",
                    params={"$filter": f"internetMessageId eq '{_odata_text(imid)}'", "$select": "id"},
                    headers={"Authorization": f"Bearer {token}"})
                if such.status_code != 200:
                    raise RuntimeError(f"Suche HTTP {such.status_code}")
                if such.json().get("value"):
                    _erledigt(z, "vorhanden")
                    zaehlung["schon_da"] += 1
                    continue
                mime = Path(z["datei"]).read_bytes()
                # Wie graph_reinject.deliver_to_mailbox_mime: erst CRLF, dann base64.
                mime = mime.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                resp = client.post(f"{GRAPH}/users/{pf}/mailFolders/{z['ordner']}/messages",
                                   content=base64.b64encode(mime),
                                   headers={"Authorization": f"Bearer {token}",
                                            "Content-Type": "text/plain"})
                if resp.status_code not in (200, 201):
                    raise RuntimeError(f"Einspielen HTTP {resp.status_code} — {resp.text[:200]}")
                neu_id = resp.json().get("id", "")
                # Ohne diesen PATCH zeigt Outlook Classic einen Senden-Knopf.
                patch = client.patch(f"{GRAPH}/users/{pf}/messages/{neu_id}", json={
                    "isDraft": False,
                    "isRead": bool(z["gelesen"]),
                    "singleValueExtendedProperties": [{"id": "Integer 0x0e07", "value": "1"}],
                }, headers=json_hdr)
                if patch.status_code not in (200, 201, 204):
                    log.warning("Notbetrieb: isDraft-PATCH für %s fehlgeschlagen: HTTP %s",
                                pf, patch.status_code)
                _erledigt(z, "eingespielt")
                zaehlung["eingespielt"] += 1
            except Exception as exc:                                # noqa: BLE001
                zaehlung["fehler"] += 1
                log.warning("Notbetrieb: Rückspielen %s für %s fehlgeschlagen: %s", imid, pf, exc)
    zaehlung["offen"] = zaehlung["fehler"]
    log.info("Notbetrieb: Rückspielen — %s eingespielt, %s schon vorhanden, %s Fehler",
             zaehlung["eingespielt"], zaehlung["schon_da"], zaehlung["fehler"])
    return zaehlung


def _erledigt(zeile, wie: str) -> None:
    with _lock, _conn() as conn:
        conn.execute("UPDATE mails SET zurueck=? WHERE postfach=? AND graph_id=?",
                     (f"{wie} {_iso(datetime.now(timezone.utc))}", zeile["postfach"], zeile["graph_id"]))


# ── Bestand und Löschen ─────────────────────────────────────────────────────

def _bestand() -> dict:
    if not _db_pfad().exists():
        return {"mails": 0, "lokal_offen": 0, "postfaecher_im_schatten": 0}
    with _lock, _conn() as conn:
        mails = conn.execute("SELECT COUNT(*) FROM mails").fetchone()[0]
        offen = conn.execute("SELECT COUNT(*) FROM mails WHERE herkunft=? AND zurueck IS NULL",
                             (LOKAL,)).fetchone()[0]
        pf = conn.execute("SELECT COUNT(DISTINCT postfach) FROM mails").fetchone()[0]
    return {"mails": mails, "lokal_offen": offen, "postfaecher_im_schatten": pf}


def status() -> dict:
    return {"aktiv": funktion_aktiv(), "vorhanden": _wurzel().exists(), **_bestand(),
            "letzter_lauf": settings_store.get(_LAST_KEY) or None,
            "fenster_tage": MAIL_FENSTER_TAGE}


def schatten_loeschen() -> dict:
    """Den ganzen Schatten entfernen — verweigert, solange lokale Post nicht
    zurückgespielt ist (die gäbe es sonst nirgends mehr)."""
    offen = _bestand()["lokal_offen"]
    if offen:
        return {"ok": False, "lokal_offen": offen,
                "error": f"{offen} im Notbetrieb angenommene Mails sind noch nicht zurückgespielt."}
    with _lock:
        if _wurzel().exists():
            shutil.rmtree(_wurzel())
    settings_store.force_update({_LAST_KEY: None})
    log.info("Notbetrieb: Schatten gelöscht")
    return {"ok": True}
