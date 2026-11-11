"""Variablen-Vorschläge (user./custom.) auch in den einzeiligen Baustein-Feldern.

Bis v1.11.12 hing die Vorschlagsliste nur an <textarea>. Das Link-Feld des
Logos, Präfix, Anzeigetext und die Social-URL sind <input type="text"> —
dort kam keine Liste, obwohl Platzhalter beim Erzeugen ausgewertet werden.

Geprüft wird die Entscheidung `mitVorschlag(el)` selbst, ausgeführt in node
gegen nachgebildete Felder. Die Browser-Tests (test_ui_browser) laufen in der
CI nicht (kein playwright), deshalb dieser Weg.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

VORLAGE = Path(__file__).resolve().parents[1] / "app/webui/templates/template_editor.html"


def _entscheidung() -> str:
    m = re.search(r"const mitVorschlag = (el => .*?);\n", VORLAGE.read_text(encoding="utf-8"), re.S)
    assert m, "mitVorschlag fehlt in template_editor.html"
    return m.group(1)


FAELLE = [
    ({"tagName": "TEXTAREA", "dataset": {}}, True),
    ({"tagName": "INPUT", "type": "text", "dataset": {"bid": "7"}}, True),     # Logo-Link, Präfix …
    ({"tagName": "INPUT", "type": "text", "dataset": {}}, False),              # außerhalb der Bausteine
    ({"tagName": "INPUT", "type": "checkbox", "dataset": {"bid": "7"}}, False),
    ({"tagName": "INPUT", "type": "color", "dataset": {"bid": "7"}}, False),
    ({"tagName": "SELECT", "dataset": {"bid": "7"}}, False),
]


@pytest.mark.skipif(not shutil.which("node"), reason="node fehlt")
def test_welche_felder_vorschlaege_bekommen():
    skript = (f"const f = {_entscheidung()};\n"
              f"console.log(JSON.stringify({json.dumps([e for e, _ in FAELLE])}.map(e => !!f(e))));")
    aus = subprocess.run(["node", "-e", skript], capture_output=True, text=True, timeout=30)
    assert aus.returncode == 0, aus.stderr
    assert json.loads(aus.stdout) == [w for _, w in FAELLE]


def test_eingabe_ereignis_nutzt_die_entscheidung():
    quelle = VORLAGE.read_text(encoding="utf-8")
    assert "if (mitVorschlag(e.target)) zeigen(e.target);" in quelle
