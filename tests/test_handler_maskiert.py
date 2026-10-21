"""Werte in Inline-Handlern (onclick="f('…')") laufen über escJsAttr().

Zwei Ebenen: Der Browser dekodiert erst das Attribut (&#39; → '), DANN liest er
das Skript. `esc()` allein ließ den JS-String deshalb aufbrechen — etwa beim
Entfernen einer Gruppe namens „O'Brien“ oder bei Block-IDs aus einer Vorlage,
die die schwächere Rolle „Signatur-Editor“ schreibt. Bis 2026-10-21 standen
dort an 47 Stellen esc(), escAttr(), ein `.replace(/'/g, …)` oder gar nichts.
"""
from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_knopf_aktion import common_funktion

VORLAGEN = Path(__file__).parent.parent / "app/webui/templates"
BASE = (VORLAGEN / "base.html").read_text(encoding="utf-8")

_ATTR = re.compile(r'\bon(?:click|change|input)="([^"]*)"')
# 'text' + X + 'text'  bzw.  '${X}'  innerhalb eines Handler-Strings
_VERKETTET = re.compile(r"\\'' \+ ([^+]+?) \+ '\\'")
_LITERAL = re.compile(r"'\$\{([^}]+)\}'")


def _fundstellen():
    for p in sorted(VORLAGEN.glob("*.html")):
        t = p.read_text(encoding="utf-8")
        for zeile_nr, zeile in enumerate(t.splitlines(), 1):
            for m in _VERKETTET.finditer(zeile):
                if "on" in zeile[:m.start()] and re.search(r'on(?:click|change|input)="[^"]*$', zeile[:m.start()]):
                    yield p.name, zeile_nr, m.group(1).strip()
            for a in _ATTR.finditer(zeile):
                for m in _LITERAL.finditer(a.group(1)):
                    yield p.name, zeile_nr, m.group(1).strip()


def test_handler_werte_laufen_ueber_escjsattr():
    funde = list(_fundstellen())
    assert len(funde) >= 40, f"Prüfer findet zu wenig ({len(funde)}) — veraltet?"
    roh = [f"{d}:{z}: {x}" for d, z, x in funde
           if not x.startswith("escJsAttr(") and not re.fullmatch(r"i|idx|\d+", x)]
    assert not roh, "Wert in Inline-Handler ohne escJsAttr():\n" + "\n".join(roh)


@pytest.mark.skipif(not shutil.which("node"), reason="node fehlt")
def test_rundreise():
    """Was im Skript ankommt, ist exakt der Ausgangswert."""
    m = re.search(r"function escJsAttr\(wert\) \{.*?\n\s*\}", BASE, re.S)
    assert m, "escJsAttr fehlt in base.html"
    werte = ["O'Brien", 'a"b', "c\\d", "</script><img src=x onerror=alert(1)>", "&amp;", "x\ny", ""]
    js = (common_funktion("esc") + common_funktion("escAttr") + m.group(0)
          + f"\nconsole.log(JSON.stringify({json.dumps(werte)}.map(escJsAttr)));")
    attrs = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True,
                                      check=True).stdout)
    for wert, attr in zip(werte, attrs):
        assert '"' not in attr and "<" not in attr, attr            # bricht das Attribut nicht
        quelltext = html.unescape(attr)                              # was der JS-Parser sieht
        zurueck = subprocess.run(["node", "-e", f"console.log(JSON.stringify('{quelltext}'))"],
                                 capture_output=True, text=True, check=True).stdout
        erwartet = wert.replace("\n", " ")                           # escAttr nimmt Umbrüche heraus
        assert json.loads(zurueck) == erwartet, (wert, attr)
