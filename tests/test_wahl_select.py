"""Vorlagen-Auswahl einer Postfachzeile (mailboxes.html, _buildWahlSelect).

Folgt ein Postfach dem Standard, übergab die Zeile STANDARD_WAHL als `aktuell`
an _fuelleVorlagenSelect. Dessen Sicherheitsnetz (fremde Werte trotzdem
anzeigen) hängte den Wert als Vorlage an: je Zeile eine zweite Option
„__standard__ (andere Art)“ mit demselben Wert wie „Standard (…)“.

Das Skript läuft in Node gegen ein kleines nachgebautes DOM; `sel.value = x`
wählt wie im Browser die ERSTE passende Option.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SEITE = Path(__file__).parent.parent / "app/webui/templates/mailboxes.html"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node fehlt")

_DOM = r"""
class El {
  constructor(tag){ this.tagName = tag; this.children = []; this.dataset = {}; this.style = {};
    this.textContent = ''; this._v = ''; this.selected = false; this.className = ''; this.disabled = false; }
  set innerHTML(h){ this.children = []; }
  appendChild(c){ this.children.push(c); return c; }
  insertBefore(c, ref){ const i = this.children.indexOf(ref);
    this.children.splice(i < 0 ? this.children.length : i, 0, c); return c; }
  get firstChild(){ return this.children[0] || null; }
  addEventListener(){}
  get value(){ if (this.tagName !== 'select') return this._v;
    const s = this.children.find(o => o.selected); return s ? s.value : ''; }
  set value(v){ if (this.tagName !== 'select') { this._v = v; return; }
    let hit = false; this.children.forEach(o => { o.selected = !hit && o.value === v; if (o.selected) hit = true; }); }
}
globalThis.document = { createElement: t => new El(t) };
"""


def _funktion(t: str, name: str) -> str:
    a = t.index(f"function {name}(")
    tiefe, i = 0, t.index("{", a)
    for j in range(i, len(t)):
        tiefe += {"{": 1, "}": -1}.get(t[j], 0)
        if tiefe == 0:
            return t[a:j + 1]
    raise AssertionError(name)


def _lauf(js: str):
    t = SEITE.read_text(encoding="utf-8")
    standard = re.search(r"^var STANDARD_WAHL = .*;$", t, re.M)
    assert standard, "STANDARD_WAHL nicht gefunden"
    vorspann = (_DOM + standard.group(0) + "\n"
                + "var _templatesByKind = {signatur: ['default', 'kurz'], oof: ['urlaub']};\n"
                + _funktion(t, "_fuelleVorlagenSelect") + "\n" + _funktion(t, "_istFest") + "\n"
                + _funktion(t, "_festQuelle") + "\n" + _funktion(t, "_wertText") + "\n"
                + _funktion(t, "_festSperren") + "\n" + _funktion(t, "_buildWahlSelect") + "\n"
                + _funktion(t, "_addinWahlZelle"))
    out = subprocess.run(["node", "-e", vorspann + "\n" + js],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


_ZEILE = """
const sel = _buildWahlSelect(0, 'x', KIND, MB, SLOT, FELD, LEER);
console.log(JSON.stringify({wert: sel.value, standard: STANDARD_WAHL, gesperrt: sel.disabled,
  titel: sel.title || '', optionen: sel.children.map(o => [o.value, o.textContent])}));
