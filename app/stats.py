"""In-memory stats with full persistence across container restarts.

stats.json layout:
  snapshot        — totals at last daily report (used to compute daily delta)
  total           — running totals at last save (restored into _stats on startup)
  snapshot_at / total_saved_at — ISO timestamps for debugging

stats_daily.json layout:
  {"YYYY-MM-DD": {"processed": N, "smime_signed": N, ...}, ...}
  Accumulated by increment_daily(); used for monthly/yearly aggregation.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
import config

log = logging.getLogger(__name__)
_STATS_FILE = Path(config.DATA_DIR) / "stats.json"
_DAILY_FILE = Path(config.DATA_DIR) / "stats_daily.json"
_lock = RLock()
_daily_lock = RLock()

KEYS = (
    "processed", "fallback", "errors", "held",
    "smime_signed", "smime_encrypted", "smime_decrypted", "certs_harvested",
    "graph_api_calls", "kv_sign_calls",
    # Belege eines Fremdsystems, deren Aufbau die Korrektur nicht mehr trifft.
    # Zaehlt, damit es im Tagesbericht auffaellt statt nur im Protokoll zu
    # stehen: Der Lexware-Fix lief von Juli bis August 2026 wirkungslos mit,
    # weil ein Vorlagenwechsel unbemerkt blieb.
    "lexware_unbekannt",
    # "antworten"         — ausgehende Mails, die in einer Kette stehen
    #                       (References/In-Reply-To vorhanden)
    # "stapel_verhindert" — davon jene, bei denen der volle Signaturblock ein
    #                       zweites Mal AUSGEBLIEBEN ist, weil in dieser Kette
    #                       schon signiert war. Beide Ausgaenge zaehlen:
    #                       Minimalsignatur oder gar keine.
    #
    # ⚠️ Gezaehlt wird die WIRKUNG, nicht der Befund — und zwar an der Stelle,
    # an der gehandelt wird. Stuende der Zaehler bei der Erkennung, wuerde er
    # auch Faelle mitnehmen, in denen vorher schon ein anderer Grund griff
    # (#nosig, Add-in-Signatur im Verfassenbereich); den verhinderten Stapel
    # duerfte er sich dann nicht zuschreiben.
    #
    # ⚠️ ZWEI Zaehler, nicht einer. Hier ist die NULL die Meldung — und eine
    # einzelne Null waere nicht zu deuten: Sie kann "hat nicht gegriffen"
    # heissen (Fehler) oder "es gab nichts zu greifen" (normal). Erst das
    # Verhaeltnis trennt beides.
    #
    # Anlass: Genau diese Erkennung lief vom 29.07. bis 10.08.2026 wirkungslos
    # mit — zwoelf Tage, in denen Empfaenger die Signatur doppelt bekamen.
    # Keiner der damals 500 Tests schlug an, weil alle gegen selbstgebautes
    # MIME liefen; im Protokoll stand es, aber dorthin sieht im Normalbetrieb
    # niemand.
    "antworten",
    "stapel_verhindert",
    # Empfaengerzertifikate, die beim Verschluesseln abgelehnt wurden (abgelaufen,
    # noch nicht gueltig oder unlesbar). Die Nachricht geht dann ueber das Portal
    # bzw. per Unzustellbarkeitsmeldung hinaus — sie faellt also nicht aus, aber
    # der Betreiber muss es merken: Ein abgelaufenes Partnerzertifikat gehoert
    # erneuert, sonst laeuft dauerhaft der Ersatzweg.
    "cert_ungueltig",
    # Stufe 2 (Widerruf). Getrennt gezaehlt, weil die drei Faelle verschiedene
    # Handlungen verlangen: ein widerrufenes Zertifikat gehoert aus dem Bestand,
    # eine unerreichbare Sperrliste ist eine Stoerung (moeglicherweise die eigene
    # Firewall), und ein Zertifikat ohne Sperrlisten-Adresse zeigt, fuer welchen
    # Teil des Bestands die Pruefung gar nicht greift.
    "cert_widerrufen",
    "cert_crl_unerreichbar",
    "cert_ohne_crl",
)
_stats: dict = {k: 0 for k in KEYS}
_snapshot: dict = {k: 0 for k in KEYS}
_session_start: str = datetime.now(timezone.utc).isoformat()


def _load() -> None:
    global _snapshot, _stats
    try:
        if _STATS_FILE.exists():
            data = json.loads(_STATS_FILE.read_text())
            _snapshot = {k: int(data.get("snapshot", {}).get(k, 0)) for k in KEYS}
            raw_total = data.get("total") or data.get("snapshot") or {}
            _stats = {k: int(raw_total.get(k, 0)) for k in KEYS}
    except Exception as exc:
        log.warning("stats: could not load: %s", exc)


def _persist_total() -> None:
    """Write current running total to disk (called after every increment)."""
    try:
        _STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if _STATS_FILE.exists():
            try:
                data = json.loads(_STATS_FILE.read_text())
            except Exception:
                pass
        data["total"] = dict(_stats)
        data["total_saved_at"] = datetime.now(timezone.utc).isoformat()
        _STATS_FILE.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        log.warning("stats: could not persist total: %s", exc)


def _increment_daily(key: str) -> None:
    """Add 1 to today's entry in stats_daily.json."""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        with _daily_lock:
            _DAILY_FILE.parent.mkdir(parents=True, exist_ok=True)
            data: dict = {}
            if _DAILY_FILE.exists():
                try:
                    data = json.loads(_DAILY_FILE.read_text())
                except Exception:
                    pass
            day = data.setdefault(today, {k: 0 for k in KEYS})
            day[key] = day.get(key, 0) + 1
            _DAILY_FILE.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        log.warning("stats: could not update daily file: %s", exc)


