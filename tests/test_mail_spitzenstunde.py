"""Spitzenstunde und Stundenverlauf in Ortszeit (LOG_TIMEZONE), nicht in UTC.

mail_log.ts ist UTC. Bis v1.12.99 gruppierten peak_hour() und get_mail_hourly()
nach substr(ts,12,2) — der UTC-Stunde — und nach dem UTC-Kalendertag. Die
Übersicht zeigte „9–10 Uhr“ für Post, die in Berlin um 10 Uhr lief.

Diese Tests MÜSSEN fehlschlagen, wenn:
  - wieder nach UTC-Stunde gruppiert wird,
  - der Tag wieder als UTC-Tag abgegrenzt wird (Post kurz vor Mitternacht Ortszeit),
  - die Graph-Aufrufzählung wieder datetime.now() ohne Zone nimmt.
"""
import ast
import sqlite3
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import mail_audit

BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(mail_audit, "DB_PATH", tmp_path / "mail_audit.db")
    mail_audit.init_db()
    yield tmp_path / "mail_audit.db"


def _zeile(pfad, ts, ms=100):
    with sqlite3.connect(pfad) as c:
        c.execute("INSERT INTO mail_log (ts, action, processing_ms) VALUES (?, 'signed', ?)", (ts, ms))


def test_spitzenstunde_in_ortszeit(db):
    # 19.11. (Winterzeit, UTC+1): 3 Mails um 09:xx UTC = 10 Uhr Berlin, 1 um 07 UTC
    for m in ("09:05", "09:20", "09:59"):
        _zeile(db, f"2026-11-19T{m}:00Z")
    _zeile(db, "2026-11-19T07:30:00Z")
    assert mail_audit.peak_hour("2026-11-19", BERLIN) == ("10", 3)


def test_tagesgrenze_in_ortszeit(db):
    # 23:30 UTC am 19.11. = 00:30 Berlin am 20.11. → gehört zum 20.
    _zeile(db, "2026-11-19T23:30:00Z")
    _zeile(db, "2026-11-19T22:59:00Z")   # 23:59 Berlin am 19.
    stunden_19 = {r["hour"]: r["count"] for r in mail_audit.get_mail_hourly("2026-11-19", BERLIN)}
    stunden_20 = {r["hour"]: r["count"] for r in mail_audit.get_mail_hourly("2026-11-20", BERLIN)}
    assert stunden_19 == {"23": 1}
    assert stunden_20 == {"00": 1}


def test_stundenverlauf_werte(db):
    _zeile(db, "2026-07-01T08:10:00Z", 100)   # Sommerzeit UTC+2 → 10 Uhr
    _zeile(db, "2026-07-01T08:40:00Z", 300)
    _zeile(db, "2026-07-01T08:50:00Z", 0)     # 0 ms zählt mit, aber nicht im Mittel
    r = mail_audit.get_mail_hourly("2026-07-01", BERLIN)
    assert r == [{"hour": "10", "count": 3, "avg_ms": 200, "peak_ms": 300}]


def test_utc_zone_bleibt_utc(db):
    _zeile(db, "2026-11-19T09:05:00Z")
    assert mail_audit.peak_hour("2026-11-19", ZoneInfo("UTC")) == ("09", 1)


def test_graph_zaehler_nutzen_ortszeit():
    """Keine zonenlose Uhr in graph_client: datetime.now() ohne Argument und
    date.today() liefern im Container UTC."""
    quelle = (Path(__file__).resolve().parent.parent / "app" / "graph_client.py").read_text()
    treffer = []
    for k in ast.walk(ast.parse(quelle)):
        if isinstance(k, ast.Call) and isinstance(k.func, ast.Attribute):
            if k.func.attr == "now" and not k.args and not k.keywords:
                treffer.append(k.lineno)
            if k.func.attr == "today":
                treffer.append(k.lineno)
    assert not treffer, f"zonenlose Uhr in graph_client.py, Zeilen {treffer}"


def test_keine_angehaengte_pluralendung_fuer_postfach():
    """„Postfach“ + 'fächer' ergab „Postfachfächer“ (6 Stellen bis v1.12.99).
    Der Plural ist ein eigenes Wort, keine Endung."""
    import re
    wurzel = Path(__file__).resolve().parent.parent / "app"
    treffer = []
    for p in list(wurzel.rglob("*.html")) + list(wurzel.rglob("*.js")) + list(wurzel.rglob("*.py")):
        for nr, zeile in enumerate(p.read_text(errors="ignore").splitlines(), 1):
            if re.search(r"""['"]fächer['"]""", zeile):
                treffer.append(f"{p.relative_to(wurzel)}:{nr}")
    assert not treffer, treffer
