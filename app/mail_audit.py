"""
Per-message audit log stored in SQLite.

Each processed SMTP transaction writes one row. The log is queried by the
dashboard to show details behind the aggregate stats numbers.

Storage: /app/data/mail_audit.db
Retention: configurable via LOG_RETENTION_DAYS (default 90), pruned on startup.
"""

import json
import logging
import sqlite3
import secure_io
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
import config

log = logging.getLogger(__name__)

DB_PATH = Path(config.DATA_DIR) / "mail_audit.db"
_lock = threading.Lock()
_initialised = False


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    # SQLite legt die Datei mit der umask des Prozesses an (im Container 644).
    # `harden_tree()` beim Start räumt das auf — eine zur Laufzeit ENTSTEHENDE
    # Datenbank bliebe bis zum nächsten Neustart mitlesbar.
    secure_io.harden_file(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    global _initialised
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock, _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mail_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT    NOT NULL,
                sender        TEXT,
                recipients    TEXT,
                subject       TEXT,
                message_id    TEXT,
                action        TEXT,
                size_bytes    INTEGER,
                processing_ms INTEGER,
                error         TEXT,
                relay_ip      TEXT,
                relay_id      TEXT
            )
        """)
        # Bestands-DBs kennen die Spalten noch nicht — `CREATE TABLE IF NOT EXISTS`
        # legt sie nicht nachtraeglich an. Ohne diese Wanderung schluege jeder
        # INSERT mit relay_ip/relay_id fehl (und der Relay-Filter faende nie etwas).
        # relay_ip = Quell-IP (IP-Relay), relay_id = authentifizierte Sende-Identitaet
        # (Submission 587); je nach Auth-Stufe ist mal das eine, mal das andere die
        # aussagekraeftige Groesse.
        spalten = {r["name"] for r in conn.execute("PRAGMA table_info(mail_log)")}
        if "relay_ip" not in spalten:
            conn.execute("ALTER TABLE mail_log ADD COLUMN relay_ip TEXT")
        if "relay_id" not in spalten:
            conn.execute("ALTER TABLE mail_log ADD COLUMN relay_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ts     ON mail_log(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_action ON mail_log(action)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sender ON mail_log(sender)")
        # Der Relay-Filter fragt "relay_ip gesetzt?" — ein Index darauf haelt die
        # gefilterte Protokollansicht schnell, auch wenn Relay-Post die Ausnahme ist.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_relay  ON mail_log(relay_ip)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS graph_api_calls (
                app_id  TEXT    NOT NULL,
                date    TEXT    NOT NULL,
                hour    INTEGER NOT NULL,
                count   INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (app_id, date, hour)
            )
        """)
    _initialised = True
    log.info("mail_audit: DB ready at %s", DB_PATH)


def log_event(
    *,
    sender: str,
    recipients: list[str],
    subject: str,
    message_id: str,
    action: str,
    size_bytes: int = 0,
    processing_ms: int = 0,
    error: str | None = None,
    relay_ip: str = "",
    relay_id: str = "",
) -> None:
    if not _initialised:
        log.warning("mail_audit: log_event(%s) dropped — DB not yet initialised", action)
        return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with _lock, _conn() as conn:
            conn.execute(
                "INSERT INTO mail_log "
                "(ts, sender, recipients, subject, message_id, action, size_bytes, processing_ms, error, relay_ip, relay_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts,
                    sender,
                    json.dumps(recipients, ensure_ascii=False),
                    subject,
                    message_id,
                    action,
                    size_bytes,
                    processing_ms,
                    error,
                    relay_ip or None,
                    relay_id or None,
                ),
            )
    except Exception as exc:
        log.warning("mail_audit: write failed: %s", exc)


