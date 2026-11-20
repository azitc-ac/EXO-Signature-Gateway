"""Postfachliste: Ausnahmen sichtbar, filterbar, zurücksetzbar — und ein Haken,
der sagt, was er tut (mailboxes.html, v1.12.91).

Ausgangslage: „Standard (X)“ (das Postfach folgt Gruppe/global) und eine
bewusst gewählte Vorlage X sahen in der Zeile fast gleich aus, und der Haken
„Vorlagen-Richtlinien“ klang nach allen Vorlagen, betraf seit v1.9.113 aber nur
Banner, Disclaimer und Add-in-Auswahl. Im Self-Service stand die Vorgabe
(„Standardmäßig können Nutzer … nicht“) statt dessen, was gerade gilt.

Die Skripte laufen in Node gegen ein kleines nachgebautes DOM (Vorbild
test_wahl_select.py); `sel.value = x` wählt wie im Browser die ERSTE passende
Option, `change` erreicht die angemeldeten Hörer.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WURZEL = Path(__file__).parent.parent
SEITE = WURZEL / "app/webui/templates/mailboxes.html"
VORLAGEN = WURZEL / "app/webui/templates"

_DOM = r"""
class El {
  constructor(tag){ this.tagName = tag; this.children = []; this.dataset = {}; this.style = {};
    this.textContent = ''; this._v = ''; this.selected = false; this.className = '';
    this.disabled = false; this.checked = false; this.title = ''; this._h = {}; this.parent = null; }
  set innerHTML(h){ this.children = []; }
  appendChild(c){ c.parent = this; this.children.push(c); return c; }
  insertBefore(c, ref){ c.parent = this; const i = this.children.indexOf(ref);
    this.children.splice(i < 0 ? this.children.length : i, 0, c); return c; }
  get firstChild(){ return this.children[0] || null; }
  get options(){ return this.children; }
  get value(){ if (this.tagName !== 'select') return this._v;
    const s = this.children.find(o => o.selected); return s ? s.value : ''; }
  set value(v){ if (this.tagName !== 'select') { this._v = v; return; }
    let hit = false; this.children.forEach(o => { o.selected = !hit && o.value === v; if (o.selected) hit = true; }); }
  addEventListener(t, f){ (this._h[t] = this._h[t] || []).push(f); }
  dispatchEvent(e){ (this._h[e.type] || []).forEach(f => f.call(this, e)); return true; }
  _alle(){ return this.children.flatMap(c => [c].concat(c._alle())); }
  querySelectorAll(s){ return this._alle().filter(e => passt(e, s)); }
  querySelector(s){ return this.querySelectorAll(s)[0] || null; }
}
// Nur die Selektorformen, die die geprüften Funktionen benutzen.
function passt(e, s){
  return s.split(',').some(t => {
    t = t.trim();
    let m = t.match(/^select\[data-ausnahme="1"\]$/);
    if (m) return e.tagName === 'select' && e.dataset.ausnahme === '1';
    m = t.match(/^(\w*)\.([\w-]+)(:checked)?$/);
    if (m) return (!m[1] || e.tagName === m[1]) && e.className.split(' ').includes(m[2])
                  && (!m[3] || e.checked);
    throw new Error('Selektor nicht nachgespielt: ' + t);
  });
}
globalThis.Event = class { constructor(t){ this.type = t; } };
const _ids = {};
const tbody = new El('tbody');
globalThis.document = {
  createElement: t => new El(t),
  createTextNode: t => { const e = new El('#text'); e.textContent = t; return e; },
  getElementById: id => _ids[id] || null,
  querySelectorAll: s => {
    if (s === '#mb-tbody tr') return tbody.children;
    return Object.values(_ids).flatMap(e => [e].concat(e._alle())).filter(e => passt(e, s));
  },
};
let meldung = null;
globalThis.showMsg = (el, text, ok) => { meldung = {el, text, ok}; };
"""


def _funktion(t: str, name: str) -> str:
    a = t.index(f"function {name}(")
    tiefe, i = 0, t.index("{", a)
    for j in range(i, len(t)):
        tiefe += {"{": 1, "}": -1}.get(t[j], 0)
        if tiefe == 0:
            return t[a:j + 1]
    raise AssertionError(name)


def _var(t: str, name: str) -> str:
    m = re.search(rf"^var {name} = .*;$", t, re.M)
    assert m, f"{name} nicht gefunden"
    return m.group(0)


FUNKTIONEN = ["_fuelleVorlagenSelect", "_istFest", "_festQuelle", "_festGruppen", "gruppenFarbe",
              "_gruppenChip", "_wertText", "_festSperren",
              "_buildWahlSelect", "_festHinweis", "massenRuhendVerwerfen",
              "_wahlInZelle", "_istAusnahme", "_ausnahmeStand", "filterMailboxes",
              "massenAufStandard", "_angehakt", "_selfTplStandText", "selfTplStandZeigen"]

# Zeile mit Signatur- und Antwort-Wahl, gebaut über die echten Bausteine.
_ZEILE = r"""
function zeile(mb, versteckt) {
  const tr = new El('tr');
  tr.dataset.search = (mb.email || '').toLowerCase(); tr.dataset.mbtype = 'user';
  if (versteckt) tr.style.display = 'none';
  const td1 = new El('td'), td2 = new El('td');
  _wahlInZelle(td1, _buildWahlSelect(0, 'mb-template', 'signatur', mb, 'sig', 'template', null), mb, 'sig', 'template');
  _wahlInZelle(td2, _buildWahlSelect(0, 'mb-min-sig', 'signatur', mb, 'min', 'min_template', '— keine —'), mb, 'min', 'min_template');
  tr.appendChild(td1); tr.appendChild(td2); tbody.appendChild(tr);
  return tr;
}
const STD = {standard: {sig: 'default', min: 'kurz'}};
function mb(x){ return Object.assign({}, STD, x); }
function aus(tr){ return tr.querySelectorAll('select.mb-template, select.mb-min-sig').map(s => s.dataset.ausnahme || ''); }
"""


def _lauf(js: str):
    if shutil.which("node") is None:
        pytest.skip("node fehlt")
    t = SEITE.read_text(encoding="utf-8")
    vorspann = (_DOM + _var(t, "STANDARD_WAHL") + "\n" + _var(t, "WAHL_SELEKTOR") + "\n"
                + _var(t, "GRUPPEN_FARBEN") + "\n"
                + (SEITE.parents[1] / "static" / "vorlagen_name.js").read_text(encoding="utf-8") + "\n"
                + "var _templatesByKind = {signatur: ['default', 'kurz', 'lang'], oof: ['urlaub']};\n"
                + "\n".join(_funktion(t, n) for n in FUNKTIONEN) + "\n" + _ZEILE)
    out = subprocess.run(["node", "-e", vorspann + "\n" + js],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


# ── 2. Ausnahme-Erkennung ────────────────────────────────────────────────────

def test_ausnahme_standard_vorlage_keine_fest():
    r = _lauf(r"""
      const a = zeile(mb({email: 'a'}));                                   // beides Standard
      const b = zeile(mb({email: 'b', template: 'lang'}));                 // bewusste Vorlage
      const c = zeile(mb({email: 'c', min_template: ''}));                 // bewusst „keine“
      const d = zeile(mb({email: 'd', template: 'lang', fest: {sig: 'Firma'}}));  // fest, Wahl ruht
      const s = b.querySelector('select.mb-template');
      const marke = s.parent.children[s.parent.children.indexOf(s) + 1];
      // Absicherung: auch wenn eine festgeschriebene Auswahl (aus welchem Grund
      // auch immer) einen anderen Wert trägt, ist sie keine Ausnahme.
      const df = d.querySelector('select.mb-template'); df.value = 'lang';
      console.log(JSON.stringify({a: aus(a), b: aus(b), c: aus(c), d: aus(d),
        festAnders: _istAusnahme(df), marke: [marke.className, marke.textContent]}));
    """)
    assert r["a"] == ["", ""]
    assert r["b"] == ["1", ""]
    assert r["c"] == ["", "1"]
    assert r["d"] == ["", ""], "festgeschrieben ist keine Ausnahme"
    assert r["festAnders"] is False
    # Der CSS-Selektor `select[data-ausnahme] + .wahl-ausnahme` braucht die Marke DIREKT dahinter.
    assert r["marke"] == ["wahl-ausnahme", "Ausnahme"]


def test_ausnahme_folgt_dem_umstellen():
    r = _lauf(r"""
      const a = zeile(mb({email: 'a'}));
      const s = a.querySelector('select.mb-template');
      const v = [s.dataset.ausnahme || ''];
      s.value = 'kurz'; s.dispatchEvent(new Event('change'));
      v.push(s.dataset.ausnahme || '');
      s.value = STANDARD_WAHL; s.dispatchEvent(new Event('change'));
      v.push(s.dataset.ausnahme || '');
      console.log(JSON.stringify(v));
    """)
    assert r == ["", "1", ""]


def test_ausnahme_farbe_hell_und_dunkel_nur_im_css():
    t = SEITE.read_text(encoding="utf-8")
    stil = t[t.index("<style>"):t.index("</style>")]
    hell = re.search(r'^\s*select\[data-ausnahme="1"\] \+ \.wahl-ausnahme \{[^}]*background:#fef3c7', stil, re.M)
    dunkel = re.search(r'\[data-theme="dark"\] select\[data-ausnahme="1"\] \+ \.wahl-ausnahme \{[^}]*background:#1a1200', stil)
    assert hell and dunkel, "Ausnahme-Marke braucht eine helle UND eine dunkle Regel"
    assert re.search(r"\.wahl-ausnahme \{ display:none", stil), "ohne Ausnahme bleibt die Marke unsichtbar"
    # Das Skript setzt nur den Zustand, keine Farbe.
    js = _funktion(t, "_ausnahmeStand") + _funktion(t, "_wahlInZelle")
    assert ".style." not in js


# ── 3. Filter „nur Ausnahmen“ ────────────────────────────────────────────────

def test_filter_zeigt_nur_ausnahmen():
    r = _lauf(r"""
      const cb = new El('input'); _ids['mb-ausnahme-filter'] = cb;
      _ids['mb-filter-count'] = new El('span');
      const a = zeile(mb({email: 'a'})), b = zeile(mb({email: 'b', template: 'lang'}));
      const c = zeile(mb({email: 'c', template: 'lang', fest: {sig: 'G'}}));
      const v = {};
      filterMailboxes(); v.ohne = [a, b, c].map(tr => tr.style.display);
      cb.checked = true; filterMailboxes(); v.mit = [a, b, c].map(tr => tr.style.display);
      v.zaehler = _ids['mb-filter-count'].textContent;
      console.log(JSON.stringify(v));
    """)
    assert r["ohne"] == ["", "", ""]
    assert r["mit"] == ["none", "", "none"]
    assert r["zaehler"] == "1 von 3 Postfächern"


def test_filterkaestchen_steht_in_der_filterzeile():
    t = SEITE.read_text(encoding="utf-8")
    zeile = t[t.index('id="mb-filter-feld"'):t.index('id="mb-aktionen-feld"')]
    assert 'id="mb-ausnahme-filter" onchange="filterMailboxes()"' in zeile


# ── 4. Massenaktion „Sichtbare auf Standard“ ─────────────────────────────────

def test_massenaktion_setzt_standard_und_ueberspringt_fest():
    r = _lauf(r"""
      _ids['mb-ausnahme-filter'] = new El('input');
      const a = zeile(mb({email: 'a', template: 'lang', min_template: ''}));
      const f = zeile(mb({email: 'f', template: 'lang', min_template: 'lang', fest: {sig: 'G'}}));
      const h = zeile(mb({email: 'h', template: 'lang'}), true);            // ausgefiltert
      massenAufStandard();
      const w = tr => tr.querySelectorAll(WAHL_SELEKTOR).map(s => s.value);
      console.log(JSON.stringify({a: w(a), aAus: aus(a), f: w(f), fFest: f.querySelector('select.mb-template').dataset.fest,
        h: w(h), meldung}));
    """)
    std = "__standard__"
    assert r["a"] == [std, std] and r["aAus"] == ["", ""], "Marke muss mitgehen (change)"
    # fest bleibt (zeigt den Gruppenwert über STANDARD_WAHL, gesperrt); die Antwort-Wahl wird zurückgesetzt.
    assert r["fFest"] == "1" and r["f"] == [std, std]
    assert r["h"] == ["lang", std], "ausgeblendete Zeilen bleiben unberührt"
    m = r["meldung"]
    assert m["el"] == "mb-massen-status" and m["ok"] is True
    assert m["text"].startswith("3 Vorlage(n) in 2 Zeile(n) auf Standard gesetzt, 1 festgelegte übersprungen"), m


def test_massenaktion_ohne_ausnahme_meldet_das():
    r = _lauf(r"""
      zeile(mb({email: 'a'})); massenAufStandard(); console.log(JSON.stringify(meldung));
    """)
    assert r["ok"] is False and r["text"].startswith("Keine Ausnahme")


def test_massenaktion_hat_einen_knopf():
    t = SEITE.read_text(encoding="utf-8")
    assert re.search(r'<button class="btn secondary btn-sm" onclick="massenAufStandard\(\)">', t)
    # Dieselben Spalten wie die Sammelauswahl und das Speichern.
    sel = re.search(r"^var WAHL_SELEKTOR = '(.*)';$", t, re.M).group(1)
    # Seit v1.12.99 jede Vorlage — auch Banner, Disclaimer und die Add-in-Auswahl.
    assert {s.strip() for s in sel.split(",")} == {
        "select.mb-template", "select.mb-min-sig", "select.mb-intern", "select.mb-banner",
        "select.mb-disclaimer", "select.mb-oof", "select.mb-addin-modus"}
    for klasse in ("mb-banner", "mb-disclaimer", "mb-addin-modus"):
        assert f"'{klasse}'" in t, f"keine Auswahl mit Klasse {klasse} in der Zeile"


# ── 1. Haken „Banner/Disclaimer nach Richtlinie“ entfällt (v1.12.99) ─────────

def test_haken_nach_richtlinie_entfaellt():
    """Kein Unterbrechen der Vererbung mehr: Spalte, Kästchen und Feld sind weg —
    eine Seite, die `use_policy` schickte, ließe den Server Banner/Disclaimer/Add-in
    als nicht geschickt behandeln (mailboxes.api_save_mailboxes)."""
    t = SEITE.read_text(encoding="utf-8")
    assert "use_policy" not in t
    assert "mb-policy" not in t and "policyCb" not in t
    assert "Banner/Disclaimer nach Richtlinie" not in t
    alt = [p.name for p in VORLAGEN.glob("*.html")
           if "Vorlagen-Richtlinien" in p.read_text(encoding="utf-8")]
    assert not alt, f"alte Bezeichnung noch in {alt}"


# ── 6. Ruhende Wahl: sichtbar, filterbar, verwerfbar ─────────────────────────

def test_ruhende_wahl_zeigt_verwerfen_und_filter():
    r = _lauf(r"""
      const cb = new El('input'); _ids['mb-ruhend-filter'] = cb;
      _ids['mb-filter-count'] = new El('span');
      const a = zeile(mb({email: 'a', template: 'lang'}));                       // Wahl, nicht fest
      const b = zeile(mb({email: 'b', template: 'lang', fest: {sig: 'G'}}));      // ruht
      const c = zeile(mb({email: 'c', fest: {sig: ''}}));                         // fest, keine Wahl
      const v = {weg: [a, b, c].map(tr => tr.querySelectorAll('.mb-ruht-weg').length)};
      // Unter der Auswahl nur die Quelle als Chip; der Satz steht im title.
      const quelle = h => h ? {titel: h.title,
        chips: h.querySelectorAll('span.gruppen-chip').map(c => [c.textContent, c.dataset.gruppenfarbe]),
        ruht: (h.querySelector('span.wahl-ruht') || {}).textContent || null} : null;
      v.b = quelle(b.querySelector('span.wahl-fest'));
      v.c = quelle(c.querySelector('span.wahl-fest'));
      v.farbeG = String(gruppenFarbe('G'));
      cb.checked = true; filterMailboxes(); v.mit = [a, b, c].map(tr => tr.style.display);
      massenRuhendVerwerfen();
      v.angehakt = b.querySelectorAll('.mb-ruht-weg:checked').map(x => x.dataset.feld);
      v.meldung = meldung;
      console.log(JSON.stringify(v));
    """)
    assert r["weg"] == [0, 1, 0]
    assert r["b"] == {"titel": "Festgelegt durch Gruppe „G“", "chips": [["G", r["farbeG"]]],
                      "ruht": "eigene Wahl „lang“ ruht"}
    assert r["c"] == {"titel": "Festgelegt durch Standardrichtlinie",
                      "chips": [["Standardrichtlinie", "standard"]], "ruht": None}
    assert r["mit"] == ["none", "", "none"]
    assert r["angehakt"] == ["template"]
    assert r["meldung"]["ok"] is True and r["meldung"]["text"].startswith("1 ruhende Wahl(en)")


def test_ruhend_filter_und_knopf_stehen_auf_der_seite():
    t = SEITE.read_text(encoding="utf-8")
    zeile = t[t.index('id="mb-filter-feld"'):t.index('id="mb-aktionen-feld"')]
    assert 'id="mb-ruhend-filter" onchange="filterMailboxes()"' in zeile
    assert 'id="ruhend"' in zeile, "Sprungziel der Aufgabe richtlinien-ruhend"
    assert re.search(r'onclick="massenRuhendVerwerfen\(\)"', t)
    # Speichern schickt die angehakten Felder mit.
    assert "ruhend_verwerfen: verwerfen," in _funktion(t, "saveMailboxes")


# ── 5. Self-Service: Kästchen je Gruppe, Zustandszeile ───────────────────────

@pytest.mark.parametrize("gruppen,kats,einzeln,an,soll", [
    ([], ["Signatur"], 0, True, "Derzeit darf niemand Vorlagen wählen."),
    (["Vertrieb", "Technik"], ["Signatur", "Antwort-Signatur"], 0, True,
     "Derzeit dürfen wählen: Vertrieb, Technik — Kategorien: Signatur, Antwort-Signatur."),
    (["Vertrieb"], [], 0, True,
     "Derzeit darf niemand Vorlagen wählen — freigeschaltet ist Vertrieb, aber keine Kategorie ist wählbar."),
    ([], ["Signatur"], 2, True,
     "Derzeit dürfen wählen: 2 einzeln freigeschaltete Postfächer — Kategorien: Signatur."),
    (["Vertrieb"], ["Signatur"], 0, False,
     "Derzeit dürfen wählen: Vertrieb — Kategorien: Signatur. Der Self-Service selbst ist ausgeschaltet (Schalter oben)."),
])
def test_zustandszeile_text(gruppen, kats, einzeln, an, soll):
    r = _lauf("console.log(JSON.stringify(_selfTplStandText(%s, %s, %d, %s)));"
              % (json.dumps(gruppen), json.dumps(kats), einzeln, json.dumps(an)))
    assert r == soll


def test_zustandszeile_liest_die_kaestchen():
    r = _lauf(r"""
      const box = new El('div'); _ids['box'] = box;
      const stand = new El('p'); stand.dataset.einzeln = '0'; _ids['self-tpl-stand'] = stand;
      const an = new El('input'); an.checked = true; _ids['self-service'] = an;
      function kb(kl, wert, text, ja){ const c = new El('input'); c.className = kl; c.value = wert;
        if (text) c.dataset.text = text; c.checked = ja; box.appendChild(c); return c; }
      const g1 = kb('self-tpl-gruppe', 'Vertrieb', null, false); kb('self-tpl-gruppe', 'Technik', null, true);
      kb('self-tpl-kat', 'sig', 'Signatur', true); kb('self-tpl-kat', 'oof', 'Abwesenheitsnotiz', false);
      selfTplStandZeigen(); const v = [stand.textContent];
      g1.checked = true; selfTplStandZeigen(); v.push(stand.textContent);
      console.log(JSON.stringify(v));
    """)
    assert r == ["Derzeit dürfen wählen: Technik — Kategorien: Signatur.",
                 "Derzeit dürfen wählen: Vertrieb, Technik — Kategorien: Signatur."]


def test_self_service_gruppen_sind_kaestchen_mit_zustandszeile():
    t = SEITE.read_text(encoding="utf-8")
    assert 'id="self-tpl-groups"' not in t and "multiple size=" not in t
    assert re.search(r'<label class="checkbox-label">\s*<input type="checkbox" class="self-tpl-gruppe" '
                     r'value="\{\{ g \}\}" onchange="saveSelfTplGroups\(\)"', t)
    assert "Standardmäßig können Nutzer ihre Vorlagen <strong>nicht</strong>" not in t
    assert 'id="self-tpl-stand" data-einzeln="{{ self_template_einzeln }}"' in t
    # Jede Änderung, die den Stand bewegt, aktualisiert die Zeile.
    for f in ("saveSelfTplGroups", "saveSelfTplKategorien", "selfServiceUmschalten"):
        assert "selfTplStandZeigen();" in _funktion(t, f), f
    assert 'data-text="{{ text }}"' in t, "Kategorie-Kästchen brauchen ihren Anzeigenamen"
    assert "ebenso eine festgelegte Vorlage" in t