"""


def _zeile(kind, mb, slot, feld, leer):
    js = (_ZEILE.replace("KIND", json.dumps(kind)).replace("MB", json.dumps(mb))
          .replace("SLOT", json.dumps(slot)).replace("FELD", json.dumps(feld))
          .replace("LEER", json.dumps(leer)))
    return _lauf(js)


@pytest.mark.parametrize("kind,slot,feld,leer", [
    ("signatur", "sig", "template", None),           # Standardsignatur, ohne „keine“
    ("signatur", "min", "min_template", "— keine —"),
    ("oof", "oof", "oof_template", "— keine —"),
])
def test_standardwahl_erscheint_genau_einmal(kind, slot, feld, leer):
    r = _zeile(kind, {feld: None, "standard": {slot: "default"}}, slot, feld, leer)
    werte = [w for w, _ in r["optionen"]]
    assert werte.count(r["standard"]) == 1, r["optionen"]
    assert not any("andere Art" in text for _, text in r["optionen"]), r["optionen"]
    assert r["wert"] == r["standard"]
    assert r["optionen"][0] == [r["standard"], "Standard (default)"]


def test_sicherheitsnetz_zeigt_fremden_wert_weiter():
    """Ein zugewiesener Wert, der nicht in der Liste der Art steht, bleibt als
    „(andere Art)“ sichtbar und gewählt — sonst löschte Speichern die Zuweisung."""
    r = _zeile("signatur", {"template": "alt", "standard": {"sig": "default"}},
               "sig", "template", None)
    assert ["alt", "alt (andere Art)"] in r["optionen"], r["optionen"]
    assert r["wert"] == "alt"
    assert [w for w, _ in r["optionen"]].count(r["standard"]) == 1


def test_eigene_wahl_keine_bleibt_gewaehlt():
    r = _zeile("oof", {"oof_template": "", "standard": {"oof": "urlaub"}},
               "oof", "oof_template", "— keine —")
    assert r["wert"] == ""
    assert [w for w, _ in r["optionen"]].count(r["standard"]) == 1


def test_festgeschriebene_wahl_zeigt_den_gruppenwert_gesperrt():
    """Von einer Gruppen-Regel festgeschrieben (mb.fest): Auswahl gesperrt, sie
    zeigt den Gruppenwert — auch wenn eine eigene Wahl im Eintrag ruht."""
    r = _zeile("signatur", {"template": "kurz", "standard": {"sig": "Marke"},
                            "fest": {"sig": "Firma"}}, "sig", "template", None)
    assert r["gesperrt"] is True
    assert r["wert"] == r["standard"]
    assert r["optionen"][0] == [r["standard"], "Festgelegt (Marke)"]
    assert "Firma" in r["titel"]
    # Ohne Festschreibung bleibt die eigene Wahl gewählt und bedienbar.
    r = _zeile("signatur", {"template": "kurz", "standard": {"sig": "Marke"}, "fest": {}},
               "sig", "template", None)
    assert r["gesperrt"] is False and r["wert"] == "kurz"


def test_festgelegt_durch_standardrichtlinie_nennt_sie():
    """Quelle "" = Standardrichtlinie (policies.QUELLE_STANDARD): gesperrt wie
    bei einer Gruppe, der Titel nennt die Standardrichtlinie — nicht „Gruppe „““."""
    r = _zeile("signatur", {"template": "kurz", "standard": {"sig": "Firma"},
                            "fest": {"sig": ""}}, "sig", "template", None)
    assert r["gesperrt"] is True and r["wert"] == r["standard"]
    assert r["titel"] == "Festgelegt durch Standardrichtlinie"


_ADDIN = """
function _addinAuswahl(wert, o) { const d = document.createElement('details'); d.wert = wert; return d; }
function _wahlInZelle(td, sel, mb, slot, feld, nach) { td.appendChild(sel); if (nach) td.appendChild(nach); }
const td = _addinWahlZelle(0, MB);
const sel = td.children[0], liste = td.children[1];
console.log(JSON.stringify({wert: sel.value, gesperrt: sel.disabled, titel: sel.title || '',
  optionen: sel.children.map(o => [o.value, o.textContent]), liste: liste.wert,
  versteckt: !!liste.hidden}));
"""


@pytest.mark.parametrize("mb,soll", [
    # Ohne eigene Wahl: Standard, Liste versteckt (die Liste zeigt dann "*").
    ({"addin_templates": None, "standard": {"addin": ["A"]}},
     {"wert": "__standard__", "text": "Standard (A)", "versteckt": True, "gesperrt": False}),
    # Eigene Wahl [] (nur Standardsignatur) ist eine Wahl, nicht „Standard“.
    ({"addin_templates": [], "standard": {"addin": "*"}},
     {"wert": "eigen", "text": "Standard (alle Vorlagen)", "versteckt": False, "gesperrt": False}),
    # Festgelegt: zeigt den festgelegten Wert, gesperrt, auch bei ruhender Wahl.
    ({"addin_templates": ["X"], "standard": {"addin": ["A", "B"]}, "fest": {"addin": "Marke"}},
     {"wert": "__standard__", "text": "Festgelegt (A, B)", "versteckt": True, "gesperrt": True}),
])
def test_addin_zelle_wie_jede_vorlage(mb, soll):
    r = _lauf(_ADDIN.replace("MB", json.dumps(mb)))
    assert r["wert"] == soll["wert"]
    assert r["optionen"][0][1] == soll["text"]
    assert r["versteckt"] is soll["versteckt"]
    assert r["gesperrt"] is soll["gesperrt"]
    if mb["addin_templates"] is not None:
        assert r["liste"] == mb["addin_templates"], "die eigene Liste muss in der Auswahl stehen"
