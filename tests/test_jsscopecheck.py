"""tools/jsscopecheck.js: Helfer aus base.html gelten nur für Seiten, die
base.html erweitern — nicht für eigenständige Seiten, die bloß common.js laden.

Anlass (2026-11-15): notfall.html rief escJsAttr (base.html) und lud nur
common.js. Die Prüfung hatte beide Quellen in einem Topf und meldete nichts;
im Browser warf die Einrichten-Seite „escJsAttr is not defined“.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
PRUEFUNG = WURZEL / "tools" / "jsscopecheck.js"


def _lauf(tmp_path: Path, seiten: dict[str, str]) -> str:
    if not shutil.which("node"):
        pytest.skip("node fehlt")
    vorlagen = tmp_path / "vorlagen"
    vorlagen.mkdir()
    (vorlagen / "base.html").write_text("<script>function escJsAttr(w) { return w; }</script>")
    (tmp_path / "common.js").write_text("function esc(w) { return w; }\n")
    for name, html in seiten.items():
        (vorlagen / name).write_text(html)
    erg = subprocess.run(["node", str(PRUEFUNG), str(vorlagen), str(tmp_path / "common.js")],
                         capture_output=True, text=True, cwd=WURZEL, timeout=60)
    if "Cannot find module" in erg.stderr:
        pytest.skip("acorn fehlt (npm ci)")
    return erg.stdout


def test_eigenstaendige_seite_hat_keine_base_helfer(tmp_path):
    aus = _lauf(tmp_path, {"eigen.html": '<script src="/static/common.js"></script>'
                                         "<script>esc('a'); escJsAttr('b');</script>"})
    assert "eigen.html: `escJsAttr`" in aus
    assert "`esc`" not in aus                                       # common.js ist geladen


def test_erweiternde_seite_hat_beide(tmp_path):
    aus = _lauf(tmp_path, {"seite.html": '{% extends "base.html" %}<script>esc("a"); escJsAttr("b");</script>'})
    assert "1 ungebundene" not in aus and "seite.html:" not in aus


def test_baustein_erbt_die_schwaechste_versorgung(tmp_path):
    aus = _lauf(tmp_path, {
        "_teil.html": "<script>escJsAttr('x');</script>",
        "voll.html": '{% extends "base.html" %}{% include "_teil.html" %}',
        "eigen.html": '<script src="/static/common.js"></script>{% include "_teil.html" %}<script>esc(1);</script>',
    })
    assert "_teil.html: `escJsAttr`" in aus
