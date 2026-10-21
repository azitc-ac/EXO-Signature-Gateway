"""Zustandsfarben: JavaScript setzt den Zustand, das CSS färbt — und beide reden
über dieselben Werte.

Seit 2026-10-21 setzt kein Skript mehr Farben (test_meldungen_einheitlich).
Die Kehrseite: Setzt ein Skript einen Zustandswert, für den es keine Regel
gibt, bleibt die Anzeige einfach ungefärbt — kein Fehler, keine Meldung. Und
fehlt nur die Regel für den Dunkelmodus, leuchtet sie dort auf.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

WURZEL = Path(__file__).parent.parent
VORLAGEN = WURZEL / "app/webui/templates"
STYLE = (WURZEL / "app/webui/static/style.css").read_text(encoding="utf-8")
DARK = (WURZEL / "app/webui/static/dark-mode.css").read_text(encoding="utf-8")

# (Selektor des Trägers, Attribut, Werte, die das JS setzt, Vorlage, JS-Muster)
ZUSTAENDE = [
    (".arm-box", "data-arm", ["ok", "fehler"], ["setup.html", "advanced.html"],
     r"armKasten\('(?:kv|wd)-arm',\s*'(\w*)'"),
    (".zustand-text", "data-zustand", ["ok", "warnung"], ["advanced.html"],
     r"\.dataset\.zustand\s*=\s*\w+\s*\?\s*'(\w+)'"),
    ("#fu-badge", "data-fu-state", ["exceeded", "licensed", "community"], ["mailboxes.html"],
     r"badge\.dataset\.fuState\s*=\s*'(\w+)'"),
    ("#fu-bar", "data-fu-stufe", ["hoch", "ueber"], ["mailboxes.html"],
     r"'(ueber|hoch|ok)'"),
]


def _regel(css: str, traeger: str, attr: str, wert: str) -> bool:
    return f'{traeger}[{attr}="{wert}"]' in css


@pytest.mark.parametrize("traeger,attr,werte,_v,_m", ZUSTAENDE, ids=[z[0] for z in ZUSTAENDE])
def test_jeder_zustand_hat_helle_und_dunkle_regel(traeger, attr, werte, _v, _m):
    fehlt = [w for w in werte if not _regel(STYLE, traeger, attr, w)]
    assert not fehlt, f"style.css: keine Regel für {traeger}[{attr}] = {fehlt}"
    # #fu-bar ist in beiden Modi gleich (Ampel auf neutraler Spur) — nur hell nötig
    if traeger != "#fu-bar":
        fehlt = [w for w in werte if not _regel(DARK, traeger, attr, w)]
        assert not fehlt, f"dark-mode.css: keine Regel für {traeger}[{attr}] = {fehlt}"


@pytest.mark.parametrize("traeger,attr,werte,vorlagen,muster", ZUSTAENDE, ids=[z[0] for z in ZUSTAENDE])
def test_js_setzt_nur_bekannte_werte(traeger, attr, werte, vorlagen, muster):
    gesetzt = set()
    for v in vorlagen:
        gesetzt |= set(re.findall(muster, (VORLAGEN / v).read_text(encoding="utf-8")))
    gesetzt.discard("")                       # '' = Grundzustand, gefärbt über den Träger
    if traeger == "#fu-bar":
        gesetzt.discard("ok")                 # ok = Grundfarbe von #fu-bar
    assert gesetzt, f"Muster findet nichts — Prüfer veraltet ({traeger})"
    unbekannt = gesetzt - set(werte)
    assert not unbekannt, f"{traeger}: JS setzt {unbekannt}, für die keine Regel vorgesehen ist"


def test_modus_karte_und_online_punkt():
    assert ".modus-karte[data-gewaehlt]" in STYLE and ".modus-karte[data-gewaehlt]" in DARK
    assert '#nav-dot[data-online="0"]' in STYLE
    setup = (VORLAGEN / "setup.html").read_text(encoding="utf-8")
    assert setup.count('class="modus-karte"') == 3
    assert "toggleAttribute('data-gewaehlt'" in setup
