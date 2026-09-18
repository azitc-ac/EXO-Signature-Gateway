"""Gateway-eigene Relay-Statistik: Volumen + Top-Absender/-Empfänger.

Bewusst GETRENNT von `relay_hosts.py`: jenes Modul ist mit dem eigenständigen
`exo-smtp-relay` byte-gleich gespiegelt (driftcheck erzwingt das). Statistik-
Erweiterungen gehören deshalb hierher — so bleibt die Spiegelung unangetastet.

Verdichtet PRO TAG (nicht pro Mail): klein und retention-freundlich, und es
entsteht kein zweiter langlebiger Speicher voller einzelner Nachrichten. Die
Pro-Gerät-Zähler je Zeitfenster führt weiterhin `relay_hosts` (`tage`); hier
kommen Volumen (Bytes) und die Top-Listen dazu.

Rechte: 600, wie jede Datei mit potenziell personenbezogenen Adressen.
"""
from __future__ import annotations

import logging
import sqlite3
import secure_io
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

log = logging.getLogger(__name__)

DB_PATH = Path(config.DATA_DIR) / "relay_stats.db"
AUFBEWAHRUNG_TAGE = 400          # wie relay_hosts, damit die 360-Tage-Sicht am Rand vollständig ist
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    secure_io.harden_file(DB_PATH)          # sonst 644 bis zum nächsten harden_tree
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS tage (
        ip     TEXT NOT NULL, tag TEXT NOT NULL,
        anzahl INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (ip, tag)) WITHOUT ROWID""")
    c.execute("""CREATE TABLE IF NOT EXISTS absender (
        absender TEXT NOT NULL, tag TEXT NOT NULL,
        anzahl INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (absender, tag)) WITHOUT ROWID""")
    c.execute("""CREATE TABLE IF NOT EXISTS empfaenger (
        empfaenger TEXT NOT NULL, tag TEXT NOT NULL,
        anzahl INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (empfaenger, tag)) WITHOUT ROWID""")
    # Volumen je Sende-Identität (Login), sofern die Einlieferung über Port 587
    # authentifiziert war. Getrennt von `ip`, weil sich ein Login mehrere Geräte
    # (IPs) teilen darf und die aussagekräftige Grösse dann die Identität ist.
    c.execute("""CREATE TABLE IF NOT EXISTS identitaet (
        identitaet TEXT NOT NULL, tag TEXT NOT NULL,
        anzahl INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (identitaet, tag)) WITHOUT ROWID""")
    # Domänen-Routing / Hybrid-Koexistenz: an ein Next-Hop-Ziel (≠ EXO) geroutete
    # Post, je Ziel und Tag. `fehler` zählt die Transaktionen, die das Ziel nicht
    # annahm; `zuletzt` merkt den letzten Zeitpunkt (ISO) für „zuletzt aktiv".
    # Das Gateway reicht diese Post byte-genau durch — Betreff/Inhalt werden nicht
    # gelesen, deshalb hier nur Anzahl/Volumen/Ziel/Zeit, keine Absender/Empfänger.
    c.execute("""CREATE TABLE IF NOT EXISTS routing (
        ziel TEXT NOT NULL, tag TEXT NOT NULL,
        anzahl INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
        fehler INTEGER NOT NULL DEFAULT 0, zuletzt TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (ziel, tag)) WITHOUT ROWID""")
    return c


def _jetzt() -> datetime:
    return datetime.now(timezone.utc)


def _heute() -> str:
    return _jetzt().strftime("%Y-%m-%d")


def merke(ip: str, absender: str = "", empfaenger: list | None = None,
          bytes_: int = 0, identitaet: str = "") -> None:
    """Eine angenommene Relay-Mail verbuchen (Tag/Volumen + Absender-/Empfänger-
    Aggregat, bei Login-Einlieferung zusätzlich je Identität). Best-effort:
    Zählen darf den Mailfluss nie aufhalten."""
    ip = (ip or "").strip()
    tag = _heute()
    groesse = max(0, int(bytes_ or 0))
    absender = (absender or "").strip().lower()[:200]
    identitaet = (identitaet or "").strip().lower()[:200]
    ziele = [(e or "").strip().lower()[:200] for e in (empfaenger or []) if (e or "").strip()]
    try:
        with _lock, _conn() as c:
            if ip:
                c.execute("INSERT INTO tage (ip, tag, anzahl, bytes) VALUES (?,?,1,?) "
                          "ON CONFLICT(ip, tag) DO UPDATE SET anzahl = anzahl + 1, "
                          "bytes = bytes + excluded.bytes", (ip, tag, groesse))
            if absender:
                c.execute("INSERT INTO absender (absender, tag, anzahl, bytes) VALUES (?,?,1,?) "
                          "ON CONFLICT(absender, tag) DO UPDATE SET anzahl = anzahl + 1, "
                          "bytes = bytes + excluded.bytes", (absender, tag, groesse))
            if identitaet:
                c.execute("INSERT INTO identitaet (identitaet, tag, anzahl, bytes) VALUES (?,?,1,?) "
                          "ON CONFLICT(identitaet, tag) DO UPDATE SET anzahl = anzahl + 1, "
                          "bytes = bytes + excluded.bytes", (identitaet, tag, groesse))
            for ziel in ziele:
                c.execute("INSERT INTO empfaenger (empfaenger, tag, anzahl, bytes) VALUES (?,?,1,?) "
                          "ON CONFLICT(empfaenger, tag) DO UPDATE SET anzahl = anzahl + 1, "
                          "bytes = bytes + excluded.bytes", (ziel, tag, groesse))
    except Exception as exc:                          # pragma: no cover
        log.warning("relay_stats.merke fehlgeschlagen: %s", exc)


def merke_routing(ziel: str, bytes_: int = 0, ok: bool = True) -> None:
    """Eine an ein Next-Hop-Ziel (Hybrid) geroutete Transaktion verbuchen.
    Best-effort: Zählen darf den Mailfluss nie aufhalten."""
    ziel = (ziel or "").strip().lower()[:100]
    if not ziel:
        return
    tag = _heute()
    groesse = max(0, int(bytes_ or 0))
    fehler = 0 if ok else 1
    jetzt = _jetzt().strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with _lock, _conn() as c:
            c.execute(
                "INSERT INTO routing (ziel, tag, anzahl, bytes, fehler, zuletzt) "
                "VALUES (?,?,1,?,?,?) "
                "ON CONFLICT(ziel, tag) DO UPDATE SET anzahl = anzahl + 1, "
                "bytes = bytes + excluded.bytes, fehler = fehler + excluded.fehler, "
                "zuletzt = excluded.zuletzt",
                (ziel, tag, groesse, fehler, jetzt))
    except Exception as exc:                          # pragma: no cover
        log.warning("relay_stats.merke_routing fehlgeschlagen: %s", exc)


def routing_statistik(tage_zurueck: int = 30) -> dict:
    """Gesamt-Anzahl/-Volumen/-Fehler + je Ziel für die an on-prem-Ziele geroutete
    (Hybrid-)Post über die letzten *tage_zurueck* Tage. `zuletzt` = letzter
    Zeitpunkt je Ziel (für „zuletzt aktiv")."""
    ab = (_jetzt() - timedelta(days=max(1, tage_zurueck))).strftime("%Y-%m-%d")
    leer = {"tage": tage_zurueck, "gesamt_anzahl": 0, "gesamt_bytes": 0,
            "gesamt_fehler": 0, "je_ziel": []}
    try:
        with _conn() as c:
            g = c.execute(
                "SELECT COALESCE(SUM(anzahl),0) a, COALESCE(SUM(bytes),0) b, "
                "COALESCE(SUM(fehler),0) f FROM routing WHERE tag >= ?", (ab,)).fetchone()
            je_ziel = [
                {"ziel": z["ziel"], "anzahl": z["a"], "bytes": z["b"],
                 "fehler": z["f"], "zuletzt": z["z"]}
                for z in c.execute(
                    "SELECT ziel, SUM(anzahl) a, SUM(bytes) b, SUM(fehler) f, "
                    "MAX(zuletzt) z FROM routing WHERE tag >= ? "
                    "GROUP BY ziel ORDER BY a DESC", (ab,))]
            return {"tage": tage_zurueck, "gesamt_anzahl": g["a"],
                    "gesamt_bytes": g["b"], "gesamt_fehler": g["f"],
                    "je_ziel": je_ziel}
    except Exception as exc:                          # pragma: no cover
        log.warning("relay_stats.routing_statistik fehlgeschlagen: %s", exc)
        return leer


def statistik(tage_zurueck: int = 30, grenze_top: int = 100) -> dict:
    """Gesamt-Anzahl/-Volumen + Top-Geräte/-Absender/-Empfänger/-Identitäten (je
    Anzahl + Bytes) über die letzten *tage_zurueck* Tage.

    `grenze_top` großzügig (100), damit die Oberfläche „Top 10 + Rest ausklappen"
    anbieten kann, ohne nachzuladen."""
    ab = (_jetzt() - timedelta(days=max(1, tage_zurueck))).strftime("%Y-%m-%d")
    leer = {"tage": tage_zurueck, "gesamt_anzahl": 0, "gesamt_bytes": 0,
            "geraete": 0, "top_geraete": [], "top_absender": [], "top_empfaenger": [],
            "top_identitaeten": []}
    try:
        with _conn() as c:
            g = c.execute("SELECT COALESCE(SUM(anzahl),0) a, COALESCE(SUM(bytes),0) b, "
                          "COUNT(DISTINCT ip) g FROM tage WHERE tag >= ?", (ab,)).fetchone()

            def _top(tabelle, spalte):
                return [{"name": z[spalte], "anzahl": z["a"], "bytes": z["b"]}
                        for z in c.execute(
                            f"SELECT {spalte}, SUM(anzahl) a, SUM(bytes) b FROM {tabelle} "
                            f"WHERE tag >= ? GROUP BY {spalte} ORDER BY a DESC, b DESC LIMIT ?",
                            (ab, grenze_top))]
            return {
                "tage": tage_zurueck,
                "gesamt_anzahl": g["a"], "gesamt_bytes": g["b"], "geraete": g["g"],
                "top_geraete": _top("tage", "ip"),
                "top_absender": _top("absender", "absender"),
                "top_empfaenger": _top("empfaenger", "empfaenger"),
                "top_identitaeten": _top("identitaet", "identitaet"),
            }
    except Exception as exc:                          # pragma: no cover
        log.warning("relay_stats.statistik fehlgeschlagen: %s", exc)
        return leer


def letzte_aktivitaet(tage_zurueck: int = AUFBEWAHRUNG_TAGE) -> dict:
    """{identitaet(login): letzter Tag mit Einlieferung} über den Zeitraum.

    Aus den Tagesaggregaten der Identitäts-Tabelle — kein zusätzlicher Schreibweg
    je Mail (das „zuletzt aktiv" der Oberfläche fällt so nebenbei ab)."""
    ab = (_jetzt() - timedelta(days=max(1, tage_zurueck))).strftime("%Y-%m-%d")
    try:
        with _conn() as c:
            return {z["identitaet"]: z["m"] for z in c.execute(
                "SELECT identitaet, MAX(tag) m FROM identitaet "
                "WHERE tag >= ? GROUP BY identitaet", (ab,))}
    except Exception as exc:                          # pragma: no cover
        log.warning("relay_stats.letzte_aktivitaet fehlgeschlagen: %s", exc)
        return {}


def identitaet_zaehler(tage_zurueck: int = 30) -> dict:
    """Sendezähler je Sende-Identität (Login): `heute` und `zeitraum` (Summe über
    die letzten *tage_zurueck* Tage). Für die Kontingent-/Transparenzanzeige.

    Der Tag ist wie überall in diesem Modul der UTC-Tag — das „heute" folgt also
    UTC-Mitternacht, konsistent mit den gespeicherten Aggregaten.
    """
    heute = _heute()
    ab = (_jetzt() - timedelta(days=max(1, tage_zurueck))).strftime("%Y-%m-%d")
    ergebnis: dict = {}
    try:
        with _conn() as c:
            for z in c.execute(
                    "SELECT identitaet, "
                    "COALESCE(SUM(CASE WHEN tag = ? THEN anzahl ELSE 0 END),0) heute, "
                    "COALESCE(SUM(anzahl),0) zeitraum "
                    "FROM identitaet WHERE tag >= ? GROUP BY identitaet",
                    (heute, ab)):
                ergebnis[z["identitaet"]] = {"heute": z["heute"], "zeitraum": z["zeitraum"]}
    except Exception as exc:                          # pragma: no cover
        log.warning("relay_stats.identitaet_zaehler fehlgeschlagen: %s", exc)
    return ergebnis


def heute_zaehler(login: str) -> int:
    """Heutige Sendeanzahl einer Identität (UTC-Tag); 0 bei Fehler/unbekannt.

    Schlanker Einzelabruf für die weiche Kontingent-Warnung im Einlieferungspfad.
    """
    login = (login or "").strip().lower()
    if not login:
        return 0
    try:
        with _conn() as c:
            r = c.execute("SELECT COALESCE(SUM(anzahl),0) a FROM identitaet "
                          "WHERE identitaet = ? AND tag = ?", (login, _heute())).fetchone()
            return int(r["a"]) if r else 0
    except Exception:                                 # pragma: no cover
        return 0


def aufraeumen(tage: int = AUFBEWAHRUNG_TAGE) -> int:
    """Aggregate jenseits der Aufbewahrung löschen."""
    grenze = (_jetzt() - timedelta(days=tage)).strftime("%Y-%m-%d")
    try:
        with _lock, _conn() as c:
            weg = c.execute("DELETE FROM tage WHERE tag < ?", (grenze,)).rowcount
            weg += c.execute("DELETE FROM absender WHERE tag < ?", (grenze,)).rowcount
            weg += c.execute("DELETE FROM empfaenger WHERE tag < ?", (grenze,)).rowcount
            weg += c.execute("DELETE FROM identitaet WHERE tag < ?", (grenze,)).rowcount
            weg += c.execute("DELETE FROM routing WHERE tag < ?", (grenze,)).rowcount
            return weg
    except Exception as exc:                          # pragma: no cover
        log.warning("relay_stats.aufraeumen fehlgeschlagen: %s", exc)
        return 0
