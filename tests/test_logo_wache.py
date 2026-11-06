"""Erkennt die Speichern-Wache des Baukastens den Tausch eines eingebetteten Logos?

Anlass: Wer in einer Vorlage ein eingebettetes Logo gegen ein anderes tauschte,
bekam keinen Speichern-Knopf. Die Wache (`speicherWache` mit
`data-wache-container`) vergleicht nur Werte von Eingabefeldern. Ein
eingebettetes Bild steht in keinem — das URL-Feld entfällt, sobald eingebettet
ist —, also ergab „Logo A eingebettet“ und „Logo B eingebettet“ denselben Stand.
Erkannt wurde nur der Wechsel zwischen URL und Einbettung, weil dabei ein Feld
erscheint oder verschwindet.

Die Abhilfe ist ein verstecktes Feld mit einer Kennung des Bildes. Geprüft wird:
1. das Feld steht im Einbettungszweig des Logo-Formulars,
2. die Kennung unterscheidet zwei gleich lange Bilder,
3. die Wache sieht den Unterschied tatsächlich.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_knopf_aktion import common_funktion

EDITOR = Path(__file__).resolve().parents[1] / "app/webui/templates/template_editor.html"


def _editor_funktion(name: str) -> str:
    text = EDITOR.read_text(encoding="utf-8")
    m = re.search(r"function\s+" + name + r"\s*\(", text)
    assert m, name
    i, tiefe = text.index("{", m.end()), 0
    for j in range(i, len(text)):
        tiefe += (text[j] == "{") - (text[j] == "}")
        if tiefe == 0:
            return text[m.start():j + 1]
    raise AssertionError(name)


def test_einbettungszweig_traegt_bildkennung():
    text = EDITOR.read_text(encoding="utf-8")
    # Der Zweig, der bei eingebettetem Bild statt des URL-Feldes erscheint.
    assert re.search(
        r"\$\{isEmbed \? `<input type=\"hidden\" data-bild-kennung "
        r"value=\"\$\{escAttr\(_bildKennung\(b\.url\)\)\}\">` : ''\}", text), \
        "Logo-Formular ohne verstecktes Kennungsfeld — die Wache sieht einen Bildtausch nicht"


@pytest.mark.skipif(not shutil.which("node"), reason="node fehlt")
def test_wache_sieht_bildtausch():
    js = "\n".join([
        common_funktion("_speicherWert"),
        common_funktion("_speicherFelder"),
        common_funktion("_speicherStandContainer"),
        _editor_funktion("_bildKennung"),
    ]) + r"""
// Zwei gleich lange Bilder, die sich nur in einem Zeichen unterscheiden.
const a = 'data:image/png;base64,' + 'A'.repeat(5000) + 'x';
const b = 'data:image/png;base64,' + 'A'.repeat(5000) + 'y';
const behaelter = url => ({querySelectorAll: () => [
  {type: 'text', value: 'Name'},
  {type: 'hidden', value: _bildKennung(url)},
]});
console.log(JSON.stringify([
  _bildKennung(a) === _bildKennung(b),
  _speicherStandContainer(behaelter(a)) === _speicherStandContainer(behaelter(b)),
  _speicherStandContainer(behaelter(a)) === _speicherStandContainer(behaelter(a)),
  _bildKennung('').length > 0,
]));
"""
    aus = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
    assert json.loads(aus) == [False, False, True, True]
