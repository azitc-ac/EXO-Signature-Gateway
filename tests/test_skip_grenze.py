"""Die Skip-Grenze (tests/skip_grenze.py) wirkt — im echten pytest-Lauf.

Geprüft in einem Unterprozess mit `-p skip_grenze`: ein Wegwerf-Verzeichnis
mit einer Testdatei, die sich überspringt. Ohne Grenze wäre der Lauf grün.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent


def _lauf(tmp_path, name: str, inhalt: str, **env) -> subprocess.CompletedProcess:
    (tmp_path / name).write_text(inhalt, encoding="utf-8")
    umgebung = {k: v for k, v in os.environ.items()
                if k not in ("SKIP_GRENZE", "BROWSER_PFLICHT", "PYTEST_ADDOPTS")}
    umgebung.update(PYTHONPATH=str(TESTS), **env)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "skip_grenze",
         "--rootdir", str(tmp_path), str(tmp_path / name)],
        capture_output=True, text=True, cwd=tmp_path, env=umgebung, timeout=120)


SKIP_IM_TEST = "import pytest\ndef test_a():\n    pytest.skip('fehlt')\n"
SKIP_BEIM_SAMMELN = "import pytest\npytest.importorskip('gibt_es_nicht_xyz')\ndef test_a():\n    pass\n"


@pytest.mark.parametrize("inhalt", [SKIP_IM_TEST, SKIP_BEIM_SAMMELN])
def test_fremder_skip_laesst_den_lauf_scheitern(tmp_path, inhalt):
    r = _lauf(tmp_path, "test_irgendwas.py", inhalt, SKIP_GRENZE="1")
    assert r.returncode != 0, r.stdout + r.stderr
    assert "SKIP_GRENZE" in r.stdout


def test_ohne_grenze_bleibt_skip_ein_skip(tmp_path):
    r = _lauf(tmp_path, "test_irgendwas.py", SKIP_IM_TEST)
    assert r.returncode == 0, r.stdout + r.stderr


# Für die Liste ein Skip IM Test: Ein Modul, das sich schon beim Sammeln
# überspringt, hinterlässt null Tests — pytest endet dann mit Exit 5, und der
# Gegenfall unten wäre aus dem falschen Grund rot.

def test_browsermodul_darf_im_hauptjob_fehlen(tmp_path):
    r = _lauf(tmp_path, "test_ui_browser.py", SKIP_IM_TEST, SKIP_GRENZE="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SKIP_GRENZE" not in r.stdout


def test_im_browserjob_darf_es_nicht(tmp_path):
    r = _lauf(tmp_path, "test_ui_browser.py", SKIP_IM_TEST,
              SKIP_GRENZE="1", BROWSER_PFLICHT="1")
    assert r.returncode != 0, r.stdout + r.stderr
    assert "SKIP_GRENZE" in r.stdout
