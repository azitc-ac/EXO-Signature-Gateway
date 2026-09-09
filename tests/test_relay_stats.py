"""Gateway-eigene Relay-Statistik: Volumen + Top-Absender/-Empfänger.

Getrennt von relay_hosts (das ist mit exo-smtp-relay gespiegelt). Diese Tests
sichern: Volumen/Aggregate werden erfasst, das Zeitfenster wird geachtet, und
das Aufräumen greift.
"""
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import relay_stats


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setattr(relay_stats, "DB_PATH", tmp_path / "relay_stats.db")
    return relay_stats


def test_merke_erfasst_volumen_und_aggregate(db):
    db.merke("10.0.0.5", absender="Drucker@x.de", empfaenger=["a@y.de", "b@y.de"], bytes_=1000)
    db.merke("10.0.0.5", absender="drucker@x.de", empfaenger=["a@y.de"], bytes_=500)
    s = db.statistik(30)
    assert s["gesamt_anzahl"] == 2 and s["gesamt_bytes"] == 1500 and s["geraete"] == 1
    top_a = {z["name"]: z for z in s["top_absender"]}
    assert top_a["drucker@x.de"]["anzahl"] == 2          # case-insensitiv aggregiert
    assert top_a["drucker@x.de"]["bytes"] == 1500
    top_e = {z["name"]: z for z in s["top_empfaenger"]}
    assert top_e["a@y.de"]["anzahl"] == 2 and top_e["a@y.de"]["bytes"] == 1500
    assert top_e["b@y.de"]["anzahl"] == 1 and top_e["b@y.de"]["bytes"] == 1000


def test_statistik_achtet_das_zeitfenster(db):
    alt = (db._jetzt() - timedelta(days=95)).strftime("%Y-%m-%d")
    with db._conn() as c:
        c.execute("INSERT INTO absender (absender, tag, anzahl, bytes) VALUES ('alt@x.de',?,9,900)", (alt,))
        c.execute("INSERT INTO tage (ip, tag, anzahl, bytes) VALUES ('1.1.1.1',?,9,900)", (alt,))
    db.merke("2.2.2.2", absender="neu@x.de", empfaenger=["z@y.de"], bytes_=100)
    s30 = db.statistik(30)
    assert s30["gesamt_anzahl"] == 1 and s30["gesamt_bytes"] == 100
    assert "alt@x.de" not in [z["name"] for z in s30["top_absender"]]     # 95 Tage > 30-Fenster
    s360 = db.statistik(360)
    assert any(z["name"] == "alt@x.de" for z in s360["top_absender"])     # aber < 360


def test_aufraeumen_loescht_aggregate(db):
    alt = (db._jetzt() - timedelta(days=db.AUFBEWAHRUNG_TAGE + 5)).strftime("%Y-%m-%d")
    with db._conn() as c:
        c.execute("INSERT INTO absender (absender, tag, anzahl, bytes) VALUES ('x@x.de',?,1,1)", (alt,))
        c.execute("INSERT INTO empfaenger (empfaenger, tag, anzahl, bytes) VALUES ('y@y.de',?,1,1)", (alt,))
        c.execute("INSERT INTO tage (ip, tag, anzahl, bytes) VALUES ('9.9.9.9',?,1,1)", (alt,))
    assert db.aufraeumen() >= 3
    assert not db.statistik(360)["top_absender"]


def test_merke_ohne_absender_zaehlt_nur_geraet(db):
    db.merke("10.0.0.9", absender="", empfaenger=[], bytes_=42)
    s = db.statistik(30)
    assert s["gesamt_anzahl"] == 1 and s["gesamt_bytes"] == 42
    assert s["top_absender"] == [] and s["top_empfaenger"] == []
