"""Technische Eingaben ohne Autokorrektur und automatische Großschreibung.

Anlass (17.11.2026): Ein Hostname kam als „Mail.firma.at“ ins Feld — die
Autokorrektur schrieb ihn groß. Für Namen, Domänen und Mailadressen ist das
nur lästig (DNS ignoriert Groß/klein), für Kennungen und Pfade falsch.

Regel: Ein <input>, dessen Platzhalter wie ein Hostname oder eine Mailadresse
aussieht (und das nicht schon type="email"/"url" ist), trägt
autocapitalize="none" (oder "off") autocorrect="off" spellcheck="false".
"""
from __future__ import annotations

import re
from pathlib import Path

VORLAGEN = Path(__file__).resolve().parent.parent / "app" / "webui" / "templates"
_INPUT = re.compile(r"<input\b[^>]*>", re.S)
_PLATZHALTER = re.compile(r'placeholder="([^"]*)"')
_TECHNISCH = re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b")


def _funde() -> list[str]:
    funde = []
    for datei in sorted(VORLAGEN.glob("*.html")):
        for m in _INPUT.finditer(datei.read_text(encoding="utf-8")):
            tag = m.group(0)
            p = _PLATZHALTER.search(tag)
            if not p or not _TECHNISCH.search(p.group(1)) or re.search(r'type="(email|url)"', tag):
                continue
            if not re.search(r'autocapitalize="(none|off)"', tag) or 'spellcheck="false"' not in tag:
                funde.append(f"{datei.name}: {tag[:120]}")
    return funde


def test_technische_eingaben_ohne_autokorrektur():
    assert not _funde(), "Ohne autocapitalize/spellcheck:\n" + "\n".join(_funde())


def test_die_regel_greift_ueberhaupt():
    """Sonst wäre der Test still grün: Es gibt Felder, die er erfasst."""
    erfasst = sum(1 for d in VORLAGEN.glob("*.html") for m in _INPUT.finditer(d.read_text(encoding="utf-8"))
                  if (p := _PLATZHALTER.search(m.group(0))) and _TECHNISCH.search(p.group(1)))
    assert erfasst >= 8, erfasst