def _utc(lokal: datetime, zone) -> str:
    return lokal.replace(tzinfo=zone).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def zeitraum_utc(date: str, zone=None, stunde: int | None = None) -> tuple[str, str]:
    """UTC-Grenzen [start, ende) von Tag/Monat/Jahr (YYYY-MM-DD | YYYY-MM | YYYY)
    in der ANZEIGE-Zeitzone, wahlweise nur einer lokalen Stunde des Tags.

    Bis v1.12.147 galten hier UTC-Grenzen, während Peak und Stundenansicht
    lokal rechneten — der Klick auf „Mail-Peak 10 Uhr“ zeigte den ganzen
    UTC-Tag. Die Tageszahlen (stats) zählen seit v1.12.148 ebenfalls lokal."""
    if zone is None:
        import settings_store
        zone = settings_store.anzeige_zone()
    if len(date) == 10:
        beginn = datetime.strptime(date, "%Y-%m-%d")
        if stunde is not None:
            beginn = beginn.replace(hour=stunde)
            ende = beginn + timedelta(hours=1)
        else:
            ende = beginn + timedelta(days=1)
    elif len(date) == 7:
        y, m = int(date[:4]), int(date[5:])
        beginn = datetime(y, m, 1)
        ende = datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)
    else:
        beginn, ende = datetime(int(date), 1, 1), datetime(int(date) + 1, 1, 1)
    return _utc(beginn, zone), _utc(ende, zone)


def _add_date_condition(date: str, conditions: list, params: list,
                        stunde: int | None = None) -> None:
    """Zeitbereich (lokal, siehe zeitraum_utc) als Bedingung auf mail_log.ts (UTC)."""
    if len(date) not in (4, 7, 10):
        return
    start, ende = zeitraum_utc(date, stunde=stunde)
    conditions.append("ts >= ? AND ts < ?")
    params += [start, ende]


def _filter(date, date_from, date_to, action, sender, nur_relay, relay_ip,
            stunde=None) -> tuple[str, list]:
    conditions: list[str] = []
    params: list = []
    if date:
        _add_date_condition(date, conditions, params, stunde)
    elif date_from or date_to:
        if date_from:
            conditions.append("ts >= ?")
            params.append(zeitraum_utc(date_from)[0])
        if date_to:
            conditions.append("ts < ?")
            params.append(zeitraum_utc(date_to)[1])
    liste = aktionen(action)
    if len(liste) == 1:
        conditions.append("action = ?")
        params.append(liste[0])
    elif liste:
        conditions.append("action IN (" + ",".join("?" * len(liste)) + ")")
        params += list(liste)
    if sender:
        conditions.append("sender = ?")
        params.append(sender)
    if relay_ip:
        conditions.append("relay_ip = ?")
        params.append(relay_ip)
    elif nur_relay:
        # Relay-Post ist beides: IP-Relay (relay_ip) UND authentifizierte
        # Submission (relay_id). Der relay-fokussierte Filter erfasst beide.
        conditions.append("((relay_ip IS NOT NULL AND relay_ip != '') "
                          "OR (relay_id IS NOT NULL AND relay_id != ''))")
    return ("WHERE " + " AND ".join(conditions)) if conditions else "", params


def query_events(
    *,
    date: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    action: str | None = None,
    sender: str | None = None,
    nur_relay: bool = False,
    relay_ip: str | None = None,
    stunde: int | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    """Return events as a list of dicts, newest first. `action` darf eine
    Gruppe („@verarbeitet“) oder Liste („a,b“) sein; `stunde` (lokal) grenzt
    einen Tag auf eine Stunde ein."""
    if not _initialised:
        return []
    where, params = _filter(date, date_from, date_to, action, sender, nur_relay, relay_ip, stunde)
    params += [limit, offset]
    try:
        with _conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM mail_log {where} ORDER BY ts DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.warning("mail_audit: query failed: %s", exc)
        return []


def count_events(
    *,
    date: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    action: str | None = None,
    sender: str | None = None,
    nur_relay: bool = False,
    relay_ip: str | None = None,
    stunde: int | None = None,
) -> int:
    if not _initialised:
        return 0
    where, params = _filter(date, date_from, date_to, action, sender, nur_relay, relay_ip, stunde)
    try:
        with _conn() as conn:
            return conn.execute(
                f"SELECT COUNT(*) FROM mail_log {where}", params
            ).fetchone()[0]
    except Exception as exc:
        log.warning("mail_audit: count failed: %s", exc)
        return 0


def avg_processing_ms(since_iso: str) -> float | None:
    """Return average processing_ms for rows with ts >= since_iso, or None if no rows."""
    if not _initialised:
        return None
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT AVG(processing_ms) FROM mail_log WHERE ts >= ? AND processing_ms > 0",
                (since_iso,),
            ).fetchone()
        val = row[0] if row else None
        return round(val, 1) if val is not None else None
    except Exception as exc:
        log.warning("mail_audit: avg_processing_ms failed: %s", exc)
        return None


