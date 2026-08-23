"""Kein Test darf an einem Stichtag umkippen.

ANLASS (23.08.2026)
-------------------
`tests/test_crl_widerruf.py` hatte `JETZT = datetime(2026, 8, 16, 12, 0)` fest
verdrahtet. Die dort gebaute Sperrliste gilt sieben Tage — also bis zum 23.08.
um 12:00. Ab diesem Moment schlugen zwei Tests fehl, ohne dass jemand etwas
geändert hatte: „CRL ist bereits überfällig — nicht verwendet".

Zwei weitere Dateien trugen dieselbe Konstante mit Zertifikaten über 200 bzw.
365 Tage. Die wären im März und August 2027 gefolgt.

Ein festes Datum sieht wie Reproduzierbarkeit aus, ist hier aber das Gegenteil:
Der Test besteht nur so lange, wie die selbstgebauten Gültigkeiten zufällig noch
in die Zukunft reichen. Wer einen bestimmten Zeitpunkt prüfen will — Ablauf,
Vorlauf, Überfälligkeit — übergibt ihn ausdrücklich als Parameter. Genau das tun
mehrere Tests, und die sind davon unberührt.
"""
import re
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
DATEIEN = sorted(p for p in TESTS.glob("*.py") if p.name != Path(__file__).name)


def _codezeilen(pfad: Path):
    """Zeilen ohne Kommentare — ein Datum in einer Begründung ist keine Bombe."""
    for nr, zeile in enumerate(pfad.read_text("utf-8").splitlines(), 1):
        nackt = zeile.strip()
        if nackt.startswith("#"):
            continue
        yield nr, zeile


@pytest.mark.parametrize("pfad", DATEIEN, ids=lambda p: p.name)
def test_kein_fest_verdrahteter_zeitpunkt(pfad):
    """Ein Bezugszeitpunkt wird berechnet, nicht hingeschrieben."""
    funde = [f"Zeile {nr}: {z.strip()[:80]}"
             for nr, z in _codezeilen(pfad)
             if re.search(r"\b(?:datetime|date)\(\s*20\d\d\s*,", z)]
    assert not funde, (
        f"{pfad.name} verdrahtet einen Zeitpunkt fest:\n  " + "\n  ".join(funde)
        + "\n\nDaraus gebaute Zertifikate und Sperrlisten laufen irgendwann ab, "
          "und der Test fällt an einem Stichtag um, ohne dass jemand etwas "
          "geändert hat.\nStattdessen:\n"
          "  JETZT = datetime.now(timezone.utc).replace(hour=12, minute=0, "
          "second=0, microsecond=0)\n"
          "Mittag, damit ein Lauf nahe Mitternacht beim Formatieren nicht einen "
          "Tag daneben landet.\nEinen bestimmten Zeitpunkt prüft man, indem man "
          "ihn übergibt (jetzt=…, naechste=…), nicht indem man die Uhr anhält.")
