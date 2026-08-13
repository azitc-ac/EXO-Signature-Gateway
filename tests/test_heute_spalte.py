"""Die Spalte "Heute" auf der Übersicht zeigt wirklich heute.

ANLASS (13.08.2026)
-------------------
Auf der Übersicht standen unter "Heute" 369 Fehler — bei 8 verarbeiteten Mails,
während "3 Tage" und der laufende Monat 0 zeigten. Die Fehler stammten sämtlich
aus dem Juli.

Ursache: Die Spalte kam aus `_stats - _snapshot`, und der Schnappschuss wird
ausschliesslich von `take_daily_snapshot()` gesetzt — aufgerufen nur vom
Tagesbericht, und der hängt in `scheduler.py` hinter
`DAILY_REPORT_ENABLED and NOTIFICATION_MAILBOX`.

Wer den Tagesbericht nicht einschaltet, sah unter "Heute" alles seit der
Installation. Das betrifft nicht nur das Entwicklungsgerät: Jedes Gateway ohne
Benachrichtigungsadresse zeigt dieselbe Zahl. Und ausgerechnet in dieser Spalte
stehen seit v1.7.180 die Kennzahlen, an denen ein Ausfall der Ketten-Erkennung
ablesbar sein soll.

Aufgefallen ist es erst, als jemand die Seite ansah — kein Test hatte je
geprüft, ob "Heute" heute meint.

⚠️ In dieser Datei stehen KEINE typografischen Anführungszeichen in
Code-Zeichenketten. Die erste Fassung hatte sie in den Meldungstexten der
Zusicherungen, und das schliessende Zeichen beendete dort die Zeichenkette —
Syntaxfehler. Genau davor warnt CLAUDE.md; es war der fünfte Fall.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

import stats


@pytest.fixture
def tagesdatei(tmp_path, monkeypatch):
    """Eigene Tagesdatei — niemals die echte anfassen."""
    f = tmp_path / "stats_daily.json"
    monkeypatch.setattr(stats, "_DAILY_FILE", f)
    return f


def _schreibe(f, eintraege: dict):
    f.write_text(json.dumps(eintraege))


def test_heute_zaehlt_nur_den_heutigen_tag(tagesdatei):
    heute = date.today().strftime("%Y-%m-%d")
    gestern = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    _schreibe(tagesdatei, {
        heute:   {"processed": 5, "errors": 1},
        gestern: {"processed": 99, "errors": 42},
    })
    h = stats.get_today()
    assert h["processed"] == 5
    assert h["errors"] == 1, "gestrige Fehler tauchen unter Heute auf"


def test_alte_zahlen_erscheinen_nicht_unter_heute(tagesdatei):
    """Der beobachtete Fall: Fehler aus dem Vormonat, heute nichts."""
    _schreibe(tagesdatei, {
        "2026-07-07": {"processed": 8, "errors": 188},
        "2026-07-26": {"errors": 40},
    })
    h = stats.get_today()
    assert h["errors"] == 0, (
        "Fehler aus dem Juli erscheinen unter Heute — genau der Fehler, "
        "wegen dessen die Spalte umgestellt wurde")
    assert h["processed"] == 0


def test_ohne_tagesdatei_alles_null(tagesdatei):
    """Frische Installation: keine Datei, keine Zahlen — kein Absturz."""
    assert not tagesdatei.exists()
    assert all(v == 0 for v in stats.get_today().values())


def test_heute_haengt_NICHT_am_schnappschuss(tagesdatei, monkeypatch):
    """⚠️ Der Kern.

    Der Schnappschuss wird nur gesetzt, wenn der Tagesbericht läuft. Hinge die
    Spalte daran, zeigte jedes Gateway ohne Benachrichtigungsadresse alles seit
    der Installation.

    Hier wird der schlimmste Fall nachgestellt: laufende Gesamtzahlen hoch,
    Schnappschuss leer (nie ein Bericht gelaufen) — und für heute steht nichts
    in der Tagesdatei. Das Ergebnis MUSS 0 sein.
    """
    hoch = {k: 0 for k in stats.KEYS}
    hoch["errors"] = 369
    monkeypatch.setattr(stats, "_stats", hoch)
    monkeypatch.setattr(stats, "_snapshot", {k: 0 for k in stats.KEYS})
    _schreibe(tagesdatei, {"2026-07-07": {"errors": 369}})
    assert stats.get_today()["errors"] == 0, (
        "Die Spalte wird aus Gesamtzahlen minus Schnappschuss gerechnet — ohne "
        "gelaufenen Tagesbericht steht dort alles seit der Installation")


def test_uebersicht_benutzt_get_today():
    """Die Datenquelle der Seite, nicht nur die Funktion für sich."""
    # Die GESAMTE Oberflaeche, nicht nur `app.py` — sonst wird diese Pruefung
    # still wirkungslos, sobald die Uebersichtsseite in ein Routenmodul zieht.
    from hilfen import webui_quelltext
    quelle = webui_quelltext()
    assert "_stats_mod2.get_today()" in quelle, \
        "Die Übersicht speist die Spalte nicht aus get_today()"
    assert "get_daily()" not in quelle, \
        "get_daily() ist wieder da — die Spalte hinge erneut am Tagesbericht"


def test_tagesbericht_behaelt_seinen_schnappschuss():
    """Gegenprobe: Der Bericht braucht die Differenz weiterhin.

    `take_daily_snapshot()` liefert den Zuwachs seit dem letzten Bericht — das
    ist für einen täglich versandten Bericht richtig und soll so bleiben. Die
    Umstellung betrifft nur die Anzeige.
    """
    from pathlib import Path
    assert hasattr(stats, "take_daily_snapshot")
    sched = (Path(__file__).resolve().parent.parent
             / "app" / "scheduler.py").read_text()
    assert "stats.take_daily_snapshot()" in sched
