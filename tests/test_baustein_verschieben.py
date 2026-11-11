"""Bausteine in Kasten / Zwei Spalten hinein und heraus verschieben (v1.11.27).

Wunsch aus einem Kundentermin (11.11.2026): Bisher ließ sich ein Baustein nur
durch Löschen und Neuanlegen in einen Behälter bringen. Geprüft wird die
Datenlogik selbst — die Funktionen werden aus der Vorlage gezogen und in node
ausgeführt (die Browser-Tests laufen in der CI nicht).
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

VORLAGE = (Path(__file__).resolve().parents[1] / "app/webui/templates/template_editor.html").read_text(encoding="utf-8")


def _funktion(name):
    i = VORLAGE.index(f"function {name}(")
    j = VORLAGE.index("{", i)
    tiefe = 0
    for k in range(j, len(VORLAGE)):
        tiefe += {"{": 1, "}": -1}.get(VORLAGE[k], 0)
        if tiefe == 0:
            return VORLAGE[i:k + 1]
    raise AssertionError(name)


def _konstante(name, ende):
    m = re.search(rf"const {name} = .*?{re.escape(ende)}", VORLAGE, re.S)
    assert m, name
    return m.group(0)


SZENARIO = """
const neu = () => [
  {id:'1', type:'name_field'},
  {id:'2', type:'box', children:[{id:'3', type:'phone'},
                                 {id:'4', type:'two_col', left:[{id:'5', type:'logo'}], right:[]}]},
  {id:'6', type:'two_col', left:[], right:[]},
  {id:'7', type:'box', children:[]},
];
const z = id => verschiebeZiele(findBlock(id)).map(x => x.wert).sort();
const aus = {};
blocks = neu();
aus.ziele1 = z('1'); aus.ziele2 = z('2'); aus.ziele4 = z('4'); aus.ziele5 = z('5');
aus.v1 = verschiebeDaten('1', '2:children');
aus.oben_nach_v1 = blocks.map(b => b.id);
aus.box2 = findBlock('2').children.map(b => b.id);
aus.v5 = verschiebeDaten('5', 'oben');
aus.links4 = findBlock('4').left.map(b => b.id);
aus.oben_nach_v5 = blocks.map(b => b.id);
blocks = neu();
aus.kasten_in_spalte = verschiebeDaten('2', '6:left');
aus.in_sich = verschiebeDaten('6', '6:left');
aus.in_eigenes_kind = verschiebeDaten('2', '4:left');
aus.unveraendert = JSON.stringify(blocks) === JSON.stringify(neu());
console.log(JSON.stringify(aus));
"""


@pytest.fixture(scope="module")
def ergebnis():
    if not shutil.which("node"):
        pytest.skip("node fehlt")
    teile = [_konstante("UNTER_TYPEN", "];"), _konstante("KASTEN_TYPEN", ";"),
             _konstante("LISTEN_NAMEN", "};"), _konstante("TYPE_LABELS", "};"),
             "let blocks = [];"]
    teile += [_funktion(n) for n in ("kindListen", "findBlock", "elternListe", "enthaelt",
                                     "zielTypen", "verschiebeZiele", "verschiebeDaten")]
    aus = subprocess.run(["node", "-e", "\n".join(teile) + SZENARIO],
                         capture_output=True, text=True, timeout=30)
    assert aus.returncode == 0, aus.stderr
    return json.loads(aus.stdout)


def test_ziele_eines_bausteins_der_hauptebene(ergebnis):
    assert ergebnis["ziele1"] == sorted(["2:children", "4:left", "4:right",
                                         "6:left", "6:right", "7:children"])


def test_kasten_laesst_sich_nirgends_hineinschieben(ergebnis):
    assert ergebnis["ziele2"] == []


def test_zweispalter_im_kasten(ergebnis):
    """Heraus auf die Hauptebene oder in einen anderen Kasten — nicht in einen
    Zweispalter (dort nicht erlaubt) und nicht in die eigene Spalte."""
    assert ergebnis["ziele4"] == sorted(["oben", "7:children"])


def test_baustein_in_spalte(ergebnis):
    assert ergebnis["ziele5"] == sorted(["oben", "2:children", "4:right",
                                         "6:left", "6:right", "7:children"])


def test_hinein_und_heraus(ergebnis):
    assert ergebnis["v1"] is True
    assert ergebnis["oben_nach_v1"] == ["2", "6", "7"]
    assert ergebnis["box2"] == ["3", "4", "1"]
    assert ergebnis["v5"] is True
    assert ergebnis["links4"] == []
    assert ergebnis["oben_nach_v5"] == ["2", "6", "7", "5"]


def test_verbotene_ziele_aendern_nichts(ergebnis):
    assert ergebnis["kasten_in_spalte"] is False
    assert ergebnis["in_sich"] is False
    assert ergebnis["in_eigenes_kind"] is False
    assert ergebnis["unveraendert"] is True
