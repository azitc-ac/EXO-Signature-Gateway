"""Zahlen der Übersicht: was sie messen, und dass die Klickliste dieselben
Mails zeigt (Entscheidung 21.11.2026, v1.12.149).

* „Heute“ ist der Tag in der Anzeige-Zeitzone (LOG_TIMEZONE) — Tageszahlen,
  Klicklisten und Peak rechnen gleich. Bis v1.12.148: Zahlen UTC, Peak lokal.
* Jede Zählstelle von „Verarbeitet“ protokolliert eine Aktion aus der Gruppe
  @verarbeitet; „S/MIME signiert“ findet auch signiert+verschlüsselte Mails.
* Der Peak-Klick filtert nach der Stunde.
* Der Tagesbericht-Schnappschuss verliert keine Zählungen (Wettlauf).
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

WURZEL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WURZEL / "app"))
import mail_audit  # noqa: E402
import settings_store  # noqa: E402
import stats  # noqa: E402

BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(mail_audit, "DB_PATH", tmp_path / "mail_audit.db")
    monkeypatch.setattr(settings_store, "anzeige_zone", lambda: BERLIN)
    mail_audit.init_db()
    yield


def _zeile(ts: str, action: str):
    with mail_audit._conn() as conn:
        conn.execute("INSERT INTO mail_log (ts, sender, recipients, subject, action) "
                     "VALUES (?, 'max@firma.at', '[]', 'x', ?)", (ts, action))


def test_tagesgrenzen_in_ortszeit():
    assert mail_audit.zeitraum_utc("2026-11-21", BERLIN) == ("2026-11-20T23:00:00Z", "2026-11-21T23:00:00Z")
    assert mail_audit.zeitraum_utc("2026-07-01", BERLIN) == ("2026-06-30T22:00:00Z", "2026-07-01T22:00:00Z")
    assert mail_audit.zeitraum_utc("2026-11-21", BERLIN, stunde=10) == ("2026-11-21T09:00:00Z", "2026-11-21T10:00:00Z")
    assert mail_audit.zeitraum_utc("2026-11", BERLIN) == ("2026-10-31T23:00:00Z", "2026-11-30T23:00:00Z")


def test_klickliste_kurz_nach_mitternacht_gehoert_zum_ortstag(db):
    _zeile("2026-11-20T23:30:00Z", "signed")      # 00:30 Uhr in Berlin, 21.11.
    assert mail_audit.count_events(date="2026-11-21") == 1
    assert mail_audit.count_events(date="2026-11-20") == 0


def test_peak_klick_filtert_nach_der_stunde(db):
    _zeile("2026-11-21T09:15:00Z", "signed")      # 10:15 Berlin
    _zeile("2026-11-21T11:15:00Z", "signed")      # 12:15 Berlin
    assert mail_audit.count_events(date="2026-11-21", stunde=10) == 1
    assert mail_audit.count_events(date="2026-11-21") == 2


def test_gruppen_klicklisten(db):
    for a in ("signed", "smime_signed", "smime_encrypted", "smime_signiert_verschluesselt",
              "durchgereicht", "error", "ohne_zertifikat", "zustellfehler"):
        _zeile("2026-11-21T09:00:00Z", a)
    z = lambda g: mail_audit.count_events(date="2026-11-21", action=g)  # noqa: E731
    assert z("@verarbeitet") == 5                 # nicht: durchgereicht, error, ohne_zertifikat
    assert z("@smime_signiert") == 2              # signiert + signiert/verschlüsselt
    assert z("@smime_verschluesselt") == 2
    assert z("@fehler") == 2                       # error + zustellfehler, NICHT ohne_zertifikat
    assert z("smime_signed,signed") == 2


def test_heute_in_anzeige_zeitzone(monkeypatch):
    weit = ZoneInfo("Pacific/Kiritimati")         # UTC+14: fast immer ein anderer Tag als UTC
    monkeypatch.setattr(settings_store, "anzeige_zone", lambda: weit)
    assert stats.heute() == datetime.now(weit).date()


def test_schnappschuss_verliert_keine_zaehlung(tmp_path, monkeypatch):
    """_save_snapshot schrieb total = Schnappschuss; lief dazwischen increment(),
    fehlte die Zählung in stats.json."""
    f = tmp_path / "stats.json"
    monkeypatch.setattr(stats, "_STATS_FILE", f)
    monkeypatch.setattr(stats, "_stats", {k: 0 for k in stats.KEYS})
    monkeypatch.setattr(stats, "_snapshot", {k: 0 for k in stats.KEYS})
    stats._stats["processed"] = 7
    alt = dict(stats._stats)
    stats._stats["processed"] = 8                 # Zählung NACH dem Schnappschuss
    with stats._lock:
        stats._save_snapshot(alt)
    d = json.loads(f.read_text())
    assert d["snapshot"]["processed"] == 7 and d["total"]["processed"] == 8


def test_jede_verarbeitet_zaehlung_protokolliert_eine_gruppenaktion():
    """Zahl und Klickliste decken sich nur, wenn jede Stelle, die „processed“
    zählt, auch eine Aktion aus @verarbeitet schreibt."""
    src = (WURZEL / "app/handler.py").read_text(encoding="utf-8").splitlines()
    gruppe = set(mail_audit.AKTIONSGRUPPEN["@verarbeitet"])
    stellen = [i for i, z in enumerate(src) if 'stats.increment("processed")' in z]
    assert len(stellen) >= 5, stellen
    for i in stellen:
        fenster = "\n".join(src[max(0, i - 25):i + 25])
        aktionen = set(re.findall(r'_audit\("(\w+)"', fenster))
        if "_audit(_action)" in fenster:
            aktionen |= set(re.findall(r'"(\w+)"', "\n".join(
                z for z in src[i:i + 15] if "_action = " in z)))
        assert aktionen & gruppe, f"handler.py:{i + 1}: zählt processed ohne Aktion aus @verarbeitet"


def test_tagesbericht_beschriftet_seit_dem_letzten_bericht():
    src = (WURZEL / "app/notification.py").read_text(encoding="utf-8")
    assert "Seit dem letzten Bericht" in src and "Statistiken heute" not in src
