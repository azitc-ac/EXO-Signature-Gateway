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
nicht zu deuten — sie kann „hat nicht gegriffen" heissen oder „es gab nichts
zu greifen".

GEZÄHLT WIRD DIE WIRKUNG
------------------------
`stapel_verhindert` zählt dort, wo der volle Block tatsächlich ausbleibt, nicht
dort, wo die Kette erkannt wird. Der Unterschied ist real: `#nosig` und eine
Add-in-Signatur im Verfassenbereich greifen vorher und lassen den Zweig gar
nicht laufen. Eine Kennzahl, deren Name eine Wirkung verspricht, muss die
Wirkung messen — sonst verlässt man sich auf sie und sie zählt etwas anderes.
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
    assert "stapel_verhindert" in stats.KEYS


# ── Der Text im Tagesbericht ────────────────────────────────────────────────

def test_text_nennt_immer_beide_zahlen():
    assert _ketten_text(127, 18) == "127, davon 18 Signatur-Stapel verhindert"


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
    start = quelle.index('_row("Antworten in Ketten"')
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
    assert quelle.count('_row("Antworten in Ketten"') == 2, \
        "Erwartet: je eine Zeile für heute und für den Monat"


@pytest.mark.parametrize("kennzahl", ["antworten", "stapel_verhindert"])
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
    assert 'stats.increment("stapel_verhindert")' in quelle
    stelle = quelle.index('stats.increment("antworten")')
    davor = quelle[stelle - 200:stelle]
    assert "if _bezuege" in davor, \
        "»antworten« wird ohne Prüfung auf References hochgezählt"


def test_beide_ausgaenge_tragen_dasselbe_log_merkmal():
    """⚠️ Sonst haengt die Nachprüfbarkeit an der Konfiguration.

    Der Zweig teilt sich: Ist eine Antwort-Signatur hinterlegt, wird sie
    gesetzt; sonst bleibt die Signatur ganz aus. Wer eine hinterlegt hat, sieht
    die eine Logzeile nie — wer keine hat, die andere nicht.

    Aufgefallen ist das erst, als der Betreiber nachfragte, ob die Kennzahl
    auch bei gesetzter Minimalsignatur steigt: Bei ihm IST eine hinterlegt, und
    die Zeile, nach der er hätte suchen sollen, wäre nie erschienen. Er hätte
    einen funktionierenden Fix für tot gehalten.

    Ein gemeinsames Merkmal macht `grep "SIG-KETTE"` zur vollständigen Probe.
    """
    quelle = (Path(__file__).resolve().parent.parent / "app" / "handler.py").read_text()
    zweig = quelle.index("elif not suppress_html_sig and _in_kette:")
    # Bis zum Ende des Zweigs, nicht auf gut Glück ein paar Zeichen weit: Die
    # zweite Logzeile liegt 1.861 Zeichen hinter dem `elif`, ein Fenster von
    # 1.800 schnitt sie ab — der Test war dadurch rot, ohne dass etwas kaputt
    # war. Die nächste Anweisung auf gleicher Ebene begrenzt sauber.
    ende = quelle.index("if not suppress_html_sig and not _force_sig:", zweig)
    block = quelle[zweig:ende]
    # ⚠️ Auf `log.info("SIG-KETTE:` prüfen, nicht auf `SIG-KETTE:` allein — das
    # Merkmal kommt im erklärenden Kommentar daneben ebenfalls vor. Die erste
    # Fassung zählte ihn mit und blieb deshalb grün, als das Merkmal aus einem
    # der beiden Zweige entfernt wurde: zwei Treffer, aber nur eine Logzeile.
    assert block.count('log.info("SIG-KETTE:') == 2, (
        "Beide Ausgänge des Ketten-Zweigs müssen dasselbe Log-Merkmal tragen — "
        "sonst ist die Wirkung nur bei einer der beiden Konfigurationen "
        "nachprüfbar.")


def test_zaehler_haengt_an_der_wirkung_nicht_am_befund():
    """⚠️ Gezählt wird, wo GEHANDELT wird — nicht, wo erkannt wird.

    Die erste Fassung zählte direkt nach `_in_kette = …`, also vor der
    Entscheidung. Damit hätte die Zahl auch Fälle mitgenommen, in denen der
    doppelte Block aus einem ANDEREN Grund ausblieb: `#nosig` im Betreff oder
    eine Signatur des Add-ins im Verfassenbereich — beides greift vorher und
    lässt den `elif`-Zweig gar nicht laufen. Die Kennzahl hätte sich eine
    Wirkung zugeschrieben, die woanders herkam.

    Aufgefallen ist das nicht beim Bauen, sondern durch die Frage »was wird
    da eigentlich konkret gezählt?«. Eine Kennzahl, deren Name die Wirkung
    verspricht, muss die Wirkung messen — sonst ist sie schlimmer als keine,
    weil man sich auf sie verlässt.
    """
    quelle = (Path(__file__).resolve().parent.parent / "app" / "handler.py").read_text()
    erkennung = quelle.index("_in_kette = (")
    zweig = quelle.index("elif not suppress_html_sig and _in_kette:")
    zaehler = quelle.index('stats.increment("stapel_verhindert")')
    assert erkennung < zweig < zaehler, (
        "»stapel_verhindert« wird gezählt, BEVOR feststeht, ob der volle Block "
        "wirklich unterbleibt. Der Zähler gehört in den elif-Zweig — sonst "
        "zählt er auch #nosig- und Add-in-Fälle mit.")
