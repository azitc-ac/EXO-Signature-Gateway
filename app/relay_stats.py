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


def statistik(tage_zurueck: int = 30, grenze_top: int = 20) -> dict:
    """Gesamt-Anzahl/-Volumen + Top-Absender/-Empfänger (je Anzahl + Bytes) über
    die letzten *tage_zurueck* Tage."""
    ab = (_jetzt() - timedelta(days=max(1, tage_zurueck))).strftime("%Y-%m-%d")
    leer = {"tage": tage_zurueck, "gesamt_anzahl": 0, "gesamt_bytes": 0,
            "geraete": 0, "top_absender": [], "top_empfaenger": [],
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
                "top_absender": _top("absender", "absender"),
                "top_empfaenger": _top("empfaenger", "empfaenger"),
                "top_identitaeten": _top("identitaet", "identitaet"),
            }
    except Exception as exc:                          # pragma: no cover
        log.warning("relay_stats.statistik fehlgeschlagen: %s", exc)
        return leer


def aufraeumen(tage: int = AUFBEWAHRUNG_TAGE) -> int:
    """Aggregate jenseits der Aufbewahrung löschen."""
    grenze = (_jetzt() - timedelta(days=tage)).strftime("%Y-%m-%d")
    try:
        with _lock, _conn() as c:
            weg = c.execute("DELETE FROM tage WHERE tag < ?", (grenze,)).rowcount
            weg += c.execute("DELETE FROM absender WHERE tag < ?", (grenze,)).rowcount
            weg += c.execute("DELETE FROM empfaenger WHERE tag < ?", (grenze,)).rowcount
            weg += c.execute("DELETE FROM identitaet WHERE tag < ?", (grenze,)).rowcount
            return weg
    except Exception as exc:                          # pragma: no cover
        log.warning("relay_stats.aufraeumen fehlgeschlagen: %s", exc)
        return 0
