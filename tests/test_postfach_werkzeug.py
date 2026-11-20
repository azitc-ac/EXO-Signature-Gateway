"""Postfachliste: Werkzeugleiste und Gruppenfarben (mailboxes.html, v1.12.110).

1. Über der Liste standen Erklärtext, sechs Filter und sechs Sammelaktionen
   offen — auf dem Telefon lag die erste Postfachzeile unter dem Bildschirmrand.
   Jetzt: Suche + „Filter“ + „Aktionen“ in einer Leiste, die Bereiche klappen
   auf. Hier geprüft wird der Aufbau (was offen über der Liste steht); die
   gemessene Höhe prüft der Browser-Test in test_ui_browser.py.
2. Festgelegte Auswahl: unter dem Feld nur die Quelle als Chip in der
   Gruppenfarbe. Die Farbe kommt aus EINER Funktion (gruppenFarbe) und steht
   als data-gruppenfarbe am Element; die Farben selbst stehen nur im CSS, hell
   UND dunkel, nur aus der freigegebenen Palette.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SEITE = Path(__file__).parent.parent / "app/webui/templates/mailboxes.html"

# Freigegebene helle Werte (CLAUDE.md, „UI-Farben & Dark Mode“). CLAUDE.md liegt
# nicht im Repository — deshalb hier ausgeschrieben.
PALETTE_HG = {"#eff6ff", "#f0f9ff", "#e0f2fe", "#dbeafe", "#ecfdf5", "#dcfce7", "#f0fdf4",
              "#fef2f2", "#fffbeb", "#fef3c7", "#fef9c3", "#fefce8", "#fff7ed", "#ede9fe",
              "#f3e8ff", "#f5f3ff", "#fff", "#f8fafc", "#f1f5f9", "#f3f4f6", "#fafafa", "#fafaf9"}
PALETTE_TEXT = {"#0f172a", "#1e293b", "#1f2937", "#334155", "#374151", "#555", "#64748b",
                "#6b7280", "#94a3b8", "#9ca3af", "#888", "#065f46", "#166534", "#15803d",
                "#991b1b", "#dc2626", "#1e40af", "#0369a1", "#0284c7", "#0078d4", "#92400e",
                "#b45309", "#c2410c", "#d97706",
                "#7c3aed"}   # Text lila — in CLAUDE.md aufgenommen 19.11.2026 (Farbplatz 3)
PALETTE_RAHMEN = {"#e2e8f0", "#e5e7eb", "#d1d5db", "#eee", "#f0f0f0", "#bae6fd", "#bfdbfe",
                  "#a7f3d0", "#bbf7d0", "#d1fae5", "#fca5a5", "#fecaca", "#fcd34d", "#fde68a",
                  "#fed7aa", "#ede9fe"}   # Rahmen lila — wie oben


def _text() -> str:
    return SEITE.read_text(encoding="utf-8")


def _stil() -> str:
    t = _text()
    return re.sub(r"/\*.*?\*/", "", t[t.index("<style>"):t.index("</style>")], flags=re.S)


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


def _node(js: str):
    if shutil.which("node") is None:
        pytest.skip("node fehlt")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def _anzahl_farben() -> int:
    return int(re.search(r"^var GRUPPEN_FARBEN = (\d+);$", _text(), re.M).group(1))


# ── Gruppenfarbe: eine Funktion, stabil, gestreut ───────────────────────────

def test_gruppenfarbe_stabil_im_bereich_und_gestreut():
    t = _text()
    namen = [f"Gruppe {i}" for i in range(40)] + ["Vertrieb", "Technik", "Alle Postfächer", "", "Ä, Ö"]
    r = _node(_var(t, "GRUPPEN_FARBEN") + "\n" + _funktion(t, "gruppenFarbe") + "\n"
              + f"const n = {json.dumps(namen)};"
              + "console.log(JSON.stringify({a: n.map(gruppenFarbe), b: n.map(gruppenFarbe)}));")
    n = _anzahl_farben()
    assert r["a"] == r["b"], "gleicher Name muss dieselbe Farbe ergeben"
    assert all(isinstance(x, int) and 0 <= x < n for x in r["a"]), r["a"]
    # 40 Namen auf 8 Farben: fast jede Farbe muss vorkommen — eine Konstante oder
    # ein Hash, der nur die Länge wertet, fällt hier durch.
    assert len(set(r["a"][:40])) >= n - 1, sorted(set(r["a"][:40]))
    # Erwarteter Wert ausgeschrieben: ändert jemand die Formel, wechseln alle
    # Gruppen still ihre Farbe — das soll eine bewusste Entscheidung sein.
    assert r["a"][40] == sum(ord(c) * 31 ** (len("Vertrieb") - 1 - i)
                             for i, c in enumerate("Vertrieb")) % 2 ** 32 % n


def test_jede_farbe_hat_helle_und_dunkle_regel_aus_der_palette():
    stil = _stil()
    n = _anzahl_farben()
    for wert in [str(i) for i in range(n)] + ["standard"]:
        sel = f'.gruppen-chip[data-gruppenfarbe="{wert}"]'
        hell = re.search(r"^\s*" + re.escape(sel) + r" \{([^}]*)\}", stil, re.M)
        dunkel = re.search(r'\[data-theme="dark"\] ' + re.escape(sel) + r" \{([^}]*)\}", stil)
        assert hell, f"keine helle Regel für {wert}"
        assert dunkel, f"keine dunkle Regel für {wert}"
        decl = dict(re.findall(r"(background|color|border-color):(#[0-9a-f]{3,6})", hell.group(1)))
        assert decl.get("background") in PALETTE_HG, (wert, decl)
        assert decl.get("color") in PALETTE_TEXT, (wert, decl)
        assert decl.get("border-color") in PALETTE_RAHMEN, (wert, decl)
        assert "background:" in dunkel.group(1) and "color:" in dunkel.group(1)
    # Mehr Regeln als Farben wären totes CSS — und ein Zeichen, dass die Zahl driftet.
    assert f'data-gruppenfarbe="{n}"' not in stil


def test_farbe_nur_ueber_gruppenfarbe_und_kein_skript_faerbt():
    t = _text()
    js = t[t.index("</style>"):]
    # Jede Zuweisung des Attributs geht durch gruppenFarbe() — oder ist „standard“.
    zuweisungen = re.findall(r"dataset\.gruppenfarbe\s*=\s*([^;]+);", js)
    html = re.findall(r"data-gruppenfarbe=\"' \+ ([^+]+) \+", js)
    assert zuweisungen and html, "Prüfer veraltet: keine Zuweisung gefunden"
    for z in zuweisungen + html:
        assert "gruppenFarbe(" in z, f"Gruppenfarbe am Baustein vorbei: {z}"
    for name in ("gruppenFarbe", "_gruppenChip", "_gruppenPunktHtml", "_festHinweis"):
        assert not re.search(r"\.style\.(color|background|borderColor)|(color|background)\s*:",
                             _funktion(t, name)), f"{name} setzt eine Farbe per Skript"
    assert t.count("function gruppenFarbe(") == 1


# ── Werkzeugleiste: was offen über der Liste steht ──────────────────────────

def _kopf() -> str:
    """Von der Postfach-Karte bis zur Tabelle."""
    t = _text()
    a = t.index("<!-- Postfach-Konfiguration -->")
    return t[a:t.index('class="tabellen-rollbereich"', a)]


def _ohne_klappbereiche(kopf: str) -> str:
    """Kopf ohne die eingeklappten Bereiche (Filter, Aktionen, Erklärung)."""
    kopf = re.sub(r"<details.*?</details>", "", kopf, flags=re.S)
    for feld in ("mb-filter-feld", "mb-aktionen-feld"):
        a = kopf.index(f'<div id="{feld}"')
        tiefe, i = 0, a
        while True:
            auf, zu = kopf.find("<div", i), kopf.find("</div>", i)
            if auf != -1 and auf < zu:
                tiefe, i = tiefe + 1, auf + 4
            else:
                tiefe, i = tiefe - 1, zu + 6
                if tiefe == 0:
                    break
        kopf = kopf[:a] + kopf[i:]
    return kopf


def test_ueber_der_liste_nur_die_leiste():
    """Offen über der Tabelle: Laden/Status, Suche, „Filter“, „Aktionen“.
    Jedes weitere Bedienelement kostet auf dem Telefon eine Zeile."""
    offen = _ohne_klappbereiche(_kopf())
    knoepfe = re.findall(r'<button[^>]*\bid="([\w-]+)"', offen)
    assert knoepfe == ["mb-load-btn", "mb-refresh-btn", "mb-filter-knopf", "mb-aktionen-knopf"], knoepfe
    assert not re.search(r"<button(?![^>]*\bid=)", offen), "Knopf ohne id offen über der Liste"
    assert "<select" not in offen, "Auswahlfeld offen über der Liste"
    assert 'type="checkbox"' not in offen, "Kästchen offen über der Liste"
    # Der Erklärtext ist eingeklappt, nicht offen.
    assert "Nur markierte Postfächer werden verarbeitet" not in offen


def test_klappbereiche_starten_zu_und_tragen_alle_funktionen():
    kopf = _kopf()
    for feld in ("mb-filter-feld", "mb-aktionen-feld"):
        assert re.search(rf'<div id="{feld}" class="[^"]*mb-klappfeld[^"]*" hidden>', kopf), feld
    filt = kopf[kopf.index('id="mb-filter-feld"'):kopf.index('id="mb-aktionen-feld"')]
    for fid in ("mb-type-filter", "mb-sig-filter", "mb-smime-filter", "mb-ausnahme-filter",
                "mb-ruhend-filter", 'id="ruhend"'):
        assert fid in filt, f"{fid} fehlt im Filterbereich"
    akt = kopf[kopf.index('id="mb-aktionen-feld"'):]
    for f in ("setAllVisible('sig', true)", "setAllVisible('sig', false)",
              "setAllVisible('smime', true)", "setAllVisible('smime', false)",
              "massenVorlageSetzen()", "massenAufStandard()", "massenRuhendVerwerfen()",
              "regelPruefenSichtbare()", 'id="mb-massen-spalte"', 'id="mb-massen-vorlage"',
              'id="mb-massen-status"', 'id="regel-probe-alle-status"'):
        assert f in akt, f"{f} fehlt im Aktionsbereich"


_DOM = r"""
class El { constructor(){ this.hidden = true; this.attr = {}; this.textContent = ''; }
  setAttribute(k, v){ this.attr[k] = v; } }
