"""Gemeinsame Vorbereitung für die Testsuite.

Die Anwendung importiert ihre Module flach (`import config`, `import secure_io`),
weil sie im Container unter `/app` liegt. Damit die Tests dieselben Importe
benutzen können — und nicht eine Sonderform, die vom Produktivpfad abweicht —
wird `app/` hier auf den Suchpfad gelegt.
"""
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP))


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
