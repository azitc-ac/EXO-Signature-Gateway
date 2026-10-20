"""Anmeldefenster müssen IM Klick geöffnet werden, nicht nach einem `await`.

Nach einem `await` gilt der Klick nicht mehr als Nutzergeste. Der Browser
blockiert das Popup dann stumm, und der Knopf scheint nichts zu tun. Der
Berechtigungsblock (_berechtigungen.html) tat genau das, als einzige von sieben
Stellen. Regel: Ein window.open mit echter Adresse, dem in derselben Funktion ein
`await` vorausgeht, braucht davor ein window.open('about:blank' | '') als Platzhalter.
"""
from __future__ import annotations

import re
from pathlib import Path

VORLAGEN = Path(__file__).parent.parent / "app/webui/templates"
_OPEN = re.compile(r"window\.open\(\s*([^,)]*)")
# Anfang der umgebenden ASYNC-Funktion (nur dort kann ein `await` stehen). Innere
# Helfer wie `.catch(function(){return {};})` verdecken ihn so nicht.
_FUNKTION = re.compile(r"\basync\s+function\b|\basync\s*\(")


def _verstoesse(text: str) -> list[str]:
    gefunden = []
    for m in _OPEN.finditer(text):
        arg = m.group(1).strip()
        if arg in ("'about:blank'", '"about:blank"', "''", '""'):
            continue
        anfang = max((f.start() for f in _FUNKTION.finditer(text, 0, m.start())), default=0)
        davor = text[anfang:m.start()]
        if "await" not in davor:
            continue
        if re.search(r"window\.open\(\s*('about:blank'|\"about:blank\"|''|\"\")", davor):
            continue
        zeile = text.count("\n", 0, m.start()) + 1
        gefunden.append(f"Zeile {zeile}: window.open({arg}…) nach await ohne Platzhalterfenster")
    return gefunden


def test_kein_popup_nach_await_ohne_platzhalter():
    fehler = []
    for p in sorted(VORLAGEN.rglob("*.html")):
        fehler += [f"{p.name} {v}" for v in _verstoesse(p.read_text(encoding="utf-8"))]
    assert not fehler, "\n".join(fehler)


def test_pruefer_erkennt_das_muster():
    schlecht = "k.addEventListener('click', async function () {\n var d = await f();\n window.open(d.auth_url, 'x');\n});"
    gut = "k.addEventListener('click', async function () {\n var w = window.open('about:blank', 'x');\n var d = await f();\n w.location.href = d.auth_url;\n});"
    assert _verstoesse(schlecht)
    assert not _verstoesse(gut)


def test_berechtigungen_meldet_blockiertes_fenster():
    t = (VORLAGEN / "_berechtigungen.html").read_text(encoding="utf-8")
    assert "Der Browser hat das Anmeldefenster blockiert" in t
    assert "win.focus()" in t
