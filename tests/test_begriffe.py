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


# ── Jede Registerregel muss zünden ───────────────────────────────────────────
#
# ANLASS (2026-08-25): Beim Eintragen der Connector-Regel fiel auf, dass keiner
# der bestehenden Tests prüft, ob ein Muster im Register überhaupt JEMALS
# greift. Genau das war beim „eingeschaltet"-Muster schon einmal der Fall: Die
# erste Fassung erlaubte nur EIN Wort zwischen „für" und „eingeschaltet" und
# fand den vom Nutzer gemeldeten Satz nicht. Der Bestand war sauber, der Test
# grün — und das Werkzeug blind.
#
# Ein Muster, das nichts fängt, ist schlimmer als keines: Es sieht im Register
# nach Absicherung aus.
#
# Je Regel ein Satz, der GEFANGEN werden muss, und einer, der in Ruhe zu lassen
# ist. Der zweite Teil ist der wichtigere — ein Muster, das zu viel fängt, wird
# weggedrückt, und dann hört man es auch nicht mehr, wenn es recht hat.
PROBEN = {
    "Rückweg-Modi": (
        ["Im 587-Modus läuft die Zustellung anders.",
         "Der Graph-only-Modus kann kein S/MIME."],
        ["Im Modus `graph` läuft die Zustellung über sendMail.",
         "Port 587 ist ein Sonderweg innerhalb von `imap`."],
    ),
    "Altname smtp587": (
        ["Stelle den Rückweg auf smtp587, dann läuft es über Port 587."],
        ["Der Modus heisst `imap`."],
    ),
    "aktiviert statt eingeschaltet": (
        ["Das Postfach ist für S/MIME eingeschaltet.",
         "Die Signatur ist für dieses Postfach nicht eingeschaltet."],
        ["Das Postfach ist für S/MIME aktiviert.",
         "Standard: eingeschaltet.",
         "Der Schalter ist eingeschaltet."],
    ),
    "Connector nicht übersetzen": (
        ["Dafür muss der Exchange-Verbinder das Weiterleiten erlauben.",
         "Exchange erkennt das Gateway am Zertifikat des Verbinders.",
         "Bei mehreren Verbindern gilt der erste."],
        ["Dafür muss der Exchange-Connector das Weiterleiten erlauben.",
         "Die Verbindung wird über TLS aufgebaut.",
         "Der Connector verbindet Exchange mit dem Gateway."],
    ),
}


def _faengt(begriff, text: str) -> bool:
    import re
    return any(re.search(muster, text) for muster in begriff.verboten)


def test_jede_regel_hat_proben():
    """Eine neue Regel ohne Probe wäre eine Regel, die niemand geprüft hat."""
    ohne = [b.name for b in begriffecheck.REGISTER if b.name not in PROBEN]
    assert not ohne, (
        f"Diese Registerregeln haben keine Probe: {ohne}\n"
        "Ohne Probe ist nicht belegt, dass das Muster jemals zündet — und ein "
        "Muster, das nichts fängt, sieht im Register nach Absicherung aus.")


def test_jede_regel_faengt_ihre_beispiele():
    for begriff in begriffecheck.REGISTER:
        treffer, ruhe = PROBEN[begriff.name]
        for text in treffer:
            assert _faengt(begriff, text), (
                f"{begriff.name}: {text!r} müsste gefangen werden, wird es aber "
                "nicht — das Muster ist zu eng.")
        for text in ruhe:
            assert not _faengt(begriff, text), (
                f"{begriff.name}: {text!r} ist richtig formuliert und wird "
                "trotzdem angemahnt — das Muster ist zu weit.")
