"""Auswahl der Add-in-Vorlagen (mailboxes.html): EIN Baustein für Richtlinie und
Postfachzeile (_addinAuswahl), seit 2026-10-21 statt zweier Kopien.

Das Skript läuft in Node gegen ein kleines nachgebautes DOM — genau die Teile,
die der Baustein benutzt. Geprüft wird das Verhalten UND die Namen, über die der
übrige Code die Auswahl ausliest (#policy-addin-all, .policy-addin-tpl bzw.
.addin-all/.addin-tpl mit data-idx) — die dürfen sich nicht verschieben.
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
  constructor(tag){ this.tagName = tag.toUpperCase(); this.children = []; this.parent = null;
    this.className = ''; this.id = ''; this.dataset = {}; this.style = {cssText:''}; this.title = '';
    this._text = ''; this.type = ''; this.checked = false; this.disabled = false; this.value = ''; this._h = {}; }
  appendChild(c){ c.parent = this; this.children.push(c); return c; }
  addEventListener(t, f){ (this._h[t] = this._h[t] || []).push(f); }
  fire(t){ (this._h[t] || []).forEach(f => f({target:this})); }
  set textContent(v){ this._text = String(v); this.children = []; }
  get textContent(){ return this._text + this.children.map(c => c.textContent).join(''); }
  set innerHTML(h){ this.children = []; this._text = '';
    for (const m of h.matchAll(/<span[^>]*>([^<]*)<\/span>/g)) { const s = new El('span'); s._text = m[1]; this.appendChild(s); } }
  _alle(){ return this.children.flatMap(c => [c, ...c._alle()]); }
  _passt(e, sel){
    const [basis, pseudo] = sel.split(':');
    const ok = basis.startsWith('.') ? e.className.split(' ').includes(basis.slice(1))
             : basis.startsWith('#') ? e.id === basis.slice(1) : e.tagName === basis.toUpperCase();
    if (!ok) return false;
    if (pseudo === 'checked') return e.checked;
    if (pseudo === 'last-child') return e.parent && e.parent.children[e.parent.children.length - 1] === e;
    return true; }
  querySelectorAll(sel){ return this._alle().filter(e => this._passt(e, sel)); }
  querySelector(sel){ return this.querySelectorAll(sel)[0] || null; }
}
globalThis.document = { createElement: t => new El(t), createTextNode: t => { const e = new El('#text'); e._text = t; return e; } };
"""


def _funktionen() -> str:
    t = SEITE.read_text(encoding="utf-8")
    teile = []
    for name in ("_addinAuswahl", "_buildPolicyAddinControl", "_buildAddinTemplatesControl"):
        a = t.index(f"function {name}(")
        tiefe, i = 0, t.index("{", a)
        for j in range(i, len(t)):
            tiefe += {"{": 1, "}": -1}.get(t[j], 0)
            if tiefe == 0:
                break
        teile.append(t[a:j + 1])
    return "\n".join(teile)


def _lauf(js: str):
    vorspann = _DOM + "var _templateList = ['a','b','c']; var _addinEnabled = true;\n" + _funktionen()
    out = subprocess.run(["node", "-e", vorspann + "\n" + js], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


_ZUSTAND = """
function zustand(d, all, tpl){
  const m = d.querySelector(all);
  return {label: d.querySelector('summary').querySelector('span:last-child').textContent,
          alle: m.checked, an: d.querySelectorAll(tpl + ':checked').map(c => c.value),
          gesperrt: d.querySelectorAll(tpl).map(c => c.disabled)};
}
"""


@pytest.mark.parametrize("wert, label, an", [
    ("*", "Alle", ["a", "b", "c"]),
    (["b"], "1 gewählt", ["b"]),
    ([], "Nur Standard", []),
])
def test_anfangszustand(wert, label, an):
    r = _lauf(_ZUSTAND + f"const d = _buildPolicyAddinControl({json.dumps(wert)});"
              "console.log(JSON.stringify(zustand(d, '#policy-addin-all', '.policy-addin-tpl')));")
    assert r["label"] == label and r["an"] == an and r["alle"] == (wert == "*")
    assert r["gesperrt"] == [wert == "*"] * 3


def test_alle_umschalten_und_einzeln_waehlen():
    r = _lauf(_ZUSTAND + """
      const d = _buildAddinTemplatesControl(4, [], true, false);
      const m = d.querySelector('.addin-all'); const tpl = d.querySelectorAll('.addin-tpl');
      const schritte = [];
      m.checked = true; m.fire('change'); schritte.push(zustand(d, '.addin-all', '.addin-tpl'));
      m.checked = false; m.fire('change'); schritte.push(zustand(d, '.addin-all', '.addin-tpl'));
      tpl[2].checked = true; tpl[2].fire('change'); schritte.push(zustand(d, '.addin-all', '.addin-tpl'));
      console.log(JSON.stringify({schritte, idx: [d.dataset.addinIdx, m.dataset.idx, ...tpl.map(c => c.dataset.idx)]}));
    """)
    s = r["schritte"]
    assert s[0] == {"label": "Alle", "alle": True, "an": ["a", "b", "c"], "gesperrt": [True] * 3}
    assert s[1] == {"label": "Nur Standard", "alle": False, "an": [], "gesperrt": [False] * 3}
    assert s[2]["label"] == "1 gewählt" and s[2]["an"] == ["c"]
    assert r["idx"] == ["4"] * 5                     # die Zeilen-Speicherung liest über data-idx


def test_postfachzeile_gesperrt_wenn_richtlinie_greift():
    r = _lauf("const d = _buildAddinTemplatesControl(0, '*', true, true);"
              "console.log(JSON.stringify({o: d.style.opacity, t: d.title, pe: d.style.pointerEvents}));")
    assert r == {"o": "0.4", "t": "Wird durch Vorlagenrichtlinie gesteuert", "pe": "none"}


def test_namen_fuer_das_auslesen_bleiben():
    t = SEITE.read_text(encoding="utf-8")
    assert "getElementById('policy-addin-all')" in t and "'.policy-addin-tpl:checked'" in t
    assert "querySelector('.addin-all')" in t and "querySelectorAll('.addin-tpl')" in t
    assert len(re.findall(r"function _addinAuswahl\(", t)) == 1
