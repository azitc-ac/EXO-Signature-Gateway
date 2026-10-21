"""Knöpfe mit Aktion: EIN Weg (knopfAktion/laeuftZeigen in common.js).

Bestand 2026-10-21: 87 Funktionen in 16 Vorlagen sperrten ihren Knopf von
Hand, in vier Bauweisen. 15 gaben ihn mit `finally` frei, die übrigen blieben
bei einem Netzwerkfehler gesperrt. Einen drehenden Pfeil hatte genau einer (das
Update), und zwar über einen eigenen Helfer, den keine andere Seite kannte.

Geprüft wird:
  * das Verhalten von knopfAktion() — in Node, gegen die echte Funktion aus
    common.js (Sperre, Pfeil, Rückbau auch bei Ausnahme, gesperrtLassen,
    Fortschrittsanzeige neben dem Knopf);
  * dass keine Vorlage wieder einen Knopf von Hand um ein `await` sperrt
    (bewusste Ausnahmen mit Grund in AUSNAHMEN);
  * dass der Pfeil nur aus common.js + style.css kommt.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WURZEL = Path(__file__).parent.parent
VORLAGEN = WURZEL / "app/webui/templates"
STATIC = WURZEL / "app/webui/static"


def common_funktion(name: str) -> str:
    """Quelltext einer Funktion aus common.js (für Node-Tests anderer Bausteine)."""
    text = (STATIC / "common.js").read_text(encoding="utf-8")
    m = re.search(r"(?:async\s+)?function\s+" + name + r"\s*\(", text)
    assert m, name
    i, tiefe = text.index("{", m.end()), 0
    for j in range(i, len(text)):
        tiefe += (text[j] == "{") - (text[j] == "}")
        if tiefe == 0:
            return text[m.start():j + 1]
    raise AssertionError(name)


# ── Verhalten ────────────────────────────────────────────────────────────────

_DOM = r"""
function el(tag, html) {
  const e = {tagName: tag, dataset: {}, style: {}, hidden: tag !== 'BUTTON', disabled: false,
             innerHTML: html || '', klassen: new Set()};
  Object.defineProperty(e, 'textContent', {get() { return e.innerHTML; }, set(v) { e.innerHTML = String(v); }});
  e.classList = {add: k => e.klassen.add(k)};
  e.matches = sel => tag === 'BUTTON';
  return e;
}
const knopf = el('BUTTON', 'Senden <b>!</b>'), anzeige = el('SPAN', '');
globalThis.document = {getElementById: id => ({knopf, anzeige})[id] || null};
const zustand = e => ({disabled: e.disabled, laeuft: 'laeuft' in e.dataset, html: e.innerHTML,
                       hidden: e.hidden, fortschritt: e.klassen.has('fortschritt')});
