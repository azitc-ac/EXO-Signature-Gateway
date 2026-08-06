"""Schnelles Wechseln zwischen Vorlagen darf keine Signaturen stapeln.

ANLASS (06.08.2026)
Beim wiederholten Umschalten zwischen Vorlagen im Add-in legten sich Signaturen
übereinander — der Hintergrund stand doppelt und ging nicht mehr weg.

ZWEI WETTLÄUFE
1. Mehrere Abrufe gleichzeitig: Ihre Antworten treffen in beliebiger Reihenfolge
   ein. Eine VERALTETE Antwort setzt dann noch `_markedSig` und
   `_prevSigTextProbe`; das Ersetzen sucht anschliessend nach einer Signatur,
   die nie im Text stand — und fügt hinzu, statt zu ersetzen.
2. Lesen und Schreiben des Nachrichtentexts sind zwei Schritte. Startet
   dazwischen ein zweiter Lauf, liest er den Stand VOR dem Schreiben des ersten
   und schreibt ihn samt eigener Signatur zurück.

Geprüft wird die Vorlage als Text — die Absicherung sind zwei Merker im
JavaScript, und genau deren Vorhandensein ist die Invariante. Ein Browsertest
mit Office-Umgebung wäre hier unverhältnismässig.
"""
import re
from pathlib import Path

ADDIN = Path(__file__).resolve().parents[1] / "app/webui/templates/addin_compose.html"


def _quelle() -> str:
    return ADDIN.read_text(encoding="utf-8")


def test_abrufe_tragen_eine_laufende_nummer():
    q = _quelle()
    assert "var _ladeNr = 0;" in q
    assert "var meineNr = ++_ladeNr;" in q, "loadSig vergibt keine Nummer"


def test_veraltete_antwort_wird_verworfen():
    """Ohne diese Prüfung setzt die langsamere Antwort den Stand der neueren
    zurück — und das Ersetzen findet die falsche Signatur."""
    q = _quelle()
    i = q.index("function loadSig")
    rumpf = q[i:q.index("function loadAndApply", i)]
    assert rumpf.count("meineNr !== _ladeNr") >= 2, (
        "Erfolg- und Fehlerpfad müssen beide auf Überholung prüfen — sonst "
        "schreibt eine veraltete Antwort noch in die Anzeige.")


def test_ersetzen_laeuft_nicht_doppelt():
    q = _quelle()
    assert "var _ersetztGerade = false;" in q
    i = q.index("function replaceSig")
    rumpf = q[i:q.index("function _doInsert", i)]
    assert "if (_ersetztGerade) return;" in rumpf, "replaceSig ohne Sperre"
    assert "_ersetztGerade = true;" in rumpf
    # Und wieder frei — sonst bleibt die Schaltfläche für immer wirkungslos.
    assert rumpf.count("_ersetztGerade = false;") >= 2, (
        "Die Sperre wird nicht auf allen Wegen gelöst (Erfolg UND Lesefehler) — "
        "sonst lässt sich nach einem Fehler nie wieder ersetzen.")


def test_sperre_wird_auch_bei_lesefehler_geloest():
    """Der Weg, den man beim Aufräumen übersieht."""
    q = _quelle()
    i = q.index("function replaceSig")
    rumpf = q[i:q.index("function _doInsert", i)]
    fehlerzweig = rumpf[rumpf.index("Lesevorgang fehlgeschlagen") - 200:
                        rumpf.index("Lesevorgang fehlgeschlagen")]
    assert "_ersetztGerade = false;" in fehlerzweig, (
        "Nach einem Lesefehler bliebe die Sperre stehen.")


def test_auch_der_einstieg_ist_gesperrt():
    """Zweite Schutzschicht: `loadAndApply` startet gar nicht erst, solange ein
    Ersetzen läuft — sonst liefe ein überflüssiger Abruf ins Leere."""
    q = _quelle()
    i = q.index("function loadAndApply")
    rumpf = q[i:q.index("function _autoInsertIfNew", i)]
    assert "if (_ersetztGerade) return;" in rumpf, (
        "loadAndApply startet auch während eines laufenden Ersetzens")
