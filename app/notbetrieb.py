"""Notbetrieb „Exchange-Schatten“: laufende Kopie der jüngsten Post je Postfach.

WOZU
----
Fällt Exchange Online aus, sollen die Nutzer über das Gateway weiterarbeiten
können — mit dem Kontext der letzten Tage, nicht mit einem leeren Postfach.
Dafür hält das Gateway einen Schatten: die volle Ordnerstruktur jedes Postfachs,
darin die Post der letzten Tage als MIME-Original. Nach der Störung wird, was
das Gateway im Notbetrieb angenommen hat, nach Exchange zurückgespielt.

FENSTER
-------
Wie weit der Schatten reicht, bestimmt `fenster_fuer()` — EINE Auflösung für
Abgleich, Aufbewahrung und Anzeige: die erste passende Regel aus
`NOTBETRIEB_REGELN` (Ziel ein einzelnes Postfach oder eine interne Gruppe, von
oben nach unten wie die Gruppen-Richtlinien), sonst `NOTBETRIEB_FENSTER`, sonst
`VORGABE_FENSTER`. Die Werte kommen ungeprüft aus der Oberfläche und werden
hier begrenzt (`_tage`), nicht beim Speichern.

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
keinen Abgleich. Gesucht wird im Zielordner, nicht im ganzen Postfach (Mail
an sich selbst: Posteingang und Gesendet tragen dieselbe Message-ID).

Angelegt wird per JSON mit PR_MESSAGE_FLAGS, NICHT per MIME — eine MIME-Anlage
ist unwiderruflich ein Entwurf (Einzelheiten `_als_graph`). Kommt die Mail beim
nächsten Abgleich aus Exchange zurück, wird die lokale Zeile zur Exchange-Kopie
(gleiche Message-ID, gleicher Ordner bevorzugt) statt doppelt zu stehen.

Das Rückspielen löst bewusst KEINE Abwesenheitsantwort aus (anders als die
Ablage-Wege in `tests/test_oof_ablage.py`): Die Post wurde während der Störung
angenommen; eine Antwort Tage später wäre falsch adressiert.
"""
from __future__ import annotations

import base64
import contextvars
import email
import email.policy
import email.utils
import hashlib
import json
import logging
import shutil
from collections import defaultdict
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

# Mail: Tage zurück. Kalender: Tage zurück und voraus (Kalender folgt).
VORGABE_FENSTER = {"mail": 3, "kal_zurueck": 3, "kal_vor": 3}
MAX_TAGE = 365

_LAST_KEY = "_NOTBETRIEB_LAST"      # Zählung des letzten Abgleichs (für die Oberfläche)
_MESS_KEY = "_NOTBETRIEB_MESSUNG"   # Platzmessung aus Exchange (Was-wäre-wenn)
MESS_TAGE = 30                      # so weit zurück wird gemessen; darüber hochgerechnet
# Verhältnis MIME-Original zu Exchanges Größenangabe. NICHT fest annehmen:
# Am 14.11.2026 im DEV-Tenant gemessen lag es bei 0,44 (Summe, 25 Mails;
# Einzelwerte 0,08–0,87) — vermutet war 1,37. Jede Messung eicht deshalb an
# einer Zufallsstichprobe (EICH_PROBEN); ohne Eichung gilt 1,0, was den
# Bedarf eher überschätzt als unterschätzt.
MIME_FAKTOR_OHNE_EICHUNG = 1.0
EICH_PROBEN = 30
_EICH_JE_POSTFACH = 5
EXO, LOKAL = "exo", "lokal"

_lock = threading.Lock()


def _wurzel() -> Path:
    return Path(config.DATA_DIR) / "notbetrieb"


def _db_pfad() -> Path:
    return _wurzel() / "schatten.db"


def funktion_aktiv() -> bool:
    """EINE Prüfung für Scheduler, Endpunkte und Oberfläche."""
    return bool(settings_store.get("NOTBETRIEB_AKTIV"))


def ausgerufen() -> bool:
    """Notfall ausgerufen: Was das Gateway an Exchange übergäbe, geht in die
    Kopie bzw. in den Direktversand (`reinject.send`). Nur bei eingeschaltetem
    Notbetrieb — ein liegengebliebener Schalter ohne Kopie lenkt nichts um."""
    return funktion_aktiv() and settings_store.get("NOTBETRIEB_AUSGERUFEN") is True


def _in_probegruppe(benutzer: str) -> bool:
    probe = settings_store.get("NOTBETRIEB_PROBEGRUPPE") or []
    return benutzer.strip().lower() in {str(a).strip().lower() for a in probe if a}


def lesen_alle() -> bool:
    """Schalter: Die Kopie ist schon im Normalbetrieb für alle lesbar
    (Vorschau — man sieht jederzeit, was im Ausfall da wäre)."""
    return settings_store.get("NOTBETRIEB_LESEN_ALLE") is True


def inhalte_erlaubt(benutzer: str) -> bool:
    """LESEN der Kopie (Webmail, IMAP). Im Notfall alle; vorher die
    Probegruppe und — mit `lesen_alle` — alle. Sonst sieht man „bereit“."""
    return ausgerufen() or lesen_alle() or _in_probegruppe(benutzer)


def senden_erlaubt(benutzer: str) -> bool:
    """SENDEN (Webmail, 587). Strenger als Lesen: Gesendet wird im
    Notbetrieb am Exchange vorbei (Direktversand, interne Post in die Kopie).
    Im Normalbetrieb liefe das neben Exchange her — dort also nur die
    Probegruppe (Probealarm), auch wenn alle lesen dürfen."""
    return ausgerufen() or _in_probegruppe(benutzer)


