"""Zwei gleichnamige Tests in einer Datei: der erste läuft nie.

Python überschreibt die frühere Funktion still mit der späteren; pytest sieht
nur eine, und der Lauf bleibt grün. Beim Zusammenführen zweier Zweige wäre das
fast passiert. Die CI fing es nicht: ruff läuft nur über app/ und ohne F811.

Bewusst per AST statt ruff F811 auf tests/: F811 meldet auch die gewollten
Fixture-Importe (`from test_x import fixture  # noqa: F811`), und es braucht ein
zusätzliches Werkzeug. Geprüft werden test_-Funktionen auf Modulebene und
test_-Methoden je Klasse.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def doppelte(quelltext: str) -> list[str]:
    baum = ast.parse(quelltext)
    funde = []

    def pruefen(knoten, ort: str):
        namen = Counter(k.name for k in knoten
                        if isinstance(k, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and k.name.startswith("test"))
        funde.extend(f"{ort}{n} ({c}×)" for n, c in namen.items() if c > 1)

    pruefen(baum.body, "")
    for k in ast.walk(baum):
        if isinstance(k, ast.ClassDef):
            pruefen(k.body, f"{k.name}.")
    return funde


def test_keine_doppelten_testnamen():
    funde = []
    dateien = sorted(TESTS.glob("test_*.py"))
    assert len(dateien) > 200, "Testdateien nicht gefunden"
    for p in dateien:
        funde += [f"{p.name}: {f}" for f in doppelte(p.read_text(encoding="utf-8"))]
    assert not funde, "Gleichnamige Tests überschreiben sich still:\n" + "\n".join(funde)


def test_pruefung_erkennt_duplikate():
    assert doppelte("def test_a():\n    pass\ndef test_a():\n    pass\n") == ["test_a (2×)"]
    assert doppelte("class TestX:\n    def test_b(self): pass\n    def test_b(self): pass\n") \
        == ["TestX.test_b (2×)"]
    assert doppelte("def test_a(): pass\nclass T:\n    def test_a(self): pass\n") == []