def _lokaler_tag(date: str, zone=None) -> tuple[str, str, object]:
    """UTC-Grenzen [start, ende) des Kalendertags *date* (YYYY-MM-DD) in der
    Anzeige-Zeitzone. mail_log.ts ist UTC; ohne diese Umrechnung erschien die
    Stunde 9 UTC als „9–10 Uhr“, obwohl es in Berlin 10–11 Uhr war."""
    if zone is None:
        import settings_store
        zone = settings_store.anzeige_zone()
    beginn = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=zone)
    ende = (beginn + timedelta(days=1)).replace(tzinfo=zone)
    f = "%Y-%m-%dT%H:%M:%SZ"
    return (beginn.astimezone(timezone.utc).strftime(f),
            ende.astimezone(timezone.utc).strftime(f), zone)


def _stunden_lokal(date: str, zone=None) -> dict[int, list]:
    """Mails des lokalen Tags *date*, nach LOKALER Stunde gruppiert:
    {stunde: [processing_ms, …]}."""
    start, ende, zone = _lokaler_tag(date, zone)
    with _conn() as conn:
        rows = conn.execute(
            "SELECT ts, processing_ms FROM mail_log WHERE ts >= ? AND ts < ?",
            (start, ende),
        ).fetchall()
    stunden: dict[int, list] = {}
    for ts, ms in rows:
        try:
            t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        stunden.setdefault(t.astimezone(zone).hour, []).append(ms)
    return stunden


def peak_hour(date: str, zone=None) -> tuple[str, int] | None:
    """(Stunde "HH", Anzahl) der geschäftigsten LOKALEN Stunde am lokalen Tag
    *date* (YYYY-MM-DD, Anzeige-Zeitzone), oder None."""
    if not _initialised:
        return None
    try:
        stunden = _stunden_lokal(date, zone)
        if not stunden:
            return None
        h = max(sorted(stunden), key=lambda k: len(stunden[k]))
        return (f"{h:02d}", len(stunden[h]))
    except Exception as exc:
        log.warning("mail_audit: peak_hour failed: %s", exc)
        return None


def flush_graph_calls(records: list[tuple]) -> None:
    """Upsert Graph API call counts. records = [(app_id, date, hour, count)]."""
    if not _initialised or not records:
        return
    try:
        with _lock, _conn() as conn:
            conn.executemany(
                "INSERT INTO graph_api_calls (app_id, date, hour, count) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(app_id, date, hour) DO UPDATE SET count = excluded.count",
                records,
            )
    except Exception as exc:
        log.warning("mail_audit: flush_graph_calls failed: %s", exc)


def get_graph_calls_hours(app_id: str, date: str) -> list[int]:
    """Return 24-element list of call counts for app_id on date."""
    if not _initialised:
        return [0] * 24
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT hour, count FROM graph_api_calls WHERE app_id=? AND date=?",
                (app_id, date),
            ).fetchall()
        result = [0] * 24
        for r in rows:
            result[r["hour"]] = r["count"]
        return result
    except Exception as exc:
        log.warning("mail_audit: get_graph_calls_hours failed: %s", exc)
        return [0] * 24


def get_graph_calls_range(app_id: str, days: int) -> list[dict]:
    """Return [{date, total}] for the last N days, oldest first."""
    if not _initialised:
        return []
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        with _conn() as conn:
            rows = conn.execute(
                "SELECT date, SUM(count) AS total FROM graph_api_calls "
                "WHERE app_id=? AND date >= ? GROUP BY date ORDER BY date",
                (app_id, cutoff),
            ).fetchall()
        return [{"date": r["date"], "total": r["total"]} for r in rows]
    except Exception as exc:
        log.warning("mail_audit: get_graph_calls_range failed: %s", exc)
        return []


