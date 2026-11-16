"""Keine Funktion und keine Klasse zweimal im selben Gültigkeitsbereich.

ANLASS (2026-11-16): `secure_io.harden_file` stand zweimal in der Datei — erst
mit festem Modus 600, weiter unten nur einschränkend. Python nimmt
stillschweigend die spätere Definition; die frühere samt ihrer Begründung
war toter Text, und wer sie las, hielt das falsche Verhalten für gültig.
Kein Linter im Projekt meldet das, und die Datei ist mit dem Hub gespiegelt.
"""
from __future__ import annotations

import ast
import collections
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
DATEIEN = sorted([*(WURZEL / "app").rglob("*.py"), *(WURZEL / "tools").rglob("*.py")])


def doppelte(quelle: str) -> list[str]:
    """Namen, die im selben Modul oder in derselben Klasse mehrfach definiert
    werden. Ausgenommen: @property-Setter/-Deleter und @overload."""
    def ausgenommen(n) -> bool:
        for d in getattr(n, "decorator_list", []):
            text = ast.unparse(d)
            if text.endswith((".setter", ".deleter")) or text.endswith("overload"):
                return True
        return False

    funde: list[str] = []

    def scan(body, ort):
        namen = collections.Counter(
            n.name for n in body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and not ausgenommen(n))
        funde.extend(f"{ort}.{k}" for k, v in namen.items() if v > 1)
        for n in body:
            if isinstance(n, ast.ClassDef):
                scan(n.body, f"{ort}.{n.name}")

    scan(ast.parse(quelle).body, "<modul>")
    return funde


def test_erkennt_doppelte_definition():
    assert doppelte("def a():\n  pass\ndef a(x):\n  pass\n") == ["<modul>.a"]
    assert doppelte("class K:\n  def f(s): pass\n  def f(s, x): pass\n") == ["<modul>.K.f"]
    assert doppelte("class K:\n  @property\n  def p(s): pass\n  @p.setter\n  def p(s, v): pass\n") == []


@pytest.mark.parametrize("datei", DATEIEN, ids=lambda p: str(p.relative_to(WURZEL)))
def test_keine_doppelten_definitionen(datei):
    assert not doppelte(datei.read_text("utf-8")), datei
