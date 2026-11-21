"""Gemeinsame Textbausteine für Oberfläche, Mails und Protokoll.

Mehrzahl: „1 Postfach“ / „6 Postfächer“ statt „6 Postfach/Postfächer“ oder
„Regel(n)“. Ein Verb, das an der Zahl hängt, bekommt sein eigenes `wort()`
(„1 Mail bestand“ / „4 Mails bestanden“). Gegenstück in JavaScript:
`anzahlText()` in base.html. Die Klammerform verbietet tests/test_mehrzahl.py.
"""
from __future__ import annotations


def wort(n: int, einzahl: str, mehrzahl: str) -> str:
    """Nur das Wort, passend zur Zahl."""
    return einzahl if n == 1 else mehrzahl


def zahl(n: int, einzahl: str, mehrzahl: str) -> str:
    """Zahl und Wort: „1 Regel“, „3 Regeln“, „0 Regeln“."""
    return f"{n} {wort(n, einzahl, mehrzahl)}"
