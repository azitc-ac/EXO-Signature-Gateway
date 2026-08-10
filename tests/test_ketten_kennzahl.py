"""Die Ketten-Erkennung ist im Betrieb sichtbar — Tagesbericht und Übersicht.

WOFÜR
-----
Eine Schutzfunktion, deren Wirken nirgends erscheint, kann beliebig lange
ausfallen. Die Erkennung „in dieser Kette wurde schon signiert" lief vom
29.07. bis 10.08.2026 zwölf Tage wirkungslos mit: Empfänger bekamen die
Signatur doppelt, keiner der damals 500 Tests schlug an, und im Protokoll
stand es zwar — nur sieht dorthin im Normalbetrieb niemand.

Dieselbe Lehre steht schon einmal im Bestand, bei `lexware_unbekannt`: „Der
Lexware-Fix lief von Juli bis August 2026 wirkungslos mit, weil ein
Vorlagenwechsel unbemerkt blieb." Zweimal dasselbe Muster ist kein Einzelfall.

DAS BESONDERE HIER
------------------
Bei den übrigen Kennzahlen ist eine Null unauffällig und wird deshalb
ausgeblendet. Hier ist die **Null die Meldung** — sie bedeutet, dass die
Signatur womöglich doppelt hinausgeht. Die Zeile muss also IMMER erscheinen.

Und sie braucht ihre Bezugsgröße: Ohne die Zahl der Antworten wäre eine Null
nicht zu deuten — sie kann „nichts erkannt, obwohl es zu erkennen gab" heissen
oder „niemand hat auf eine eigene Kette geantwortet".
"""
from __future__ import annotations

from pathlib import Path

import pytest

import stats
from notification import _ketten_farbe, _ketten_text

VORLAGEN = Path(__file__).resolve().parent.parent / "app" / "webui" / "templates"


def test_beide_zaehler_sind_deklariert():
    """Ohne Eintrag in KEYS wird nichts gezählt und nichts gespeichert."""
    assert "antworten" in stats.KEYS
    assert "sig_kette_erkannt" in stats.KEYS


# ── Der Text im Tagesbericht ────────────────────────────────────────────────

def test_text_nennt_immer_beide_zahlen():
    assert _ketten_text(127, 18) == "127, davon 18 bereits signiert"


def test_text_bei_null_erkannt_zeigt_die_null():
    """Der stille Ausfall sieht genau so aus — er darf nicht verschwinden."""
    t = _ketten_text(127, 0)
    assert "127" in t and "0" in t


def test_ohne_antworten_bleibt_es_schlicht():
    """Kein Alarm, wo es nichts zu erkennen gab."""
    assert _ketten_text(0, 0) == "0"


# ── Die Einfärbung ──────────────────────────────────────────────────────────

def test_viele_antworten_und_nichts_erkannt_wird_hervorgehoben():
    assert _ketten_farbe(127, 0) != ""


def test_wenige_antworten_ohne_treffer_sind_kein_alarm():
    """Bei drei Antworten am Tag ist eine Null völlig normal."""
    assert _ketten_farbe(3, 0) == ""


def test_treffer_vorhanden_ist_nie_ein_alarm():
    assert _ketten_farbe(127, 18) == ""
    assert _ketten_farbe(127, 1) == ""


# ── Sichtbarkeit in Tagesbericht und Übersicht ──────────────────────────────

def test_tagesbericht_zeigt_die_zeile_bedingungslos():
    """⚠️ Der Kern. Würde jemand die Zeile — wie bei den übrigen Kennzahlen —
    nur bei einem Wert grösser null zeigen, wäre genau der Ausfall unsichtbar,
    für den sie da ist.

    Geprüft wird am Quelltext: Die Zeile darf nicht in einem `if … else ""`
    stehen, wie es `lexware_unbekannt` bewusst tut.
    """
    quelle = (Path(__file__).resolve().parent.parent / "app" / "notification.py").read_text()
    start = quelle.index('_row("Antworten auf Ketten"')
    # Der Ausdruck bis zum Ende des Aufrufs — dort darf kein Bedingungsoperator
    # stehen, der die Zeile bei 0 unterdrückt.
    ausschnitt = quelle[start:start + 400]
    zeilen = ausschnitt.split("\n")[:4]
    assert not any(" if " in z and " else " in z for z in zeilen), (
        "Die Ketten-Zeile im Tagesbericht steht unter einer Bedingung — bei 0 "
        "würde sie verschwinden, und genau die 0 ist hier die Meldung.")


def test_tagesbericht_fuehrt_beide_zeitraeume():
    """Ein einzelner Tag schwankt; erst der Monat zeigt einen Abriss."""
    quelle = (Path(__file__).resolve().parent.parent / "app" / "notification.py").read_text()
    assert quelle.count('_row("Antworten auf Ketten"') == 2, \
        "Erwartet: je eine Zeile für heute und für den Monat"


@pytest.mark.parametrize("kennzahl", ["antworten", "sig_kette_erkannt"])
def test_uebersicht_zeigt_beide_kennzahlen(kennzahl):
    """Die Übersicht ist der Ort, an den der Betreiber ohnehin sieht."""
    dashboard = (VORLAGEN / "dashboard.html").read_text()
    assert f"stats_daily.{kennzahl}" in dashboard, \
        f"{kennzahl} fehlt auf der Übersicht"
    assert f"stats_monthly.{kennzahl}" in dashboard, \
        f"{kennzahl} fehlt in der Monatsspalte der Übersicht"


def test_handler_zaehlt_beide_und_nur_bei_antworten():
    """Die Bezugsgrösse darf nur für echte Antworten hochgezählt werden —
    sonst stünde dort die Gesamtzahl aller Mails und das Verhältnis wäre
    wertlos."""
    quelle = (Path(__file__).resolve().parent.parent / "app" / "handler.py").read_text()
    assert 'stats.increment("antworten")' in quelle
    assert 'stats.increment("sig_kette_erkannt")' in quelle
    stelle = quelle.index('stats.increment("antworten")')
    davor = quelle[stelle - 200:stelle]
    assert "if _bezuege" in davor, \
        "»antworten« wird ohne Prüfung auf References hochgezählt"
