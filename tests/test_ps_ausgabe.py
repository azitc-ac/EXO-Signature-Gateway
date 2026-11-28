"""PowerShell-Ausgabe geht über EINEN Weg (ps_text.ausgabe), ohne ANSI-Farbcodes.

Anlass (28.11.2026): Unter „Gespeichert“ stand nach dem Speichern der
Postfachliste die rohe Warnung „[33;1mWARNING: …“.
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import ps_text

WURZEL = Path(__file__).resolve().parent.parent


def test_farbcodes_entfernt():
    p = SimpleNamespace(stdout="\x1b[33;1mWARNING: nichts geändert\x1b[0m\nOK", stderr=None)
    assert ps_text.ausgabe(p) == "WARNING: nichts geändert\nOK"


def test_niemand_setzt_die_ausgabe_von_hand_zusammen():
    muster = re.compile(r"proc\.stdout\s*\+\s*\"\\n\"\s*\+\s*proc\.stderr")
    treffer = [f"{d.relative_to(WURZEL)}" for d in (WURZEL / "app").rglob("*.py")
               if d.name != "ps_text.py" and muster.search(d.read_text(encoding="utf-8"))]
    assert treffer == [], treffer
