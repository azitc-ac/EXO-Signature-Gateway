"""Ein Begriff, eine Schreibweise — und das Werkzeug dazu meldet nicht zu viel.

ANLASS (2026-08-23/24)
----------------------
Die 587-Verwirrung entstand, weil dasselbe Ding vier Namen trug: `smtp587` in
der Konfiguration, „IMAP + Graph" in der Oberfläche, „587-Modus" im Erklärtext —
den es gar nicht gibt. Kein Test schlug an, weil alle Verhalten prüfen und
keiner die Sprache daneben.

⚠️ Der zweite Test ist der wichtigere. Der erste Lauf des Prüfers meldete zwölf
Stellen, **elf davon zu Recht bestehend**:

    if s.REINJECT_MODE in ('imap','smtp587')    ← MUSS so sein, Bestandsanlagen
    „Der Altname `smtp587` bezeichnet …"        ← erklärt ihn gerade
    „einen 587-Modus gibt es nicht"             ← widerlegt ihn gerade

Ein Werkzeug, das elf richtige Stellen anmahnt, um eine falsche zu finden, wird
weggedrückt — und dann hört man es auch nicht mehr, wenn es recht hat. Nach den
zwei Ausnahmen (Vergleich im Code, erklärende Umgebung) blieb genau eine
Meldung übrig, und die war ein echter Fehler.
"""
import subprocess
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
SKRIPT = WURZEL / "tools" / "begriffecheck.py"
sys.path.insert(0, str(WURZEL / "tools"))

import begriffecheck  # noqa: E402


def test_bestand_ist_sauber():
    r = subprocess.run([sys.executable, str(SKRIPT)], cwd=str(WURZEL),
                       capture_output=True, text=True)
    assert r.returncode == 0, (
        "Eine Stelle benutzt einen Begriff, den es so nicht gibt:\n"
        + r.stdout + r.stderr)


def test_vergleich_im_code_ist_kein_sprachfehler():
    """Der Altname MUSS im Vergleich stehen — sonst brechen Bestandsanlagen."""
    for zeile in [
        """    {% if s.REINJECT_MODE in ('imap','smtp587') %}checked{% endif %}""",
        """    skip_inbound = reinject_mode in ("graph", "imap", "smtp587")""",
        """    if mode == "smtp587":""",
    ]:
        assert begriffecheck._ist_vergleich(zeile, "smtp587"), zeile


def test_erklaerende_umgebung_wird_verschont():
    """Wer den Altnamen erklärt, verwendet ihn nicht."""
    zeilen = [
        "# Rueckweg an Exchange.",
        "# `smtp587` ist ein Altname fuer `imap` — der Modus macht IMAP APPEND.",
        '    "REINJECT_MODE": "smtp",',
    ]
    assert begriffecheck._wird_erklaert(zeilen, 1)


def test_blosse_verwendung_wird_gemeldet():
    """Die Gegenrichtung: ohne Erklärung, ohne Vergleich — das ist der Fund."""
    zeilen = ["Stelle den Rueckweg auf smtp587, dann laeuft es ueber Port 587."]
    assert not begriffecheck._wird_erklaert(zeilen, 0)
    assert not begriffecheck._ist_vergleich(zeilen[0], "smtp587")


def test_ausnahmen_sind_begruendet():
    for begriff in begriffecheck.REGISTER:
        for datei, grund in begriff.ausnahmen.items():
            assert len(grund) > 15, (
                f"{begriff.name}/{datei}: Ausnahme ohne tragfähige Begründung — "
                "die kann beim nächsten Durchsehen niemand mehr einordnen.")


def test_pruefung_laeuft_in_der_ci():
    ci = (WURZEL / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    assert "tools/begriffecheck.py" in ci, (
        "Ein Prüfskript, das nur von Hand läuft, läuft irgendwann nicht mehr.")