const log = {};
"""


def _node(szenario: str) -> dict:
    js = (_DOM + common_funktion("laeuftZeigen") + "\n" + common_funktion("knopfAktion")
          + "\n(async () => {\n" + szenario + "\nconsole.log(JSON.stringify(log));\n})();")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


node_fehlt = pytest.mark.skipif(shutil.which("node") is None, reason="node fehlt")


@node_fehlt
def test_knopf_waehrend_und_danach():
    log = _node("""
      log.erg = await knopfAktion('knopf', 'Läuft…', async () => { log.waehrend = zustand(knopf); return {ok: true}; });
      log.danach = zustand(knopf);""")
    assert log["waehrend"] == {"disabled": True, "laeuft": True, "html": "Läuft…",
                               "hidden": False, "fortschritt": False}
    # Inhalt samt Markup zurück, nicht nur der Text
    assert log["danach"] == {"disabled": False, "laeuft": False, "html": "Senden <b>!</b>",
                             "hidden": False, "fortschritt": False}
    assert log["erg"] == {"ok": True}


@node_fehlt
def test_rueckbau_auch_wenn_die_arbeit_wirft():
    log = _node("""
      try { await knopfAktion(knopf, 'x', async () => { throw new Error('weg'); }); }
      catch (e) { log.fehler = e.message; }
      log.danach = zustand(knopf);""")
    assert log["fehler"] == "weg"                      # nicht verschluckt
    assert log["danach"]["disabled"] is False and log["danach"]["laeuft"] is False
    assert log["danach"]["html"] == "Senden <b>!</b>"


@node_fehlt
def test_gesperrt_lassen_nur_bei_erfolg():
    log = _node("""
      await knopfAktion(knopf, 'Neustart…', async () => ({ok: true}), {gesperrtLassen: true});
      log.ok = zustand(knopf);
      knopf.disabled = false; delete knopf.dataset.laeuft; knopf.innerHTML = 'Senden';
      await knopfAktion(knopf, 'Neustart…', async () => ({ok: false}), {gesperrtLassen: true});
      log.fehler = zustand(knopf);""")
    # Erfolg: Pfeil dreht weiter, bis die Seite neu lädt
    assert log["ok"]["disabled"] is True and log["ok"]["laeuft"] is True
    # Fehlschlag: wieder bedienbar
    assert log["fehler"] == {"disabled": False, "laeuft": False, "html": "Senden",
                             "hidden": False, "fortschritt": False}


@node_fehlt
def test_fortschrittsanzeige_neben_dem_knopf():
    log = _node("""
      await knopfAktion(knopf, 'Erstelle ZIP…', async (text) => {
        log.anfang = [zustand(knopf), zustand(anzeige)];
        text('Teilschritt 2');
        log.phase = anzeige.textContent;
        return {ok: true};
      }, {anzeige: 'anzeige'});
      log.danach = [zustand(knopf), zustand(anzeige)];""")
    k, a = log["anfang"]
    assert k["disabled"] is True and k["laeuft"] is False and k["html"] == "Senden <b>!</b>"  # Pfeil an der Anzeige
    assert a == {"disabled": False, "laeuft": True, "html": "Erstelle ZIP…", "hidden": False, "fortschritt": True}
    assert log["phase"] == "Teilschritt 2"
    k, a = log["danach"]
    assert k["disabled"] is False and a["hidden"] is True and a["laeuft"] is False and a["html"] == ""


# ── Keine Handarbeit mehr in den Vorlagen ─────────────────────────────────────

_SPERRE = re.compile(r"(?<![\w.$])([\w.$]+(?:\([^()]*\))?)\.disabled\s*=\s*true")
_FUNKTION = re.compile(r"(?:function\s+(\w+)|(?:window\.)?(\w+)\s*=\s*(?:async\s+)?function\b)")

# (Datei, Funktion, gesperrtes Element) → Grund. Wer hier etwas einträgt, nennt,
# warum knopfAktion() nicht passt.
AUSNAHMEN = {
    ("backup.html", "_updLaeuft", "document.getElementById('upd-btn')"):
        "Update läuft über viele Abfragetakte und einen Neustart; nutzt laeuftZeigen()",
    ("backup.html", "checkUpdate", "document.getElementById('upd-btn')"):
        "sperrt den Update-Knopf, solange die Prüfung läuft (Zustand, kein Laufanzeiger)",
    ("backup.html", "_updCheckWatcher", "document.getElementById('upd-btn')"):
        "Zustand: ohne Watcher kein Update",
    ("backup.html", "backupInhaltLaden", "knopf"):
        "Zustand: Wiederherstellen erst, wenn ein Backup gelesen ist",
    ("advanced.html", "wdDeployStart", "btn"):
        "Bereitstellung läuft über viele Abfragetakte; nutzt laeuftZeigen()",
    ("advanced.html", "wdLoadSubs", "sel"): "Auswahlliste, kein Knopf",
    ("advanced.html", "wdSubGewaehlt", "rg"): "Auswahlliste, kein Knopf",
    ("setup.html", "loadKvSubscriptions", "sel"): "Auswahlliste, kein Knopf",
    ("setup.html", "loadKvVaults", "sel"): "Auswahlliste, kein Knopf",
    ("setup.html", "loadKvResourceGroups", "sel"): "Auswahlliste, kein Knopf",
    ("setup.html", "saveModeSelection", "smimeCb"): "Kästchen, Zustand des Modus",
    ("setup.html", "_setUrl", "document.getElementById('kv-save-btn')"):
        "Zustand: Speichern erst nach bestandenem Test",
    ("setup.html", "onKvUrlInput", "document.getElementById('kv-save-btn')"):
        "Zustand: Speichern erst nach bestandenem Test",
    ("setup.html", "testKeyvault", "document.getElementById('kv-save-btn')"):
        "Zustand: Speichern erst nach bestandenem Test",
    ("addin_compose.html", "loadSig", "document.getElementById('btn-insert')"):
        "Zustand: Einfügen erst, wenn die Signatur geladen ist",
    ("self.html", "senden", "btn"): "eigenständige Seite (Teams/Outlook) ohne common.js",
    ("smime_selfservice.html", "doUpload", "btn"): "eigenständige Seite ohne common.js",
}


def _handsperren():
    for p in sorted(VORLAGEN.glob("*.html")):
        text = p.read_text(encoding="utf-8")
        for m in _SPERRE.finditer(text):
            name = m.group(1)
            rest = text[m.end():m.end() + 3000]
            frei = re.search(re.escape(name) + r"\.disabled\s*=\s*(?:false|!)", rest)
            stueck = rest[:frei.start()] if frei else rest[:600]
            if not re.search(r"\bawait\b|\.then\(", stueck):
                continue
            funktionen = [f.group(1) or f.group(2) for f in _FUNKTION.finditer(text, 0, m.start())]
            yield (p.name, funktionen[-1] if funktionen else "?", name), \
                  text.count("\n", 0, m.start()) + 1


def test_keine_handgebaute_knopfsperre():
    neu = [f"{d}:{z} in {fn}(): {n}.disabled = true … await" for (d, fn, n), z in _handsperren()
           if (d, fn, n) not in AUSNAHMEN]
    assert not neu, ("Knopf von Hand gesperrt — knopfAktion() aus common.js nutzen "
                     "(oder mit Grund in AUSNAHMEN):\n" + "\n".join(neu))


def test_ausnahmen_sind_nicht_veraltet():
    gefunden = {k for k, _ in _handsperren()}
    veraltet = [k for k in AUSNAHMEN if k not in gefunden]
    assert not veraltet, "Ausnahme ohne Fundstelle — streichen:\n" + "\n".join(map(str, veraltet))


def test_pruefer_erkennt_muster():
    text = "async function f(){ btn.disabled = true; const r = await postJSON('/x'); btn.disabled = false; }"
    assert _SPERRE.search(text)
    assert re.search(r"\bawait\b", text[_SPERRE.search(text).end():])


def test_pfeil_nur_aus_common_js_und_style_css():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert re.search(r"@keyframes spin\b", css)
    assert re.search(r"\.btn\[data-laeuft\]::before[\s\S]{0,120}content:\s*'⟳'", css)
    assert re.search(r"\.fortschritt\[data-laeuft\]::before", css)
    assert ".fortschritt" in (STATIC / "dark-mode.css").read_text(encoding="utf-8")
    assert "dataset.laeuft" in common_funktion("laeuftZeigen")
    for p in sorted(VORLAGEN.glob("*.html")):
        text = p.read_text(encoding="utf-8")
        assert "dataset.laeuft" not in text and "data-laeuft" not in text, p.name
        assert "_spin(" not in text, p.name
        if p.name != "portal.html":                      # eigenständige Seite ohne style.css
            assert "@keyframes spin" not in text, p.name