def get_mail_hourly(date: str, zone=None) -> list[dict]:
    """Stundenwerte aus mail_log für den lokalen Tag *date* (Anzeige-Zeitzone),
    Stunden in Ortszeit."""
    if not _initialised:
        return []
    try:
        stunden = _stunden_lokal(date, zone)
        aus = []
        for h in sorted(stunden):
            ms = [m for m in stunden[h] if m is not None]
            pos = [m for m in ms if m > 0]
            aus.append({"hour": f"{h:02d}", "count": len(stunden[h]),
                        "avg_ms": int(sum(pos) / len(pos)) if pos else None,
                        "peak_ms": max(ms) if ms else None})
        return aus
    except Exception as exc:
        log.warning("mail_audit: get_mail_hourly failed: %s", exc)
        return []


# Kategorien, deren Detailzeilen laenger aufbewahrt werden als der uebrige,
# hochvolumige Postverkehr — genau die, hinter denen man in der Uebersicht spaeter
# noch nachsehen will: Krypto (S/MIME), Fehler/Abweisungen und die Warteschlange.
# Reine HTML-Signatur ("signed") und Durchlaeufe zaehlen NICHT dazu (Massenware,
# kurze Aufbewahrung). Diese Zeilen sind niedrigvolumig; sie lange zu halten kostet
# fast nichts (grobe Schaetzung: 100 Krypto-Mails/Tag * ~200 B * 730 Tage ~= 15 MB).
LANGZEIT_ACTIONS = (
    "smime_encrypted", "smime_signed", "smime_decrypted",     # Krypto
    "smime_signiert_empfangen", "smime_signiert_verschluesselt",
    "error", "fallback", "tenant_fremd", "relay_abgelehnt",   # Fehler/Abweisung
    "smime_encrypt_fehler", "smime_decrypt_fehler", "tenant_fehler", "ohne_zertifikat",
    "zustellfehler",
    "held",                                                    # Warteschlange
)


# Gruppen von Aktionen hinter je EINER Zahl der Übersicht (seit v1.12.148).
# Zahl und Klickliste meinen damit dieselben Mails — bis dahin öffnete
# „Verarbeitet“ ALLE Aktionen, „S/MIME signiert“ ließ signiert+verschlüsselte
# aus, „Fehler“ zeigte Zeilen, die nicht gezählt waren. Die Zähler dazu stehen
# in stats (siehe test_uebersicht_zahlen.py: je Gruppe Zähler ↔ Aktionen).
AKTIONSGRUPPEN: dict[str, tuple[str, ...]] = {
    # bearbeitet oder geprüft: Signatur, S/MIME, Portal, eingehende Prüfung
    "@verarbeitet": ("signed", "smime_signed", "smime_encrypted", "smime_signiert_verschluesselt",
                     "smime_decrypted", "smime_signiert_empfangen", "portal", "zustellfehler"),
    "@smime_signiert": ("smime_signed", "smime_signiert_verschluesselt"),
    "@smime_verschluesselt": ("smime_encrypted", "smime_signiert_verschluesselt"),
    # echte Ausfälle des Gateways (nicht: Bedienfälle wie fehlendes Zertifikat)
    # „fallback“ NICHT: ein Fallback-Fall schreibt zuerst „error“, dann
    # „fallback“ — die Mail stünde doppelt in der Liste.
    "@fehler": ("error", "smime_encrypt_fehler", "smime_decrypt_fehler",
                "tenant_fehler", "zustellfehler"),
}


def aktionen(action: str | None) -> tuple[str, ...]:
    """„@gruppe“ → ihre Aktionen; „a,b“ → (a, b); einzelne Aktion → (a,)."""
    if not action:
        return ()
    if action in AKTIONSGRUPPEN:
        return AKTIONSGRUPPEN[action]
    return tuple(a for a in action.split(",") if a)


