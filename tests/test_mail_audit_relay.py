"""Relay-Markierung im Mail-Protokoll + der relay-fokussierte Filter.

Hintergrund: Das Relay-Protokoll (relay.html) zeigt NUR die über das SMTP-Relay
eingelieferte Post. Damit das ohne einen zweiten Speicher geht, trägt jede
Audit-Zeile die Quelle (`relay_ip`) — leer bei Post auf dem regulären
Exchange-Weg, gesetzt bei Relay-Einlieferung. `query_events(nur_relay=True)`
filtert genau darauf.

Diese Tests MÜSSEN fehlschlagen, wenn:
  - die Spalte `relay_ip` nicht mehr gespeichert wird (Filter fände nichts),
  - der `nur_relay`-Filter auch Nicht-Relay-Zeilen durchließe,
  - die Wanderung für Bestands-DBs fehlt (ALTER TABLE ADD COLUMN).
"""
import sys
import sqlite3
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import mail_audit


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(mail_audit, "DB_PATH", tmp_path / "mail_audit.db")
    mail_audit.init_db()
    yield


def _log(action, relay_ip=""):
    mail_audit.log_event(
        sender="drucker@firma.de", recipients=["kunde@extern.de"],
        subject="Scan", message_id="m", action=action, relay_ip=relay_ip)


def test_relay_ip_wird_gespeichert_und_zurueckgegeben(db):
    _log("signed", relay_ip="10.1.5.7")
    zeilen = mail_audit.query_events(nur_relay=True)
    assert len(zeilen) == 1
    assert zeilen[0]["relay_ip"] == "10.1.5.7"


def test_nur_relay_schliesst_exchange_post_aus(db):
    _log("signed", relay_ip="10.1.5.7")     # Relay
    _log("signed")                          # regulärer Exchange-Weg (leer → NULL)
    assert mail_audit.count_events() == 2
    assert mail_audit.count_events(nur_relay=True) == 1
    assert len(mail_audit.query_events(nur_relay=True)) == 1
    # Leerer relay_ip landet als NULL, nicht als "" — sonst zöge der Filter ihn mit.
    with mail_audit._conn() as conn:
        nullen = conn.execute(
            "SELECT COUNT(*) FROM mail_log WHERE relay_ip IS NULL").fetchone()[0]
    assert nullen == 1


def test_relay_ip_filter_einzelnes_geraet(db):
    _log("signed", relay_ip="10.1.5.7")
    _log("relay_abgelehnt", relay_ip="10.1.5.9")
    assert mail_audit.count_events(relay_ip="10.1.5.7") == 1
    assert mail_audit.count_events(relay_ip="10.1.5.9") == 1
    z = mail_audit.query_events(relay_ip="10.1.5.9")
    assert z[0]["action"] == "relay_abgelehnt"


def test_wanderung_ergaenzt_spalte_in_bestands_db(tmp_path, monkeypatch):
    """Eine DB im alten Schema (ohne relay_ip) muss die Spalte beim init_db
    nachbekommen — sonst schlüge jeder relay-markierte INSERT fehl."""
    pfad = tmp_path / "mail_audit.db"
    with sqlite3.connect(str(pfad)) as conn:
        conn.execute("""
            CREATE TABLE mail_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
                sender TEXT, recipients TEXT, subject TEXT, message_id TEXT,
                action TEXT, size_bytes INTEGER, processing_ms INTEGER, error TEXT
            )""")
        conn.execute("INSERT INTO mail_log (ts, action) VALUES ('2026-01-01T00:00:00Z','signed')")
    monkeypatch.setattr(mail_audit, "DB_PATH", pfad)
    mail_audit.init_db()
    spalten = set()
    with mail_audit._conn() as conn:
        spalten = {r["name"] for r in conn.execute("PRAGMA table_info(mail_log)")}
    assert "relay_ip" in spalten
    # Und ein relay-markierter Schreibvorgang geht jetzt durch.
    _log("signed", relay_ip="10.2.2.2")
    assert mail_audit.count_events(nur_relay=True) == 1
