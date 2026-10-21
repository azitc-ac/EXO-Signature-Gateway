"""Hinweiszeile in einer Tabelle („Noch keine …“, „Wird geladen …“, Fehler)
kommt aus tabelleHinweis() in base.html.

Bis 2026-10-21 bauten 20 Stellen die Zeile von Hand, in vier Stilen
(#888, #94a3b8 mit eigenem Abstand, .muted, zentriert), und eine
Fehlerzeile erschien gedämpft statt rot.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

VORLAGEN = Path(__file__).parent.parent / "app/webui/templates"
BASE = (VORLAGEN / "base.html").read_text(encoding="utf-8")
# im Skript zugewiesen — statisches HTML in der Vorlage ist davon nicht betroffen
_HAND = re.compile(r"""innerHTML\s*=\s*\n?\s*['`]<tr><td colspan""")


def test_keine_handgebaute_hinweiszeile():
    funde = [f"{p.name}:{p.read_text(encoding='utf-8').count(chr(10), 0, m.start()) + 1}"
             for p in sorted(VORLAGEN.glob("*.html")) if p.name != "base.html"
             for m in _HAND.finditer(p.read_text(encoding="utf-8"))]
    assert not funde, "Hinweiszeile von Hand — tabelleHinweis() aus base.html:\n" + "\n".join(funde)


@pytest.mark.skipif(not shutil.which("node"), reason="node fehlt")
def test_verhalten():
    fn = re.search(r"function tabelleHinweis\(.*?\n\s*\}\n", BASE, re.S).group(0)
    js = fn + r"""
const tb = {innerHTML: ''}, aus = [];
globalThis.document = {getElementById: id => id === 'x' ? tb : null};
tabelleHinweis(tb, 3, 'Noch nichts.'); aus.push(tb.innerHTML);
tabelleHinweis('x', 5, 'Fehler: kaputt', true); aus.push(tb.innerHTML);
tabelleHinweis('fehlt', 2, 'egal');                       // wirft nicht
console.log(JSON.stringify(aus));
"""
    a, b = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
    assert a == '<tr><td colspan="3" class="muted">Noch nichts.</td></tr>'
    assert b == '<tr><td colspan="5" style="color:#991b1b">Fehler: kaputt</td></tr>'
