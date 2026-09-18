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
    assert s["top_identitaeten"] == []                    # ohne Login keine Identitäts-Zeile


def test_identitaet_wird_getrennt_je_login_gezaehlt(db):
    # Ein Login, zwei Geräte-IPs — die Identität bündelt beide.
    db.merke("10.0.0.5", absender="kd@x.de", empfaenger=["a@y.de"], bytes_=1000,
             identitaet="Kundendienst@Firma.de")
    db.merke("10.0.0.9", absender="kd@x.de", empfaenger=["b@y.de"], bytes_=500,
             identitaet="kundendienst@firma.de")           # gleicher Login, andere IP
    s = db.statistik(30)
    top_i = {z["name"]: z for z in s["top_identitaeten"]}
    assert top_i["kundendienst@firma.de"]["anzahl"] == 2   # case-insensitiv gebündelt
    assert top_i["kundendienst@firma.de"]["bytes"] == 1500
    assert s["geraete"] == 2                                # IP-Sicht zählt weiter zwei Geräte


def test_top_geraete_zeigt_ip_mit_mails_und_volumen(db):
    db.merke("10.0.0.5", absender="a@x.de", empfaenger=["z@y.de"], bytes_=1000)
    db.merke("10.0.0.5", absender="a@x.de", empfaenger=["z@y.de"], bytes_=500)
    db.merke("10.0.0.9", absender="b@x.de", empfaenger=["z@y.de"], bytes_=200)
    top = {z["name"]: z for z in db.statistik(30)["top_geraete"]}
    assert top["10.0.0.5"]["anzahl"] == 2 and top["10.0.0.5"]["bytes"] == 1500
    assert top["10.0.0.9"]["anzahl"] == 1 and top["10.0.0.9"]["bytes"] == 200
    # Reihenfolge: das aktivste Gerät zuerst
    assert db.statistik(30)["top_geraete"][0]["name"] == "10.0.0.5"


def test_letzte_aktivitaet_je_identitaet(db):
    from datetime import timedelta
    heute = db._jetzt().strftime("%Y-%m-%d")
    frueher = (db._jetzt() - timedelta(days=3)).strftime("%Y-%m-%d")
    with db._conn() as c:
        c.execute("INSERT INTO identitaet (identitaet, tag, anzahl, bytes) VALUES ('kd@f.de',?,1,1)", (frueher,))
        c.execute("INSERT INTO identitaet (identitaet, tag, anzahl, bytes) VALUES ('kd@f.de',?,1,1)", (heute,))
        c.execute("INSERT INTO identitaet (identitaet, tag, anzahl, bytes) VALUES ('alt@f.de',?,1,1)", (frueher,))
    aktiv = db.letzte_aktivitaet()
    assert aktiv["kd@f.de"] == heute          # jüngster Tag gewinnt
    assert aktiv["alt@f.de"] == frueher
    assert db.letzte_aktivitaet() != {} and "gibtsnicht@f.de" not in db.letzte_aktivitaet()


def test_aufraeumen_loescht_auch_identitaeten(db):
    alt = (db._jetzt() - timedelta(days=db.AUFBEWAHRUNG_TAGE + 5)).strftime("%Y-%m-%d")
    with db._conn() as c:
        c.execute("INSERT INTO identitaet (identitaet, tag, anzahl, bytes) VALUES ('kd@x.de',?,1,1)", (alt,))
    assert db.aufraeumen() >= 1
    assert not db.statistik(360)["top_identitaeten"]


def test_routing_erfasst_ziel_volumen_und_zeit(db):
    db.merke_routing("onprem", bytes_=1000, ok=True)
    db.merke_routing("Onprem", bytes_=500, ok=True)        # case-insensitiv
    st = db.routing_statistik(30)
    assert st["gesamt_anzahl"] == 2 and st["gesamt_bytes"] == 1500
    assert st["gesamt_fehler"] == 0
    assert len(st["je_ziel"]) == 1
    z = st["je_ziel"][0]
    assert z["ziel"] == "onprem" and z["anzahl"] == 2 and z["bytes"] == 1500
    assert z["zuletzt"]                                     # Zeitpunkt gesetzt


def test_routing_zaehlt_fehler_getrennt(db):
    db.merke_routing("onprem", bytes_=100, ok=True)
    db.merke_routing("onprem", bytes_=100, ok=False)
    st = db.routing_statistik(30)
    assert st["gesamt_anzahl"] == 2                          # Fehlversuch zählt als Transaktion
    assert st["gesamt_fehler"] == 1
    assert st["je_ziel"][0]["fehler"] == 1


def test_routing_ohne_ziel_ist_noop(db):
    db.merke_routing("", bytes_=100, ok=True)
    assert db.routing_statistik(30)["gesamt_anzahl"] == 0


def test_routing_statistik_achtet_zeitfenster(db):
    alt = (db._jetzt() - timedelta(days=95)).strftime("%Y-%m-%d")
    with db._conn() as c:
        c.execute("INSERT INTO routing (ziel, tag, anzahl, bytes, fehler, zuletzt) "
                  "VALUES ('alt',?,9,900,0,'2020-01-01T00:00:00Z')", (alt,))
    db.merke_routing("neu", bytes_=100, ok=True)
    st30 = db.routing_statistik(30)
    assert st30["gesamt_anzahl"] == 1
    assert [z["ziel"] for z in st30["je_ziel"]] == ["neu"]
    assert any(z["ziel"] == "alt" for z in db.routing_statistik(360)["je_ziel"])


def test_aufraeumen_loescht_routing(db):
    alt = (db._jetzt() - timedelta(days=db.AUFBEWAHRUNG_TAGE + 5)).strftime("%Y-%m-%d")
    with db._conn() as c:
        c.execute("INSERT INTO routing (ziel, tag, anzahl, bytes, fehler, zuletzt) "
                  "VALUES ('onprem',?,1,1,0,'x')", (alt,))
    assert db.aufraeumen() >= 1
    assert db.routing_statistik(360)["je_ziel"] == []


def test_identitaet_zaehler_heute_und_zeitraum(db):
    db.merke("10.0.0.1", absender="a@x.de", empfaenger=["c@y.de"], bytes_=100, identitaet="Drucker.EG")
    db.merke("10.0.0.1", absender="a@x.de", empfaenger=["c@y.de"], bytes_=100, identitaet="drucker.eg")
    vor3 = (db._jetzt() - timedelta(days=3)).strftime("%Y-%m-%d")
    with db._conn() as c:
        c.execute("INSERT INTO identitaet (identitaet, tag, anzahl, bytes) VALUES ('drucker.eg',?,5,500)", (vor3,))
    z = db.identitaet_zaehler(30)
    assert z["drucker.eg"]["heute"] == 2         # zwei heute (case-insensitiv aggregiert)
    assert z["drucker.eg"]["zeitraum"] == 7      # 2 heute + 5 vor 3 Tagen
    assert db.heute_zaehler("Drucker.EG") == 2   # case-insensitiv
    assert db.heute_zaehler("gibtsnicht") == 0
