"""Kategorien-gescoptes Pruning + Klickbarkeit der Übersichtszahlen.

Hintergrund: Die aggregierte Übersichtszahl (stats_daily.json) überlebt das
Pruning, die Detailzeilen (mail_log) nicht — ein Klick auf eine alte Zahl landete
auf einer leeren Liste. Zwei Antworten:

  1. Krypto/Fehler/Warteschlange werden LÄNGER aufbewahrt als der hochvolumige
     Rest (LANGZEIT_ACTIONS, KRYPTO_LOG_RETENTION_DAYS).
  2. Eine Zahl wird nur verlinkt, wenn dahinter noch Zeilen liegen
     (detail_zeitfenster + zeitraum_hat_detail).

Diese Tests MÜSSEN fehlschlagen, wenn man das Scoping zurückbaut (dann verschwände
die Krypto-Zeile mit dem normalen Schnitt) bzw. die Überlappungslogik verdreht.
"""
import sys
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import mail_audit


def _iso(tage_alt: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=tage_alt)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert(action: str, ts: str) -> None:
    with mail_audit._conn() as conn:
        conn.execute(
            "INSERT INTO mail_log (ts, sender, recipients, subject, message_id, action) "
            "VALUES (?,?,?,?,?,?)",
            (ts, "a@x.de", "[]", "s", "m", action),
        )


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(mail_audit, "DB_PATH", tmp_path / "mail_audit.db")
    mail_audit.init_db()
    yield


def _count(action=None) -> int:
    with mail_audit._conn() as conn:
        if action:
            return conn.execute("SELECT COUNT(*) FROM mail_log WHERE action=?", (action,)).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM mail_log").fetchone()[0]


# ── Pruning ───────────────────────────────────────────────────────────────────

def test_krypto_bleibt_normal_geht(db):
    """Zeilen älter als retention, jünger als langzeit: Krypto bleibt, normal weg."""
    _insert("smime_encrypted", _iso(100))   # Krypto, 100 Tage alt
    _insert("signed", _iso(100))            # reine HTML-Signatur (Massenware)
    _insert("held", _iso(100))              # Warteschlange
    _insert("error", _iso(100))             # Fehler
    mail_audit.prune_old_events(retention_days=30, langzeit_days=730)
    assert _count("signed") == 0                     # normaler Schnitt hat gegriffen
    assert _count("smime_encrypted") == 1            # Krypto überlebt
    assert _count("held") == 1
    assert _count("error") == 1


def test_langzeit_kategorie_wird_nach_langzeit_tagen_doch_geloescht(db):
    _insert("smime_encrypted", _iso(800))   # älter als langzeit (730)
    _insert("smime_encrypted", _iso(100))   # innerhalb langzeit
    mail_audit.prune_old_events(retention_days=30, langzeit_days=730)
    assert _count("smime_encrypted") == 1   # nur die alte ist weg


def test_langzeit_nie_kuerzer_als_retention(db):
    """langzeit_days < retention_days darf die Langzeit-Zeilen NICHT früher löschen."""
    _insert("smime_encrypted", _iso(100))
    mail_audit.prune_old_events(retention_days=200, langzeit_days=30)  # 30 < 200
    # geclamped auf 200 → 100-Tage-Zeile bleibt
    assert _count("smime_encrypted") == 1


def test_none_langzeit_ist_altes_verhalten(db):
    """langzeit_days=None → alles nach retention_days, wie früher."""
    _insert("smime_encrypted", _iso(100))
    _insert("signed", _iso(100))
    mail_audit.prune_old_events(retention_days=30)   # kein langzeit → beide weg
    assert _count() == 0


# ── Zeitfenster + Klickbarkeit ──────────────────────────────────────────────

def test_detail_zeitfenster_je_action_und_stern(db):
    _insert("smime_encrypted", "2026-07-15T10:00:00Z")
    _insert("error", "2026-09-01T08:00:00Z")
    f = mail_audit.detail_zeitfenster()
    assert f["smime_encrypted"] == ("2026-07-15T10:00:00Z", "2026-07-15T10:00:00Z")
    assert f["*"][0] == "2026-07-15T10:00:00Z"       # frühester über alle
    assert f["*"][1] == "2026-09-01T08:00:00Z"       # spätester über alle


def test_zeitraum_hat_detail_ueberlappung():
    f = {"smime_encrypted": ("2026-07-15T10:00:00Z", "2026-07-15T10:00:00Z"),
         "*": ("2026-07-15T10:00:00Z", "2026-07-15T10:00:00Z")}
    # Monat mit Daten → klickbar
    assert mail_audit.zeitraum_hat_detail(f, "smime_encrypted", "2026-07") is True
    # Nachbarmonate ohne Daten → nicht klickbar
    assert mail_audit.zeitraum_hat_detail(f, "smime_encrypted", "2026-08") is False
    assert mail_audit.zeitraum_hat_detail(f, "smime_encrypted", "2026-06") is False
    # Jahr, das den Monat enthält → klickbar; anderes Jahr → nicht
    assert mail_audit.zeitraum_hat_detail(f, "smime_encrypted", "2026") is True
    assert mail_audit.zeitraum_hat_detail(f, "smime_encrypted", "2025") is False
    # Tag genau → klickbar
    assert mail_audit.zeitraum_hat_detail(f, "smime_encrypted", "2026-07-15") is True
    # "*" nutzt das Gesamtfenster
    assert mail_audit.zeitraum_hat_detail(f, "*", "2026-07") is True


def test_zeitraum_hat_detail_leere_faelle():
    f = {"smime_encrypted": ("2026-07-15T10:00:00Z", "2026-07-15T10:00:00Z")}
    assert mail_audit.zeitraum_hat_detail(f, "", "2026-07") is False        # keine Aktion
    assert mail_audit.zeitraum_hat_detail(f, "error", "2026-07") is False   # Aktion ohne Fenster
    assert mail_audit.zeitraum_hat_detail({}, "smime_encrypted", "2026-07") is False


def test_langzeit_actions_deckt_die_zugesagten_kategorien(db):
    """Die drei zugesagten Familien müssen drinstehen — sonst prunt der normale
    Schnitt sie doch weg (stille Regression)."""
    for a in ("smime_encrypted", "smime_signed", "smime_decrypted",  # Krypto
              "error", "fallback", "tenant_fremd", "relay_abgelehnt",  # Fehler/Abweisung
              "held"):                                                 # Warteschlange
        assert a in mail_audit.LANGZEIT_ACTIONS
    # reine HTML-Signatur bleibt bewusst DRAUSSEN (Massenware)
    assert "signed" not in mail_audit.LANGZEIT_ACTIONS
