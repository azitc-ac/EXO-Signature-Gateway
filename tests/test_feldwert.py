"""feldWert(): EIN Weg, den getrimmten Wert eines Feldes zu lesen.

Bis 2026-10-21 stand `(document.getElementById(x).value || '').trim()` bzw.
`….value.trim()` an 89 Stellen in 14 Vorlagen.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_knopf_aktion import common_funktion

VORLAGEN = Path(__file__).parent.parent / "app/webui/templates"
OHNE_COMMON_JS = {"portal.html", "self.html", "addin_compose.html", "smime_selfservice.html"}
_ALT = re.compile(r"document\.getElementById\([^()]*\)\.value\s*(?:\|\|\s*''\)\s*)?\.trim\(\)")


def test_keine_handgelesenen_feldwerte():
    funde = [f"{p.name}:{p.read_text(encoding='utf-8').count(chr(10), 0, m.start()) + 1}"
             for p in sorted(VORLAGEN.glob("*.html")) if p.name not in OHNE_COMMON_JS
             for m in _ALT.finditer(p.read_text(encoding="utf-8"))]
    assert not funde, "Feldwert von Hand gelesen — feldWert(id) aus common.js nutzen:\n" + "\n".join(funde)


def test_pruefer_erkennt_beide_formen():
    assert _ALT.search("(document.getElementById('a').value || '').trim()")
    assert _ALT.search("document.getElementById(felder[k]).value.trim()")


@pytest.mark.skipif(not shutil.which("node"), reason="node fehlt")
def test_verhalten():
    js = common_funktion("feldWert") + r"""
const felder = {a: {value: '  x y \n'}, leer: {value: ''}, zahl: {value: 5}, nichts: {value: null}};
globalThis.document = {getElementById: id => felder[id] || null};
console.log(JSON.stringify([feldWert('a'), feldWert('leer'), feldWert('fehlt'), feldWert(felder.zahl),
                            feldWert('nichts'), feldWert(null)]));
"""
    aus = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
    assert json.loads(aus) == ["x y", "", "", "5", "", ""]
