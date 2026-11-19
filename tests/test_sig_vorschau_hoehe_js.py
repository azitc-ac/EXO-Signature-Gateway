"""Signatur-Vorschau (static/sig_preview.js): Höhe auch nach dem Sichtbarwerden.

Lab 19.11.: Im Vorlagen-Editor wird die als unwirksam markierte Vorlage in ein
geschlossenes <details> gerendert. Dort misst scrollHeight 0; gemessen wurde
nur beim Laden (+400 ms) — nach dem Aufklappen blieb der Rahmen bei 60 px und
schnitt den Inhalt ab. Seit v1.12.107 misst ein ResizeObserver neu.

In node mit nachgebautem DOM: Der Test MUSS fehlschlagen, wenn der
Beobachter fehlt.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

DATEI = Path(__file__).resolve().parents[1] / "app/webui/static/sig_preview.js"

SKRIPT = r"""
var beobachter = [];
globalThis.ResizeObserver = function (cb) { this.cb = cb; beobachter.push(this); };
ResizeObserver.prototype.observe = function (el) { this.el = el; };
var body = { scrollHeight: 0 };
var rahmen = { style: {}, setAttribute: function () {}, addEventListener: function (n, f) { this['on' + n] = f; },
               contentDocument: { body: body } };
globalThis.document = { createElement: function () { return rahmen; } };
var box = { innerHTML: '', appendChild: function () {} };
__QUELLE__
sigVorschau(box, '<p>Max</p>');
rahmen.onload();                         // im geschlossenen <details>: Höhe 0
var vorher = rahmen.style.height;
body.scrollHeight = 140;                 // aufgeklappt — jetzt hat der Inhalt Höhe
beobachter.forEach(function (b) { if (b.el === rahmen) b.cb([]); });
console.log(JSON.stringify({ vorher: vorher, nachher: rahmen.style.height, zahl: beobachter.length }));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node fehlt")
def test_rahmen_waechst_nach_dem_aufklappen():
    skript = SKRIPT.replace("__QUELLE__", DATEI.read_text(encoding="utf-8").replace("'use strict';", ""))
    aus = subprocess.run(["node", "-e", skript], capture_output=True, text=True, timeout=30)
    assert aus.returncode == 0, aus.stderr
    erg = json.loads(aus.stdout)
    assert erg["vorher"] == "8px"
    assert erg["nachher"] == "148px", erg