def get() -> dict:
    with _lock:
        d = dict(_stats)
    d["session_start"] = _session_start
    return d


def get_today() -> dict:
    """Die Zahlen des heutigen Kalendertages.

    ⚠️ HIESS BIS v1.7.185 `get_daily()` UND RECHNETE `_stats - _snapshot`.
    Das war etwas anderes, als der Name auf der Übersicht verspricht: Der
    Schnappschuss wird ausschliesslich von `take_daily_snapshot()` gesetzt, und
    das ruft nur der Tagesbericht — `scheduler.py` fuehrt ihn hinter
    `DAILY_REPORT_ENABLED and NOTIFICATION_MAILBOX`.

    Wer den Tagesbericht also nicht eingeschaltet oder keine Empfaengeradresse
    hinterlegt hat, bekam unter „Heute" ALLES SEIT DER INSTALLATION. Auf dem
    Entwicklungsgeraet standen am 13.08.2026 unter „Heute" 369 Fehler, die
    saemtlich aus dem Juli stammten — waehrend „3 Tage" und der laufende Monat
    korrekt 0 zeigten. Aufgefallen ist es erst, als jemand die Seite ansah.

    Die Tagesdatei ist die verlaessliche Quelle: `increment()` schreibt sie bei
    JEDEM Ereignis mit, unabhaengig von Berichten und Einstellungen.
    """
    return get_last_n_days(1)


def get_last_n_days(n: int) -> dict:
    """Sum stats for the last n calendar days including today."""
    from datetime import date, timedelta
    result = {k: 0 for k in KEYS}
    try:
        if not _DAILY_FILE.exists():
            return result
        data = json.loads(_DAILY_FILE.read_text())
        today = date.today()
        for i in range(n):
            counts = data.get((today - timedelta(days=i)).strftime("%Y-%m-%d"), {})
            for k in KEYS:
                result[k] += int(counts.get(k, 0))
    except Exception as exc:
        log.warning("stats: get_last_n_days error: %s", exc)
    return result


def get_period(year: int, month: int | None = None) -> dict:
    """Aggregate stats_daily.json for a given year (and optionally month).

    Returns a dict with the same KEYS, summed over all matching days.
    """
    result = {k: 0 for k in KEYS}
    try:
        if not _DAILY_FILE.exists():
            return result
        data = json.loads(_DAILY_FILE.read_text())
        prefix = f"{year:04d}-{month:02d}" if month else f"{year:04d}"
        for day_str, counts in data.items():
            if day_str.startswith(prefix):
                for k in KEYS:
                    result[k] += int(counts.get(k, 0))
    except Exception as exc:
        log.warning("stats: get_period error: %s", exc)
    return result


def increment(key: str) -> None:
    with _lock:
        _stats[key] = _stats.get(key, 0) + 1
        _persist_total()
    _increment_daily(key)


def take_daily_snapshot() -> dict:
    """Snapshot current totals (for daily report). Returns daily delta."""
    with _lock:
        daily = {k: max(0, _stats[k] - _snapshot.get(k, 0)) for k in KEYS}
        new_snap = dict(_stats)
    _save_snapshot(new_snap)
    return daily


def _save_snapshot(snapshot: dict) -> None:
    global _snapshot
    _snapshot = snapshot
    try:
        _STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if _STATS_FILE.exists():
            try:
                data = json.loads(_STATS_FILE.read_text())
            except Exception:
                pass
        data["snapshot"] = snapshot
        data["snapshot_at"] = datetime.now(timezone.utc).isoformat()
        data["total"] = snapshot
        data["total_saved_at"] = datetime.now(timezone.utc).isoformat()
        _STATS_FILE.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        log.warning("stats: could not persist snapshot: %s", exc)



def get_session_start() -> str:
    """ISO timestamp of when this container process started."""
    return _session_start


_load()
