"""Gemeinsame Vorbereitung für die Testsuite.

Die Anwendung importiert ihre Module flach (`import config`, `import secure_io`),
weil sie im Container unter `/app` liegt. Damit die Tests dieselben Importe
benutzen können — und nicht eine Sonderform, die vom Produktivpfad abweicht —
wird `app/` hier auf den Suchpfad gelegt.

⚠️ DATA_DIR wird gesetzt, BEVOR irgendein Anwendungsmodul importiert wird.

Die Module lesen `config.DATA_DIR` beim Import und legen daraus ihre Pfade fest
(`settings_store.SETTINGS_FILE`, `smime_store.SMIME_DIR`, …). Wer die Variable
erst danach umbiegt, ändert nichts mehr — die Konstanten stehen bereits.

Vorher stand in jedem Modul das Literal `"/app/data"`, und jeder Test musste
sein Modul einzeln umbiegen (`tests/test_seiten.py` führt dafür eine Liste).
Wer eines vergass, schrieb während des Testlaufs in das ECHTE Datenverzeichnis
— also dorthin, wo CLIENT_SECRET und MAILBOX_CONFIG liegen. Am 26.07.2026 ist
das beinahe passiert. Ein Verzeichnis, das es im Testlauf gar nicht gibt, kann
niemand versehentlich treffen: Der Pfad zeigt auf ein Wegwerf-Verzeichnis, und
`/app/data` existiert auf dem Entwicklungsrechner ohnehin nicht.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

_TEST_DATA = Path(tempfile.mkdtemp(prefix="exo-tests-data-"))
os.environ.setdefault("DATA_DIR", str(_TEST_DATA))

APP = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP))


def test_datenverzeichnis() -> Path:
    """Das Wegwerf-Verzeichnis dieses Laufs — für Tests, die es brauchen."""
    return _TEST_DATA


@pytest.fixture
def data_dir(tmp_path):
    """Leeres Datenverzeichnis. NIEMALS gegen /app/data testen — ein
    versehentlicher Schreibvorgang würde dort CLIENT_SECRET und MAILBOX_CONFIG
    überschreiben (in der Entwicklung am 2026-07-26 beinahe passiert).

    Nach dem Test werden die Rechte wieder gelockert: die Tests härten
    absichtlich auf 600/700, und pytest bekommt seinen temporären Baum danach
    nicht mehr weggeräumt (`PermissionError` beim `rm_rf`). Die Warnungen
    daraus würden in der CI echte Meldungen überdecken.
    """
    d = tmp_path / "data"
    d.mkdir()
    yield d
    for p in sorted(d.rglob("*"), reverse=True):
        try:
            p.chmod(0o700 if p.is_dir() else 0o600)
        except OSError:
            pass


def mode_of(path: Path) -> str:
    """Rechte als dreistellige Oktalzeichenkette, z.B. '600'."""
    import stat
    return oct(stat.S_IMODE(path.stat().st_mode))[-3:]