const _ids = {'mb-filter-feld': new El(), 'mb-filter-knopf': new El(),
              'mb-aktionen-feld': new El(), 'mb-aktionen-knopf': new El()};
globalThis.document = { getElementById: id => _ids[id] || null };
function stand(){ return ['mb-filter-feld', 'mb-aktionen-feld'].map(f => _ids[f].hidden)
  .concat(['mb-filter-knopf', 'mb-aktionen-knopf'].map(k => _ids[k].attr['aria-expanded'])); }
"""


def test_klappe_oeffnet_einen_und_schliesst_den_anderen():
    t = _text()
    r = _node(_DOM + _var(t, "MB_KLAPPEN") + "\n" + _funktion(t, "mbKlappe") + r"""
      const v = [];
      mbKlappe('mb-filter-feld'); v.push(stand());
      mbKlappe('mb-aktionen-feld'); v.push(stand());
      mbKlappe('mb-aktionen-feld'); v.push(stand());
      mbKlappe('mb-filter-feld', true); mbKlappe('mb-filter-feld', true); v.push(stand());
      console.log(JSON.stringify(v));
    """)
    assert r == [[False, True, "true", "false"],
                 [True, False, "false", "true"],
                 [True, True, "false", "false"],
                 [False, True, "true", "false"]]


def test_sprung_auf_ruhend_oeffnet_den_filter():
    t = _text()
    i = t.index("if (location.hash === '#ruhend')")
    assert "mbKlappe('mb-filter-feld', true)" in t[i:t.index("}", i)]


def test_filterknopf_zaehlt_die_eingeklappten_filter():
    t = _text()
    r = _node(r"""
      const _ids = {};
      for (const id of ['mb-filter', 'mb-type-filter', 'mb-sig-filter', 'mb-smime-filter'])
        _ids[id] = {value: ''};
      for (const id of ['mb-ausnahme-filter', 'mb-ruhend-filter']) _ids[id] = {checked: false};
      _ids['mb-filter-aktiv'] = {textContent: 'x'}; _ids['mb-filter-count'] = {textContent: ''};
      globalThis.document = { getElementById: id => _ids[id] || null, querySelectorAll: () => [] };
    """ + _var(t, "MB_FILTER_SCHLUESSEL") + "\n" + _var(t, "MB_FILTER_FELDER") + "\n"
      + _funktion(t, "_mbFilterMerken") + "\n" + _funktion(t, "filterMailboxes") + r"""
      const v = [];
      filterMailboxes(); v.push(_ids['mb-filter-aktiv'].textContent);
      _ids['mb-filter'].value = 'abc'; filterMailboxes(); v.push(_ids['mb-filter-aktiv'].textContent);
      _ids['mb-sig-filter'].value = 'an'; _ids['mb-ruhend-filter'].checked = true;
      filterMailboxes(); v.push(_ids['mb-filter-aktiv'].textContent);
      console.log(JSON.stringify(v));
    """)
    # Die Suche steht offen in der Leiste und zählt nicht mit.
    assert r == ["", "", " (2)"]