def postfach_fuer(adresse: str) -> str | None:
    """Postfach (Primäradresse) zu einer Adresse — Primäradresse, UPN oder ein
    Alias aus den gesicherten Profilen —, sonst None."""
    a = (adresse or "").strip().lower()
    if not a or not _db_pfad().exists():
        return None
    with _lock, _conn() as conn:
        z = conn.execute("SELECT primary_adr FROM konten WHERE lower(primary_adr)=? OR lower(upn)=?",
                         (a, a)).fetchone()
        if z:
            return z["primary_adr"].lower()
        p = conn.execute("SELECT daten FROM profile WHERE adresse=?", (a,)).fetchone()
    if not p:
        return None
    daten = json.loads(p["daten"])
    for kandidat in (daten.get("mail"), daten.get("userPrincipalName")):
        if kandidat and kandidat.lower() != a:
            return postfach_fuer(kandidat)
    return None


# Zielordner für interne Empfänger: Posteingang, oder Junk-E-Mail, wenn die
# Echtheitsprüfung des Notfall-Eingangs das verlangt (notbetrieb_echtheit). Der
# Handler setzt ihn zu Beginn JEDER Mail neu — eine zweite Mail derselben
# Verbindung erbt die Entscheidung der ersten nicht.
eingang_ordner: contextvars.ContextVar = contextvars.ContextVar("notbetrieb_eingang_ordner",
                                                                 default="inbox")


def zustellen(mail_from: str, rcpt_tos: list, inhalt: bytes) -> dict:
    """EIN Zustellweg im Notbetrieb — für das Webmail und, solange der Notfall
    ausgerufen ist, für alles, was das Gateway an Exchange übergäbe:
    interne Empfänger (Postfach im Rechte-Abbild) in den Posteingang ihrer
    Kopie, externe über den Direktversand. Erst nach außen, dann intern —
    scheitert der Direktversand, ist noch nichts halb angekommen. Wirft bei
    Fehler (der Aufrufer entscheidet über Warteschlange oder Meldung)."""
    # Aliase auf ihr Postfach: Ohne das hielte die Zustellung `vorname.nachname@`
    # für extern und schickte die Mail per Direktversand an den eigenen MX.
    ziele = {e: postfach_fuer(e) for e in rcpt_tos}
    intern = [pf for pf in ziele.values() if pf]
    extern = [e for e, pf in ziele.items() if not pf]
    if extern:
        import direktversand
        import domain_routing
        import reinject
        if not direktversand.aktiv():
            raise RuntimeError("Direktversand nicht eingerichtet — externe Empfänger nicht erreichbar")
        ziel = domain_routing.aufloesen(domain_routing.DIREKT)
        reinject._an_postfix(mail_from, extern, inhalt, ziel["host"], ziel["port"])
    for e in dict.fromkeys(intern):
        lokal_ablegen(e, eingang_ordner.get(), inhalt)
    return {"intern": len(intern), "extern": len(extern)}


def sichtbar() -> bool:
    """Seite und Menüpunkt: an ODER es liegt noch eine Kopie. Eine
    ausgeschaltete Funktion darf keine Daten behalten, an die niemand mehr
    herankommt — Zurückspielen und Löschen bleiben erreichbar."""
    return funktion_aktiv() or _wurzel().exists()


# ── Fenster ─────────────────────────────────────────────────────────────────

def _tage(wert, vorgabe: int) -> int:
    try:
        n = int(wert)
    except (TypeError, ValueError):
        return vorgabe
    return max(0, min(MAX_TAGE, n))


def _fenster_aus(quelle: dict | None, basis: dict) -> dict:
    quelle = quelle if isinstance(quelle, dict) else {}
    return {k: _tage(quelle.get(k), basis[k]) for k in VORGABE_FENSTER}


def standard_fenster() -> dict:
    return _fenster_aus(settings_store.get("NOTBETRIEB_FENSTER"), VORGABE_FENSTER)


