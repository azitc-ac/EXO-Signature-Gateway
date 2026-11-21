"""Aktion einer Mail im Protokoll: EIN Name und EINE Farbgruppe je Aktion.

Bis 2026-10-21 hielten Dashboard und Relay-Seite je eine eigene Tabelle aus
Namen und Hex-Farben. Die Farben kamen über eine Variable in den HTML-Text
(`style="color:${color}"`), zwei davon ohne Dunkelregel — für die Suche nach
`style="color:#…"` unsichtbar. Jetzt: `AKTIONEN`/`aktionMarke()` in base.html,
Farbe über `.aktion-marke[data-art]` in style.css + dark-mode.css.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_knopf_aktion import common_funktion

WURZEL = Path(__file__).parent.parent
VORLAGEN = WURZEL / "app/webui/templates"
BASE = (VORLAGEN / "base.html").read_text(encoding="utf-8")
STYLE = (WURZEL / "app/webui/static/style.css").read_text(encoding="utf-8")
DARK = (WURZEL / "app/webui/static/dark-mode.css").read_text(encoding="utf-8")


def _tabelle() -> dict[str, tuple[str, str]]:
    block = re.search(r"const AKTIONEN = \{(.*?)\n\s*\};", BASE, re.S)
    assert block, "AKTIONEN fehlt in base.html"
    return {k: (n, a) for k, n, a in
            re.findall(r"'?(@?\w+)'?:\s*\['([^']*)',\s*'(\w+)'\]", block.group(1))}


def _server_aktionen() -> set[str]:
    """Jede Aktion, die handler.py ins Protokoll schreibt."""
    src = (WURZEL / "app/handler.py").read_text(encoding="utf-8")
    werte = set(re.findall(r'\b_audit\("(\w+)"', src))
    werte |= set(re.findall(r'action="(\w+)"', src))
    # _action = "…" — seit v1.12.149 mehrere Zuweisungen (signiert+verschlüsselt eigen)
    zuweisungen = re.findall(r"_action = (.+)", src)
    assert zuweisungen, "Zuweisung _action in handler.py nicht gefunden — Prüfer veraltet"
    for z in zuweisungen:
        werte |= set(re.findall(r'"(\w+)"', z))
    return werte


def test_jede_server_aktion_hat_namen_und_gruppe():
    server, tabelle = _server_aktionen(), _tabelle()
    assert len(server) >= 14, f"Prüfer findet zu wenig Aktionen: {sorted(server)}"
    fehlt = server - set(tabelle)
    assert not fehlt, f"Aktion ohne Eintrag in AKTIONEN (base.html): {sorted(fehlt)}"
    # Einträge „@…“ sind Gruppen (Titel der Klickliste in der Übersicht) —
    # genau die aus mail_audit.AKTIONSGRUPPEN, keine anderen.
    import sys
    sys.path.insert(0, str(WURZEL / "app"))
    import mail_audit
    gruppen = {k for k in tabelle if k.startswith("@")}
    assert gruppen == set(mail_audit.AKTIONSGRUPPEN), (sorted(gruppen), sorted(mail_audit.AKTIONSGRUPPEN))
    tot = set(tabelle) - server - gruppen
    assert not tot, f"AKTIONEN nennt Aktionen, die der Server nie schreibt: {sorted(tot)}"
    # Jede Aktion einer Gruppe schreibt der Server wirklich.
    for g, liste in mail_audit.AKTIONSGRUPPEN.items():
        assert set(liste) <= server, f"{g}: {sorted(set(liste) - server)} schreibt der Server nie"


def test_jede_gruppe_hat_helle_und_dunkle_regel():
    for art in {a for _, a in _tabelle().values()} | {"neutral"}:
        sel = f'.aktion-marke[data-art="{art}"]'
        assert sel in STYLE, f"style.css: keine Regel {sel}"
        assert f'[data-theme="dark"] {sel}' in DARK, f"dark-mode.css: keine Regel {sel}"


def test_keine_eigene_tabelle_mehr():
    """Die Seiten benutzen den Baustein, statt Namen/Farben selbst zu halten."""
    for datei in ("dashboard.html", "relay.html"):
        t = (VORLAGEN / datei).read_text(encoding="utf-8")
        assert "aktionMarke(e.action)" in t, datei
        assert not re.search(r"smime_decrypted\s*:", t), f"{datei}: eigene Aktionstabelle"
        assert "+ color +" not in t and "${color}" not in t, f"{datei}: Farbe per Variable"


@pytest.mark.skipif(not shutil.which("node"), reason="node fehlt")
def test_marke_maskiert_und_faellt_zurueck():
    base_js = re.search(r"(const AKTIONEN = \{.*?\n\s*\};.*?function aktionMarke\(aktion\) \{.*?\n\s*\})",
                        BASE, re.S).group(1)
    skript = common_funktion("esc") + "\n" + base_js + """
console.log(JSON.stringify([aktionMarke('held'), aktionMarke('<b>x</b>'), aktionText('signed'),
                            aktionText('neu_x'), aktionMarke(null)]));
"""
    aus = subprocess.run(["node", "-e", skript], capture_output=True, text=True, check=True).stdout
    held, fremd, text, unbekannt, leer = json.loads(aus)
    assert held == '<span class="aktion-marke" data-art="warnung">Warteschlange</span>'
    assert "<b>" not in fremd and "&lt;b&gt;" in fremd and 'data-art="neutral"' in fremd
    assert text == "Signiert" and unbekannt == "neu_x"
    assert leer == '<span class="aktion-marke" data-art="neutral"></span>'