def _cutoff_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def prune_old_events(retention_days: int = 90, langzeit_days: int | None = None) -> int:
    """Alte Detailzeilen loeschen. Hochvolumige Zeilen (normale Signatur, Durchlauf)
    nach *retention_days*; die verdichtungswuerdigen Kategorien (LANGZEIT_ACTIONS)
    erst nach *langzeit_days* — damit die Klick-Liste hinter den Uebersichtszahlen
    fuer genau diese Faelle lange erhalten bleibt. `langzeit_days=None` faellt auf
    *retention_days* zurueck (altes Verhalten); nie kuerzer als *retention_days*.
    Gibt die Zahl geloeschter Zeilen zurueck."""
    if not _initialised:
        return 0
    langzeit_days = retention_days if langzeit_days is None else max(langzeit_days, retention_days)
    cutoff = _cutoff_iso(retention_days)
    lang_cutoff = _cutoff_iso(langzeit_days)
    ph = ",".join("?" * len(LANGZEIT_ACTIONS))
    try:
        with _lock, _conn() as conn:
            deleted = conn.execute(
                f"DELETE FROM mail_log WHERE ts < ? "
                f"AND (action IS NULL OR action NOT IN ({ph}))",
                (cutoff, *LANGZEIT_ACTIONS),
            ).rowcount
            deleted += conn.execute(
                f"DELETE FROM mail_log WHERE ts < ? AND action IN ({ph})",
                (lang_cutoff, *LANGZEIT_ACTIONS),
            ).rowcount
        if deleted:
            log.info("mail_audit: pruned %d events (retention %d d, Langzeit %d d)",
                     deleted, retention_days, langzeit_days)
        return deleted
    except Exception as exc:
        log.warning("mail_audit: prune failed: %s", exc)
        return 0


def detail_zeitfenster() -> dict:
    """Pro action-Wert das Fenster (min_ts, max_ts) der NOCH vorhandenen
    Detailzeilen, plus "*" ueber alle. Grundlage fuer die Klickbarkeit der
    Uebersichtszahlen: die aggregierte Zahl ueberlebt das Pruning, die Zeilen
    dahinter nicht — also nur verlinken, wo noch Zeilen liegen."""
    if not _initialised:
        return {}
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT action, MIN(ts) AS mn, MAX(ts) AS mx FROM mail_log "
                "WHERE ts IS NOT NULL GROUP BY action"
            ).fetchall()
        fenster: dict = {}
        gmin = gmax = None
        for r in rows:
            if not r["mn"]:
                continue
            fenster[r["action"]] = (r["mn"], r["mx"])
            gmin = r["mn"] if gmin is None or r["mn"] < gmin else gmin
            gmax = r["mx"] if gmax is None or r["mx"] > gmax else gmax
        if gmin:
            fenster["*"] = (gmin, gmax)
        return fenster
    except Exception as exc:
        log.warning("mail_audit: detail_zeitfenster failed: %s", exc)
        return {}


def zeitraum_hat_detail(fenster: dict, action: str, date_str: str) -> bool:
    """Ueberlappt der Zeitraum (YYYY | YYYY-MM | YYYY-MM-DD) mit dem Fenster
    vorhandener Detailzeilen fuer *action* ("*" = alle)? Reine Zeichenketten-
    Vergleiche auf ISO-Zeitstempeln — lexikografisch = chronologisch."""
    if not action:
        return False
    if action == "*":
        w = fenster.get("*")
    else:
        teile = [fenster[a] for a in aktionen(action) if a in fenster]
        w = (min(t[0] for t in teile), max(t[1] for t in teile)) if teile else None
    if not w:
        return False
    mn, mx = w
    if len(date_str) == 4:          # Jahr
        start, end = f"{date_str}-01-01", f"{date_str}-12-31T23:59:59Z"
    elif len(date_str) == 7:        # Monat
        start, end = f"{date_str}-01", f"{date_str}-31T23:59:59Z"
    else:                           # Tag
        start, end = date_str, f"{date_str}T23:59:59Z"
    return mn <= end and mx >= start
