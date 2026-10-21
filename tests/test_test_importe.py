"""Tests importieren einander ohne `tests.`-Präfix.

Die CI ruft `pytest tests/` auf; dort ist `tests` kein Paket. Lokal mit
`python -m pytest` liegt das Arbeitsverzeichnis im Suchpfad, und
`from tests.x import …` geht durch — am 21.10.2026 deshalb lokal grün und in
der CI ein ModuleNotFoundError für die ganze Suite.
"""
import re
from pathlib import Path


def test_kein_tests_praefix():
    funde = [f"{p.name}:{i}" for p in sorted(Path(__file__).parent.glob("*.py"))
             for i, z in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
             if re.match(r"\s*(from tests\.|import tests\b)", z)]
    assert not funde, "In der CI nicht importierbar — `from test_x import …`:\n" + "\n".join(funde)
