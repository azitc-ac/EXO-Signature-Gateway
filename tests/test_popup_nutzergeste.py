"""Fenster öffnen: EIN Weg — `fensterImKlick()` / `anmeldeFenster()` in common.js.

Anlass (2026-10-20): Sieben Stellen öffneten Fenster in vier Bauweisen (Name,
Ersatzweg, Meldung). Die jüngste (Berechtigungsblock) öffnete das Fenster erst
nach einem `await`. Damit gilt der Klick nicht mehr als Nutzergeste, der Browser
blockierte das Fenster stumm, und der Knopf schien nichts zu tun. Die
Konsolidierung vom Juli hatte „Fenster öffnen“ nie als Kategorie, deshalb wuchs
es nach. Jetzt erzwungen: `window.open` gibt es nur in common.js, und dort vor
jedem `await`.
"""
from __future__ import annotations

import re
from pathlib import Path

WURZEL = Path(__file__).parent.parent / "app/webui"
COMMON = WURZEL / "static/common.js"


def _dateien():
    for p in sorted(WURZEL.rglob("*")):
        if p.suffix in (".html", ".js") and "vendor" not in p.parts and p != COMMON:
            yield p


def test_window_open_nur_in_common_js():
    treffer = []
    for p in _dateien():
        for nr, zeile in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if "window.open(" in zeile:
                treffer.append(f"{p.relative_to(WURZEL)}:{nr}: {zeile.strip()[:90]}")
    assert not treffer, ("Fenster nur über fensterImKlick()/anmeldeFenster() aus common.js öffnen:\n"
                         + "\n".join(treffer))


def _funktion(js: str, name: str) -> str:
    m = re.search(r"(async\s+)?function\s+" + name + r"\s*\(", js)
    assert m, name
    tiefe, i = 0, js.index("{", m.end())
    for j in range(i, len(js)):
        tiefe += {"{": 1, "}": -1}.get(js[j], 0)
        if tiefe == 0:
            return js[m.start():j + 1]
    raise AssertionError(name)


def test_anmeldefenster_oeffnet_vor_dem_ersten_await():
    js = COMMON.read_text(encoding="utf-8")
    rumpf = _funktion(js, "anmeldeFenster")
    assert rumpf.index("fensterImKlick(") < rumpf.index("await")
    assert "window.open('about:blank'" in _funktion(js, "fensterImKlick")
    assert "focus()" in _funktion(js, "fensterImKlick")


def test_berechtigungen_nutzt_anmeldefenster():
    # Der Klick-Handler öffnet das Fenster über anmeldeFenster() VOR dem ersten
    # await — die Adresse holt anmeldeFenster selbst (seit v1.10.27 über eine
    # Funktion, die zusätzlich meldet, ob die Anmeldung auf localhost endet).
    t = (WURZEL / "templates/_berechtigungen.html").read_text(encoding="utf-8")
    start = t.index("knopf.addEventListener('click'")
    rumpf = t[start:t.index("pruefKnopf.addEventListener", start)]
    assert "anmeldeFenster(" in rumpf
    vor_fenster = rumpf[:rumpf.index("anmeldeFenster(")]
    assert "await" not in vor_fenster.replace("async function", ""), \
        "await vor dem Öffnen — der Klick gilt dann nicht mehr als Nutzergeste"
    assert "/api/setup/berechtigungen/erteilen" in rumpf[rumpf.index("anmeldeFenster("):]
