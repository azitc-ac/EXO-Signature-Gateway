"""Derselbe Reiter heisst auf jeder Seite gleich.

ANLASS (2026-08-24): Sieben Seiten führten den Reiter als „Anbindung", die
Seite selbst und alle Verweise darauf als „Anbindung & Lizenzen". Wer nach dem
Lizenzbereich suchte, fand einen Reiter, der ihn nicht nannte.

Dieselbe Klasse wie das Begriffsregister (`tools/begriffecheck.py`), nur eine
Ebene konkreter: Dort geht es um Wörter im Fliesstext, hier um die Beschriftung
desselben Bedienelements an verschiedenen Stellen. Beides fällt niemandem auf,
der nur eine Seite ansieht.
"""
import re
from collections import defaultdict
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
VORLAGEN = WURZEL / "app" / "webui" / "templates"

# href → {Beschriftung: [Dateien]}
def _reiter() -> dict[str, dict[str, list[str]]]:
    gefunden: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    muster = re.compile(
        r'<a\s+href="(/[^"]*)"[^>]*class="nav-sub-tab[^"]*"[^>]*>([^<]+)</a>')
    for p in sorted(VORLAGEN.glob("*.html")):
        for ziel, text in muster.findall(p.read_text("utf-8")):
            gefunden[ziel][text.strip()].append(p.name)
    return gefunden


@pytest.mark.parametrize("ziel", sorted(_reiter()))
def test_ein_ziel_eine_beschriftung(ziel):
    """Ein Reiter, der auf dasselbe Ziel zeigt, trägt überall denselben Namen."""
    namen = _reiter()[ziel]
    assert len(namen) == 1, (
        f"Der Reiter auf {ziel} heisst unterschiedlich:\n"
        + "\n".join(f"  »{n}«: {', '.join(d)}" for n, d in sorted(namen.items()))
        + "\n\nWer nach dem Bereich sucht, findet ihn unter dem Namen nicht, "
          "den die Seite selbst trägt.")