def fenster_fuer(config_key: str, upn: str = "", regeln: list | None = None,
                 std: dict | None = None) -> tuple[dict, str]:
    """(Fenster, Herkunft) für ein Postfach. Herkunft: „Postfach“, „Gruppe <Name>“
    oder „Vorgabe“ — für die Anzeige, damit sichtbar ist, warum was gilt.

    Eine Postfach-Regel nennt die Adresse (so zeigt sie die Oberfläche); der
    Schlüssel in MAILBOX_CONFIG kann auch eine ExchangeGuid sein — beides passt.
    Gruppen führen Schlüssel."""
    import gruppen
    std = standard_fenster() if std is None else _fenster_aus(std, VORGABE_FENSTER)
    if regeln is None:
        regeln = settings_store.get("NOTBETRIEB_REGELN") or []
    mitglied = None
    for regel in regeln if isinstance(regeln, list) else []:
        if not isinstance(regel, dict):
            continue
        ziel = str(regel.get("ziel") or "")
        art, _, name = ziel.partition(":")
        if art == "postfach" and name.lower() in (config_key.lower(), upn.lower()):
            return _fenster_aus(regel, std), "Postfach"
        if art == "gruppe":
            if mitglied is None:
                mitglied = {g: {m.lower() for m in ms} for g, ms in gruppen.interne_gruppen().items()}
            if config_key.lower() in mitglied.get(name, set()):
                return _fenster_aus(regel, std), f"Gruppe {name}"
    return std, "Vorgabe"


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
            tage     INTEGER NOT NULL,
            PRIMARY KEY (postfach, ordner)
        );
        CREATE TABLE IF NOT EXISTS konten (
            primary_adr TEXT PRIMARY KEY,
            upn         TEXT,
            typ         TEXT
        );
        CREATE TABLE IF NOT EXISTS rechte (
            postfach TEXT NOT NULL,
            nutzer   TEXT NOT NULL,
            recht    TEXT NOT NULL,
            PRIMARY KEY (postfach, nutzer, recht)
        );
        CREATE TABLE IF NOT EXISTS meta (
            k TEXT PRIMARY KEY,
            v TEXT
        );
        CREATE TABLE IF NOT EXISTS profile (
            adresse TEXT PRIMARY KEY,
            daten   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ordner (
            postfach TEXT NOT NULL,
            id       TEXT NOT NULL,
            eltern   TEXT,
            name     TEXT NOT NULL,
            PRIMARY KEY (postfach, id)
        );
        CREATE TABLE IF NOT EXISTS entwuerfe (
            benutzer TEXT NOT NULL,
            id       TEXT NOT NULL,
            daten    TEXT NOT NULL,
            ts       TEXT NOT NULL,
            PRIMARY KEY (benutzer, id)
        );
        """)
    # Spalten, die nach der ersten Fassung dazukamen (Bestandsdatenbanken).
    vorhanden = {z[1] for z in conn.execute("PRAGMA table_info(mails)")}
    for spalte in _MAIL_ZUSATZ:
        if spalte not in vorhanden:
            conn.execute(f"ALTER TABLE mails ADD COLUMN {spalte}")
    if "wk" not in {z[1] for z in conn.execute("PRAGMA table_info(ordner)")}:
        conn.execute("ALTER TABLE ordner ADD COLUMN wk TEXT")
    return conn


# Kopfdaten für die Liste im Webmail — beim ersten Lesen aus dem MIME
# nachgetragen (notbetrieb_lesen._kopf_nachtragen), damit Abgleich und lokale
# Ablage nichts davon wissen müssen.
_MAIL_ZUSATZ = ("betreff TEXT", "von TEXT", "an TEXT", "anhang INTEGER", "text TEXT")
_MAIL_ZUSATZ = tuple(z.split()[0] for z in _MAIL_ZUSATZ)

# Bekannte Ordner, deren Kennung je Postfach gemerkt wird: Lokale Post wird
# unter dem bekannten Namen abgelegt („inbox“), Exchange-Kopien unter der
# Ordner-Id — erst die Zuordnung zeigt beide im selben Ordner.
WK_ORDNER = ("inbox", "sentitems", "junkemail")


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


def _grenze(tage: int, jetzt: datetime | None = None) -> datetime:
    return (jetzt or datetime.now(timezone.utc)) - timedelta(days=tage)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _zeit(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat((text or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def lokal_ablegen(postfach: str, ordner: str, mime: bytes,
                  empfangen: datetime | None = None, gelesen: bool = False) -> str:
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
            " VALUES (?,?,?,?,?,?,?,?)",
            (postfach.lower(), graph_id, ordner, imid,
             _iso(empfangen or datetime.now(timezone.utc)), 1 if gelesen else 0, str(pfad), LOKAL))
    return imid


def lokal_vorhanden(postfach: str, ordner: str, imid: str) -> bool:
    """Liegt diese im Notbetrieb entstandene Mail schon im Ordner (Message-ID)?"""
    imid = (imid or "").strip()
    if not imid or not _db_pfad().exists():
        return False
    with _lock, _conn() as conn:
        return conn.execute("SELECT 1 FROM mails WHERE postfach=? AND ordner=? AND imid=? AND herkunft=?",
                            (postfach.lower(), ordner, imid, LOKAL)).fetchone() is not None


# ── Abgleich von Exchange ───────────────────────────────────────────────────

UMFANG_ALLE, UMFANG_AKTIVIERTE = "alle", "aktivierte"


def umfang() -> str:
    return UMFANG_AKTIVIERTE if settings_store.get("NOTBETRIEB_UMFANG") == UMFANG_AKTIVIERTE else UMFANG_ALLE


def _postfaecher() -> list[tuple[str, str]]:
    """(config_key, adresse) je Postfach im Schatten — EINE Auswahl für
    Abgleich, Aufbewahrung, Platzmessung und Anzeige.

    Umfang „alle“ (Vorgabe): jedes Benutzer- und geteilte Postfach aus dem
    Rechte-Abbild. Der Notbetrieb soll ALLEN Mitarbeitern helfen, nicht nur
    denen, deren Signatur das Gateway setzt. Solange noch kein Abbild gelesen
    ist, gilt die engere Auswahl — sonst stünde der Schatten bis zum ersten
    Rechte-Abgleich leer.

    Umfang „aktivierte“: die im Gateway aktivierten (sig oder smime, wie der
    Abwesenheits-Abgleich) und jedes Postfach, auf das eines von ihnen
    Vollzugriff hat — sonst stünde ein geteiltes Postfach im Notbetrieb leer da.

    Schlüssel ist der aus MAILBOX_CONFIG (Fenster-Regeln je Postfach kennen
    ihn), sonst die Adresse."""
    import notbetrieb_rechte
    out = []
    for key, cfg in (settings_store.get("MAILBOX_CONFIG") or {}).items():
        if isinstance(cfg, dict) and (cfg.get("sig") or cfg.get("smime")):
            upn = (cfg.get("primary") or key or "").strip().lower()
            if "@" in upn:
                out.append((key, upn))
    bekannt = {upn for _, upn in out}
    if umfang() == UMFANG_ALLE:
        alle = notbetrieb_rechte.alle_postfaecher()
        if alle:
            schluessel = {}
            for key, cfg in (settings_store.get("MAILBOX_CONFIG") or {}).items():
                if isinstance(cfg, dict):
                    schluessel[(cfg.get("primary") or key or "").strip().lower()] = key
            return [(schluessel.get(pf, pf), pf) for pf in alle]
    out += [(pf, pf) for pf in notbetrieb_rechte.zusatz_postfaecher(sorted(bekannt)) if pf not in bekannt]
    return out


def _ordner_liste(client: httpx.Client, token: str, postfach: str) -> list[dict]:
    """Alle Mailordner samt Unterordnern (Graph: mailFolders + childFolders)."""
    hdr = {"Authorization": f"Bearer {token}"}
    sel = "?$select=id,displayName,parentFolderId,childFolderCount&$top=100"
    ergebnis: list[dict] = []
    offen = [f"{GRAPH}/users/{postfach}/mailFolders{sel}"]
    while offen:
        url = offen.pop()
        while url:
            resp = client.get(url, headers=hdr)
            if resp.status_code != 200:
                raise RuntimeError(f"Ordnerliste: HTTP {resp.status_code} — {resp.text[:200]}")
            daten = resp.json()
            for f in daten.get("value") or []:
                ergebnis.append(f)
                if f.get("childFolderCount"):
                    offen.append(f"{GRAPH}/users/{postfach}/mailFolders/{f['id']}/childFolders{sel}")
            url = daten.get("@odata.nextLink") or ""
    return ergebnis


def _ordner_abgleichen(client: httpx.Client, token: str, postfach: str, zaehlung: dict) -> list[str]:
    """Ordnerbaum nachführen. Verschwundene Ordner verlieren ihre Exchange-Kopien
    (lokale Post bleibt — sie wird beim Rückspielen ohnehin neu zugeordnet)."""
    liste = _ordner_liste(client, token, postfach)
    ids = [f["id"] for f in liste if f.get("id")]
    wk = {}
    for name in WK_ORDNER:
        r = client.get(f"{GRAPH}/users/{postfach}/mailFolders/{name}?$select=id",
                       headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 200 and (r.json() or {}).get("id"):
            wk[r.json()["id"]] = name
    with _lock, _conn() as conn:
        alt = {z["id"] for z in conn.execute("SELECT id FROM ordner WHERE postfach=?", (postfach,))}
        conn.executemany("INSERT OR REPLACE INTO ordner (postfach, id, eltern, name, wk) VALUES (?,?,?,?,?)",
                         [(postfach, f["id"], f.get("parentFolderId") or "", f.get("displayName") or "",
                           wk.get(f["id"]))
                          for f in liste if f.get("id")])
        weg = alt - set(ids)
        dateien = []
        for oid in weg:
            dateien += [z["datei"] for z in conn.execute(
                "SELECT datei FROM mails WHERE postfach=? AND ordner=? AND herkunft=?", (postfach, oid, EXO))]
            conn.execute("DELETE FROM mails WHERE postfach=? AND ordner=? AND herkunft=?", (postfach, oid, EXO))
            conn.execute("DELETE FROM ordner WHERE postfach=? AND id=?", (postfach, oid))
            conn.execute("DELETE FROM delta WHERE postfach=? AND ordner=?", (postfach, oid))
    for d in dateien:
        _datei_weg(d)
    zaehlung["entfernt"] += len(dateien)
    return ids


def _abgleich_ordner(client: httpx.Client, token: str, postfach: str, ordner: str,
                     tage: int, zaehlung: dict) -> None:
    hdr = {"Authorization": f"Bearer {token}", "Prefer": "odata.maxpagesize=50"}
    with _lock, _conn() as conn:
        zeile = conn.execute("SELECT link, tage FROM delta WHERE postfach=? AND ordner=?",
                             (postfach, ordner)).fetchone()
    if zeile and tage > zeile["tage"]:
        # Fenster vergrößert: Der gemerkte Link trägt den alten Filter und würde
        # die ältere Post nie liefern. Neu aufsetzen (Einfügungen sind Upserts).
        zeile = None
    grenze = _grenze(tage)
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
            return _abgleich_ordner(client, token, postfach, ordner, tage, zaehlung)
        if resp.status_code != 200:
            raise RuntimeError(f"Delta {ordner}: HTTP {resp.status_code} — {resp.text[:200]}")
        daten = resp.json()
        for m in daten.get("value") or []:
            gid = m.get("id") or ""
            if not gid:
                continue
            if "@removed" in m:
                # Gelöscht ODER in einen anderen Ordner verschoben — dort kommt
                # sie als neu an (Graph-Doku, message: delta).
                with _lock, _conn() as conn:
                    alt = conn.execute(
                        "SELECT datei FROM mails WHERE postfach=? AND graph_id=? AND ordner=? AND herkunft=?",
                        (postfach, gid, ordner, EXO)).fetchone()
                    if alt:
                        conn.execute("DELETE FROM mails WHERE postfach=? AND graph_id=?", (postfach, gid))
                if alt:
                    _datei_weg(alt["datei"])
                    zaehlung["entfernt"] += 1
                continue
            empfangen = _zeit(m.get("receivedDateTime") or "")
            with _lock, _conn() as conn:
                da = conn.execute("SELECT ordner FROM mails WHERE postfach=? AND graph_id=?",
                                  (postfach, gid)).fetchone()
                if da:
                    conn.execute("UPDATE mails SET gelesen=?, ordner=? WHERE postfach=? AND graph_id=?",
                                 (1 if m.get("isRead") else 0, ordner, postfach, gid))
                    continue
                # Zurückgespielte lokale Post kommt mit neuer Graph-ID wieder an.
                # Die lokale Zeile wird zur Exchange-Kopie, statt dass die Mail
                # doppelt in der Kopie steht (exohv 14.11.2026: 5 von 5 doppelt).
                imid = m.get("internetMessageId") or ""
                # Erst im selben Ordner suchen: Eine Mail an sich selbst liegt mit
                # derselben Message-ID im Posteingang UND unter Gesendet.
                lok = conn.execute(
                    "SELECT graph_id FROM mails WHERE postfach=? AND imid=? AND herkunft=?"
                    " ORDER BY (ordner=? OR ordner=(SELECT wk FROM ordner WHERE postfach=? AND id=?)) DESC"
                    " LIMIT 1", (postfach, imid, LOKAL, ordner, postfach, ordner)).fetchone() if imid else None
                if lok:
                    conn.execute(
                        "UPDATE mails SET graph_id=?, ordner=?, gelesen=?, herkunft=?,"
                        " zurueck=COALESCE(zurueck, ?) WHERE postfach=? AND graph_id=?",
                        (gid, ordner, 1 if m.get("isRead") else 0, EXO,
                         f"vorhanden {_iso(datetime.now(timezone.utc))}", postfach, lok["graph_id"]))
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
                conn.execute("INSERT OR REPLACE INTO delta (postfach, ordner, link, tage) VALUES (?,?,?,?)",
                             (postfach, ordner, ende, tage))


def aufbewahrung_durchsetzen(jetzt: datetime | None = None) -> int:
    """Kopien außerhalb des Fensters ihres Postfachs löschen — und die
    Exchange-Kopien von Postfächern, die nicht mehr aktiviert sind. Lokale Post
    erst, wenn sie zurückgespielt ist (siehe Modul-Docstring). Gibt die Anzahl
    zurück."""
    if not _db_pfad().exists():
        return 0
    grenzen = {upn: _iso(_grenze(fenster_fuer(key, upn)[0]["mail"], jetzt)) for key, upn in _postfaecher()}
    weg: list = []
    with _lock, _conn() as conn:
        for z in conn.execute("SELECT postfach, graph_id, datei, empfangen, herkunft, zurueck FROM mails"):
            darf = z["herkunft"] == EXO or z["zurueck"] is not None
            grenze = grenzen.get(z["postfach"])
            if darf and (grenze is None or z["empfangen"] < grenze):
                weg.append(z)
        conn.executemany("DELETE FROM mails WHERE postfach=? AND graph_id=?",
                         [(z["postfach"], z["graph_id"]) for z in weg])
        bekannt = list(grenzen)
        frage = ",".join("?" * len(bekannt)) or "''"
        conn.execute(f"DELETE FROM ordner WHERE postfach NOT IN ({frage})", bekannt)
        conn.execute(f"DELETE FROM delta WHERE postfach NOT IN ({frage})", bekannt)
    for z in weg:
        _datei_weg(z["datei"])
    return len(weg)


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
        for key, pf in postfaecher:
            fenster = fenster_fuer(key, pf)[0]
            try:
                for ordner in _ordner_abgleichen(client, token, pf, zaehlung):
                    _abgleich_ordner(client, token, pf, ordner, fenster["mail"], zaehlung)
            except Exception as exc:                               # noqa: BLE001
                zaehlung["postfaecher_fehler"] += 1
                log.warning("Notbetrieb: Abgleich %s fehlgeschlagen: %s", pf, exc)
            # Kalender und Kontakte (lesend) — ein Fehler dort stört die Mail nicht.
            import notbetrieb_pim
            try:
                notbetrieb_pim.kalender_abgleichen(client, token, pf, fenster["kal_zurueck"], fenster["kal_vor"])
                notbetrieb_pim.kontakte_abgleichen(client, token, pf)
            except Exception as exc:                               # noqa: BLE001
                zaehlung["pim_fehler"] = zaehlung.get("pim_fehler", 0) + 1
                log.warning("Notbetrieb: Kalender/Kontakte %s fehlgeschlagen: %s", pf, exc)
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


# Anhänge bis zu dieser Gesamtgröße gehen mit der Anlage, größere einzeln per
# Upload-Sitzung. Gemessen exohv 14.11.2026: 3,5 MB beim Anlegen angenommen,
# Upload-Sitzung (5 MB in 3-MB-Stücken) an einer Nicht-Entwurf-Mail ebenfalls.
_ANLAGE_BUDGET = 3 * 1024 * 1024
_STUECK = 3 * 1024 * 1024


def _graph_zeit(dt: datetime | None) -> str | None:
    return _iso(dt) if dt else None


def _adressen(*koepfe) -> list[dict]:
    return [{"emailAddress": {"address": a, **({"name": n} if n else {})}}
            for n, a in email.utils.getaddresses([str(k) for k in koepfe if k]) if a]


def _als_graph(mime: bytes, gelesen: bool, empfangen: datetime | None) -> tuple[dict, list]:
    """Lokale Mail als Graph-Nachricht (JSON) — dazu die Anhänge, die nicht in
    die Anlage passen.

    Warum nicht MIME: Eine per MIME angelegte Nachricht ist IMMER ein Entwurf,
    und `isDraft` ist danach nicht mehr änderbar (Graph v1.0 „Update message“;
    gemessen 5 von 5, der PATCH antwortet trotzdem 200). Nur bei der Anlage
    per JSON wirkt PR_MESSAGE_FLAGS (0x0E07) — ohne MSGFLAG_UNSENT ist die
    Nachricht kein Entwurf (gemessen exohv 14.11.2026). Lokale Post baut
    ausschließlich das Webmail (Text, Anhänge, weitergeleitete Mail), der
    JSON-Nachbau verliert dabei nichts Wesentliches.

    Message-ID über `internetMessageId` (beim Anlegen setzbar, per $filter
    auffindbar — die Idempotenz hängt daran). In-Reply-To/References als
    MAPI-Eigenschaften, Graph nimmt als Kopfzeilen nur `x-`."""
    msg = email.message_from_bytes(mime, policy=email.policy.default)
    rumpf = msg.get_body(preferencelist=("html", "plain"))
    html = rumpf is not None and rumpf.get_content_subtype() == "html"
    props = [{"id": "Integer 0x0E07", "value": "1" if gelesen else "0"}]
    for kennung, wert in (("SystemTime 0x0E06", _graph_zeit(empfangen)),
                          ("SystemTime 0x0039", _graph_zeit(_kopf_zeit(msg.get("Date")))),
                          ("String 0x1042", str(msg.get("In-Reply-To") or "").strip()),
                          ("String 0x1039", str(msg.get("References") or "").strip())):
        if wert:
            props.append({"id": kennung, "value": wert})
    n = {"subject": str(msg.get("Subject") or ""),
         "body": {"contentType": "HTML" if html else "Text",
                  "content": rumpf.get_content().replace("\r\n", "\n") if rumpf else ""},
         "internetMessageId": str(msg.get("Message-ID") or "").strip(),
         "toRecipients": _adressen(*msg.get_all("To", [])),
         "ccRecipients": _adressen(*msg.get_all("Cc", [])),
         "bccRecipients": _adressen(*msg.get_all("Bcc", [])),
         "replyTo": _adressen(*msg.get_all("Reply-To", [])),
         "singleValueExtendedProperties": props}
    for feld, kopf in (("from", "From"), ("sender", "Sender")):
        a = _adressen(msg.get(kopf))
        if a:
            n[feld] = a[0]
    anlage, gross, budget = [], [], _ANLAGE_BUDGET
    for teil in msg.iter_attachments():
        typ = teil.get_content_type()
        if typ == "message/rfc822":
            daten = teil.get_payload(0).as_bytes(policy=email.policy.SMTP)
        else:
            daten = teil.get_payload(decode=True) or b""
        name = teil.get_filename() or ("nachricht.eml" if typ == "message/rfc822" else "anhang")
        if len(daten) > budget:
            gross.append((name, typ, daten))
            continue
        budget -= len(daten)
        a = {"@odata.type": "#microsoft.graph.fileAttachment", "name": name, "contentType": typ,
             "contentBytes": base64.b64encode(daten).decode()}
        cid = str(teil.get("Content-ID") or "").strip().strip("<>")
        if cid:
            a["contentId"] = cid
            a["isInline"] = teil.get_content_disposition() == "inline"
        anlage.append(a)
    if anlage:
        n["attachments"] = anlage
    return n, gross


def _kopf_zeit(wert) -> datetime | None:
    try:
        return email.utils.parsedate_to_datetime(str(wert)) if wert else None
    except (TypeError, ValueError):
        return None


def _gross_anhaengen(client, token: str, pf: str, neu_id: str, name: str, typ: str, daten: bytes) -> None:
    """Großen Anhang per Upload-Sitzung nachreichen (Graph: attachment createUploadSession)."""
    s = client.post(f"{GRAPH}/users/{pf}/messages/{neu_id}/attachments/createUploadSession",
                    json={"AttachmentItem": {"attachmentType": "file", "name": name, "size": len(daten),
                                             "contentType": typ}},
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    timeout=60)
    if s.status_code not in (200, 201):
        raise RuntimeError(f"Upload-Sitzung HTTP {s.status_code}")
    url = s.json()["uploadUrl"]
    for pos in range(0, len(daten), _STUECK):
        stueck = daten[pos:pos + _STUECK]
        # Die Upload-URL ist vorab berechtigt — kein Authorization-Kopf (Graph-Doku).
        r = client.put(url, content=stueck, timeout=120, headers={
            "Content-Length": str(len(stueck)),
            "Content-Range": f"bytes {pos}-{pos + len(stueck) - 1}/{len(daten)}"})
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Upload HTTP {r.status_code}")


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
                # Im Zielordner suchen, nicht im ganzen Postfach: Eine Mail an sich
                # selbst hat Posteingang und Gesendet mit derselben Message-ID —
                # sonst gälte die zweite als schon vorhanden.
                such = client.get(
                    f"{GRAPH}/users/{pf}/mailFolders/{z['ordner']}/messages",
                    params={"$filter": f"internetMessageId eq '{_odata_text(imid)}'", "$select": "id"},
                    headers={"Authorization": f"Bearer {token}"})
                if such.status_code != 200:
                    raise RuntimeError(f"Suche HTTP {such.status_code}")
                if such.json().get("value"):
                    _erledigt(z, "vorhanden")
                    zaehlung["schon_da"] += 1
                    continue
                nachricht, gross = _als_graph(Path(z["datei"]).read_bytes(), bool(z["gelesen"]),
                                              _zeit(z["empfangen"]))
                resp = client.post(f"{GRAPH}/users/{pf}/mailFolders/{z['ordner']}/messages",
                                   json=nachricht, headers=json_hdr, timeout=120)
                if resp.status_code not in (200, 201):
                    raise RuntimeError(f"Einspielen HTTP {resp.status_code} — {resp.text[:200]}")
                neu_id = resp.json().get("id", "")
                try:
                    for name, typ, daten in gross:
                        _gross_anhaengen(client, token, pf, neu_id, name, typ, daten)
                except Exception:
                    # Ohne Löschen fände der nächste Lauf die Mail über die
                    # Message-ID und hielte sie für erledigt — ohne den Anhang.
                    client.delete(f"{GRAPH}/users/{pf}/messages/{neu_id}",
                                  headers={"Authorization": f"Bearer {token}"})
                    raise
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


# ── Profile für die Signatur ────────────────────────────────────────────────

def profile_sichern() -> dict:
    """Entra-Profile aller Benutzer sichern — dieselben Felder, die die Signatur
    liest (`graph_client._build_select_fields`). Im Normalbetrieb kommt Name,
    Telefon usw. live aus Graph; fällt Microsoft aus, wäre die Signatur leer.
    `graph_client._graph_user_data` fällt dann auf diese Fassung zurück.
    Schlüssel: Mail, UPN und jede SMTP-Proxyadresse (wie die Nachschlage-Reihenfolge
    dort). Bei einem Fehler bleibt der alte Stand."""
    token = graph_client._acquire_token()
    if not token:
        return {"ok": False, "fehler": "kein Graph-Zugang"}
    felder = graph_client._build_select_fields() + ",proxyAddresses"
    zeilen: dict[str, str] = {}
    url = f"{GRAPH}/users?$select={felder}&$top=999"
    try:
        with httpx.Client(timeout=60) as client:
            while url:
                r = client.get(url, headers={"Authorization": f"Bearer {token}"})
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                d = r.json()
                for u in d.get("value") or []:
                    adressen = {u.get("mail") or "", u.get("userPrincipalName") or ""}
                    adressen |= {a.split(":", 1)[1] for a in (u.get("proxyAddresses") or [])
                                 if a.lower().startswith("smtp:")}
                    daten = json.dumps({k: v for k, v in u.items() if k != "proxyAddresses"})
                    for a in adressen:
                        if a:
                            zeilen[a.lower()] = daten
                url = d.get("@odata.nextLink") or ""
    except Exception as exc:                                        # noqa: BLE001
        log.warning("Notbetrieb: Profile nicht gesichert: %s", exc)
        return {"ok": False, "fehler": str(exc)}
    with _lock, _conn() as conn:
        conn.execute("DELETE FROM profile")
        conn.executemany("INSERT INTO profile (adresse, daten) VALUES (?,?)", zeilen.items())
        conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('profile_stand', ?)",
                     (_iso(datetime.now(timezone.utc)),))
    log.info("Notbetrieb: %s Profiladressen gesichert", len(zeilen))
    return {"ok": True, "adressen": len(zeilen)}


def profil(adresse: str) -> dict | None:
    """Gesichertes Profil (Graph-Form) oder None."""
    if not _db_pfad().exists():
        return None
    with _lock, _conn() as conn:
        z = conn.execute("SELECT daten FROM profile WHERE adresse=?",
                         ((adresse or "").strip().lower(),)).fetchone()
    return json.loads(z["daten"]) if z else None


# ── Bestand und Löschen ─────────────────────────────────────────────────────

def _bestand() -> dict:
    if not _db_pfad().exists():
        return {"mails": 0, "lokal_offen": 0, "postfaecher_im_schatten": 0,
                "profile": 0, "profile_stand": None}
    with _lock, _conn() as conn:
        mails = conn.execute("SELECT COUNT(*) FROM mails").fetchone()[0]
        offen = conn.execute("SELECT COUNT(*) FROM mails WHERE herkunft=? AND zurueck IS NULL",
                             (LOKAL,)).fetchone()[0]
        pf = conn.execute("SELECT COUNT(DISTINCT postfach) FROM mails").fetchone()[0]
        profile = conn.execute("SELECT COUNT(DISTINCT daten) FROM profile").fetchone()[0]
        stand = conn.execute("SELECT v FROM meta WHERE k='profile_stand'").fetchone()
    return {"mails": mails, "lokal_offen": offen, "postfaecher_im_schatten": pf,
            "profile": profile, "profile_stand": stand["v"] if stand else None}


def status() -> dict:
    import notbetrieb_eingang
    return {"aktiv": funktion_aktiv(), "vorhanden": _wurzel().exists(), **_bestand(),
            "ausgerufen": ausgerufen(), "eingang": notbetrieb_eingang.zaehler(),
            "letzter_lauf": settings_store.get(_LAST_KEY) or None,
            "fenster": standard_fenster()}


def postfach_uebersicht() -> list[dict]:
    """Je aktiviertem Postfach: geltendes Fenster samt Herkunft und Bestand."""
    zahlen: dict[str, tuple[int, int]] = {}
    if _db_pfad().exists():
        with _lock, _conn() as conn:
            for z in conn.execute("SELECT postfach, COUNT(*) AS n FROM mails GROUP BY postfach"):
                zahlen[z["postfach"]] = (z["n"], 0)
            for z in conn.execute("SELECT postfach, COUNT(*) AS n FROM ordner GROUP BY postfach"):
                zahlen[z["postfach"]] = (zahlen.get(z["postfach"], (0, 0))[0], z["n"])
    out = []
    for key, upn in _postfaecher():
        fenster, herkunft = fenster_fuer(key, upn)
        mails, ordner = zahlen.get(upn, (0, 0))
        out.append({"postfach": upn, "schluessel": key, **fenster, "herkunft": herkunft,
                    "mails": mails, "ordner": ordner})
    return out


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


# ── Platzbedarf: Was wäre, wenn ─────────────────────────────────────────────
#
# Graph v1.0 kennt keine Ordnergröße (mailFolder hat nur Zähler). Gemessen wird
# deshalb je Mail die Exchange-Größe (MAPI PR_MESSAGE_SIZE, 0x0E08) über alle
# Ordner, nach Empfangstag summiert. Daraus rechnet platz_rechnen() für JEDE
# Fenster-Einstellung den Bedarf — auch für ungespeicherte, damit man vor dem
# Speichern sieht, ob die Platte reicht.

_GROESSE = "Integer 0x0E08"


def _ist_groesse(kennung) -> bool:
    """Ist das die Größen-Eigenschaft? Graph liefert sie NICHT in der angefragten
    Schreibweise zurück, sondern als „Integer 0xe08“ (ohne führende Null, klein;
    gemessen 14.11.2026). Deshalb der Zahlenwert, nicht der Text."""
    typ, _, tag = str(kennung or "").partition(" ")
    try:
        return typ.lower() == "integer" and int(tag, 16) == 0x0E08
    except ValueError:
        return False


MESS_PARALLEL = 4
_messlauf: dict = {"laeuft": False, "fertig": 0, "gesamt": 0}


def _postfach_messen(token: str, upn: str, tage: int, jetzt: datetime) -> dict:
    """Bytes je Empfangstag (0 = heute) für EIN Postfach. Nur Empfangszeit und
    Größe werden geholt; 429 wird einmal je Seite mit Retry-After abgewartet."""
    import random
    import time
    je_tag = [0] * (tage + 1)
    anzahl = 0
    stichprobe: list[tuple[str, int]] = []      # (id, Exchange-Größe), Reservoir
    url = (f"{GRAPH}/users/{upn}/messages?$filter=receivedDateTime+ge+{_iso(_grenze(tage, jetzt))}"
           f"&$select=receivedDateTime&$top=1000"
           f"&$expand=singleValueExtendedProperties($filter=id+eq+'{_GROESSE}')")
    with httpx.Client(timeout=60) as client:
        while url:
            resp = client.get(url, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code == 429:
                time.sleep(min(60, int(resp.headers.get("Retry-After") or 5)))
                resp = client.get(url, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code} — {resp.text[:200]}")
            daten = resp.json()
            for m in daten.get("value") or []:
                zeit = _zeit(m.get("receivedDateTime") or "")
                if zeit is None:
                    continue
                groesse = next((int(e.get("value") or 0)
                                for e in m.get("singleValueExtendedProperties") or []
                                if _ist_groesse(e.get("id"))), 0)
                je_tag[min(tage, max(0, (jetzt - zeit).days))] += groesse
                anzahl += 1
                if groesse and m.get("id"):
                    if len(stichprobe) < _EICH_JE_POSTFACH:
                        stichprobe.append((m["id"], groesse))
                    elif random.random() < _EICH_JE_POSTFACH / anzahl:
                        stichprobe[random.randrange(_EICH_JE_POSTFACH)] = (m["id"], groesse)
            url = daten.get("@odata.nextLink") or ""
    return {"je_tag": je_tag, "mails": anzahl, "stichprobe": stichprobe}


def _eichen(token: str, kandidaten: list[tuple[str, str, int]]) -> tuple[float | None, int]:
    """MIME-Größe gegen Exchange-Größe an einer Stichprobe. Gewichtet nach Bytes
    (Summe/Summe), weil es um den Gesamtbedarf geht, nicht um die typische Mail."""
    exo = mime = n = 0
    with httpx.Client(timeout=60) as client:
        for upn, mid, groesse in kandidaten:
            r = client.get(f"{GRAPH}/users/{upn}/messages/{mid}/$value",
                           headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 200 and r.content:
                exo += groesse
                mime += len(r.content)
                n += 1
    return (mime / exo if exo else None), n


def platz_messen(tage: int = MESS_TAGE) -> dict:
    """Alle aktivierten Postfächer messen (nebenläufig) und das Ergebnis merken.
    Fortschritt in `messlauf()` für die Oberfläche."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    postfaecher = _postfaecher()
    token = graph_client._acquire_token() if postfaecher else None
    if postfaecher and not token:
        return {"ok": False, "error": "Kein Graph-Zugang."}
    jetzt = datetime.now(timezone.utc)
    _messlauf.update(laeuft=True, fertig=0, gesamt=len(postfaecher))
    ergebnis: dict[str, dict] = {}
    fehler = 0
    try:
        with ThreadPoolExecutor(max_workers=MESS_PARALLEL) as pool:
            auftraege = {pool.submit(_postfach_messen, token, upn, tage, jetzt): (key, upn)
                         for key, upn in postfaecher}
            for f in as_completed(auftraege):
                key, upn = auftraege[f]
                try:
                    r = f.result()
                    ergebnis[upn] = {"schluessel": key, **r}
                except Exception as exc:                            # noqa: BLE001
                    fehler += 1
                    log.warning("Notbetrieb: Platzmessung %s fehlgeschlagen: %s", upn, exc)
                _messlauf["fertig"] += 1
        import random
        kandidaten = [(upn, mid, g) for upn, m in ergebnis.items() for mid, g in m.pop("stichprobe", [])]
        random.shuffle(kandidaten)
        faktor, eich_n = _eichen(token, kandidaten[:EICH_PROBEN]) if kandidaten else (None, 0)
    finally:
        _messlauf["laeuft"] = False
    dauer = (datetime.now(timezone.utc) - jetzt).total_seconds()
    messung = {"ok": True, "tage": tage, "ts": jetzt.isoformat(), "postfaecher": ergebnis,
               "fehler": fehler, "dauer_s": round(dauer, 1),
               "mails": sum(m["mails"] for m in ergebnis.values()),
               "faktor": round(faktor, 3) if faktor else None, "eich_n": eich_n}
    log.info("Notbetrieb: Platzmessung %s Postfächer, %s Mails, %s Fehler in %.1f s, Faktor %s (n=%s)",
             len(ergebnis), messung["mails"], fehler, dauer, messung["faktor"], eich_n)
    settings_store.force_update({_MESS_KEY: messung})
    return messung


def messlauf() -> dict:
    return dict(_messlauf)


def _ordner_bytes(pfad: Path) -> int:
    if not pfad.exists():
        return 0
    return sum(f.stat().st_size for f in pfad.rglob("*") if f.is_file())


def platz_rechnen(regeln: list | None = None, std: dict | None = None,
                  messung: dict | None = None) -> dict:
    """Bedarf für eine (auch ungespeicherte) Fenster-Einstellung.

    Innerhalb der Messspanne zählen die gemessenen Tage; darüber hinaus wird
    mit dem Tagesmittel der Messung hochgerechnet (`hochgerechnet` je Postfach).
    Verglichen wird mit dem freien Platz PLUS dem, was der Schatten heute schon
    belegt — der wird ja ersetzt, nicht ergänzt."""
    messung = messung if messung is not None else (settings_store.get(_MESS_KEY) or {})
    pf = messung.get("postfaecher") or {}
    mess_tage = int(messung.get("tage") or 0)
    belegt = _ordner_bytes(_wurzel())
    try:
        frei = shutil.disk_usage(_wurzel() if _wurzel().exists() else config.DATA_DIR).free
    except OSError:
        frei = 0
    faktor = messung.get("faktor") or MIME_FAKTOR_OHNE_EICHUNG
    zeilen = []
    summe = 0
    nach_herkunft: dict[str, int] = defaultdict(int)
    for upn, m in sorted(pf.items()):
        fenster, herkunft = fenster_fuer(m.get("schluessel") or upn, upn, regeln, std)
        tage = fenster["mail"]
        je_tag = list(m.get("je_tag") or [])
        gemessen = sum(je_tag[: tage + 1])
        hoch = 0.0
        if tage > mess_tage and mess_tage:
            hoch = sum(je_tag) / (mess_tage + 1) * (tage - mess_tage)
        exo_bytes = gemessen + hoch
        bedarf = int(exo_bytes * faktor)
        summe += bedarf
        nach_herkunft[herkunft] += bedarf
        zeilen.append({"postfach": upn, "tage": tage, "herkunft": herkunft, "bedarf": bedarf,
                       "hochgerechnet": bool(hoch)})
    verfuegbar = frei + belegt
    return {"ok": True, "gemessen": bool(pf), "mess_ts": messung.get("ts"), "mess_tage": mess_tage,
            "mess_dauer_s": messung.get("dauer_s"), "mess_mails": messung.get("mails"),
            "mess_fehler": messung.get("fehler"),
            "bedarf": summe, "frei": frei, "belegt": belegt, "verfuegbar": verfuegbar,
            "anteil": (summe / verfuegbar) if verfuegbar else None,
            "nach_herkunft": dict(nach_herkunft), "postfaecher": zeilen,
            "mime_faktor": faktor, "geeicht": bool(messung.get("faktor")),
            "eich_n": messung.get("eich_n") or 0}
