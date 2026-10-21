"""Zustimmung zu Rechtstexten (_zustimmung.html) — Verhalten, nicht nur Aufbau.

Der Dialog hält fest, wer welcher FASSUNG (version + content_hash) in welchem
ZUSAMMENHANG (context) zugestimmt hat. Seit 2026-10-21 ein Baustein für die
Gates A (Hub), B (Rechnungskauf) und C (Auftragsverarbeitung) statt dreier
eigener Dialoge. Das Skript läuft hier in Node gegen eine nachgebaute Seite.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

from test_knopf_aktion import common_funktion

VORLAGEN = Path(__file__).parent.parent / "app/webui/templates"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node fehlt")

ZWEI = [("hub-terms", "Hub-Nutzungsbedingungen", "Satz <strong>A</strong>"),
        ("license-supplement", "Lizenzbedingungen-Ergänzung", "Satz B")]


def _rendern(dokumente=ZWEI, kontext="hub_connect"):
    env = Environment(loader=FileSystemLoader(str(VORLAGEN)), autoescape=True)
    return env.from_string('{% from "_zustimmung.html" import zustimmung_dialog %}'
                           '{{ zustimmung_dialog("hub", "Titel", kontext, dokumente, "OK") }}'
                           ).render(dokumente=dokumente, kontext=kontext)


def test_aufbau():
    html = _rendern().split("<script>")[0]                   # nur das HTML, nicht die Steuerung
    assert 'data-dokumente="hub-terms,license-supplement"' in html
    assert 'data-kontext="hub_connect"' in html
    assert html.count('class="zs-haken"') == 2                # ein Haken je Dokument
    assert html.count("zs-reiter") == 2 and "zs-schritt" in html
    assert "Satz <strong>A</strong>" in html                  # Satz als HTML, nicht maskiert
    einzel = _rendern([("dpa", "AVV", "Satz")], "support_upload").split("<script>")[0]
    assert "zs-reiter" not in einzel and "zs-schritt" not in einzel


# Nachgebaute Seite: genau die Elemente, die das Skript anspricht.
_STUBS = r"""
const aufrufe = {post: [], msg: [], aktion: 0};
let STATUS = {}, POST_OK = true;
function el(cls){ return {cls, dataset:{}, style:{}, disabled:false, checked:false, textContent:'', innerHTML:'',
  classList:{add(){},remove(){},toggle(){}}, matches: () => true}; }
const box = el('box'); box.dataset = {dokumente:'hub-terms,license-supplement', kontext:KONTEXT};
box.style.display = 'none';
const teile = {'.zs-text':el('t'), '.zs-schritt':el('s'), '.zs-ok':el('ok'), '.zs-meldung':el('m')};
const haken = [el('h0'), el('h1')], reiter = [el('r0'), el('r1')];
reiter[0].dataset.i = '0'; reiter[1].dataset.i = '1';
box.querySelector = s => teile[s];
box.querySelectorAll = s => s === '.zs-haken' ? haken : s === '.zs-reiter' ? reiter : [];
globalThis.window = globalThis;
globalThis.document = {getElementById: id => id === 'zs-hub' ? box : null};
globalThis.showMsg = (e, t, ok) => { aufrufe.msg.push([t, ok]); e.style.display = 'block'; };
globalThis.errText = d => (d && (d.error || d.detail)) || 'Fehler';
globalThis._mdToHtml = t => '<p>' + t + '</p>';
globalThis.getJSON = async url => {
  if (url.startsWith('/api/legal/status')) return STATUS;
  const id = decodeURIComponent(url.split('/').pop().split('?')[0]);
  return {ok: true, text: 'Text ' + id, version: 'v-' + id, content_hash: 'h-' + id};
};
globalThis.postJSON = async (url, body) => { aufrufe.post.push(body); return POST_OK ? {ok:true} : {ok:false, error:'nein'}; };
const aktion = async () => { aufrufe.aktion++; };
const warte = () => new Promise(r => setTimeout(r, 0));
"""


def _lauf(szenario: str, kontext: str = "hub_connect") -> dict:
    html = _rendern(kontext=kontext)
    gerendert = re.search(r'data-kontext="([^"]*)"', html).group(1)   # wie der Browser: aus dem Attribut
    skript = re.search(r"<script>([\s\S]*?)</script>", html).group(1)
    # knopfAktion/laeuftZeigen echt aus common.js — keine Attrappe, die vom Original abweichen kann
    echt = common_funktion("laeuftZeigen") + "\n" + common_funktion("knopfAktion") + "\n"
    js = f"const KONTEXT = {json.dumps(gerendert)};\n" + _STUBS + echt + skript + "\n(async () => {\n" + szenario + "\n" \
        "console.log(JSON.stringify({aufrufe, display: box.style.display, ok_gesperrt: teile['.zs-ok'].disabled}));\n})();"
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_alles_zugestimmt_aktion_sofort():
    r = _lauf("STATUS = {ok:true, documents:{'hub-terms':{accepted:true}, 'license-supplement':{accepted:true}}};"
              "await zustimmungDann('hub', aktion);")
    assert r["aufrufe"]["aktion"] == 1 and r["aufrufe"]["post"] == [] and r["display"] == "none"


def test_fehlende_zustimmung_dialog_dann_aktion():
    r = _lauf("STATUS = {ok:true, documents:{'hub-terms':{accepted:true}}};"
              "await zustimmungDann('hub', aktion); await warte();"
              "if (aufrufe.aktion !== 0 || box.style.display !== 'flex' || !teile['.zs-ok'].disabled) throw new Error('vorher');"
              "haken[0].checked = true; zustimmungHaken('hub');"
              "if (!teile['.zs-ok'].disabled) throw new Error('ein Haken reicht nicht');"
              "haken[1].checked = true; zustimmungHaken('hub');"
              "await zustimmungBestaetigen('hub');")
    a = r["aufrufe"]
    assert a["aktion"] == 1 and r["display"] == "none"
    assert a["post"] == [
        {"document_id": "hub-terms", "version": "v-hub-terms", "content_hash": "h-hub-terms", "context": "hub_connect"},
        {"document_id": "license-supplement", "version": "v-license-supplement",
         "content_hash": "h-license-supplement", "context": "hub_connect"},
    ]


def test_speichern_scheitert_keine_aktion():
    r = _lauf("STATUS = {ok:true, documents:{}}; POST_OK = false;"
              "await zustimmungDann('hub', aktion);"
              "haken.forEach(h => h.checked = true); zustimmungHaken('hub');"
              "await zustimmungBestaetigen('hub');")
    assert r["aufrufe"]["aktion"] == 0 and r["display"] == "flex"
    assert r["aufrufe"]["msg"][-1][1] is False and "nein" in r["aufrufe"]["msg"][-1][0]
    assert r["ok_gesperrt"] is False                       # erneut versuchbar


def test_status_unlesbar_server_entscheidet():
    r = _lauf("STATUS = {ok:false, error:'HTTP 500'}; await zustimmungDann('hub', aktion);")
    assert r["aufrufe"]["aktion"] == 1 and r["display"] == "none"


def test_erneutes_oeffnen_setzt_haken_zurueck():
    r = _lauf("STATUS = {ok:true, documents:{}};"
              "await zustimmungDann('hub', aktion); haken.forEach(h => h.checked = true);"
              "zustimmungSchliessen('hub'); await zustimmungDann('hub', aktion);"
              "if (haken.some(h => h.checked)) throw new Error('Haken blieb');")
    assert r["ok_gesperrt"] is True and r["aufrufe"]["aktion"] == 0


def test_kontext_kommt_aus_dem_aufruf():
    """Jedes Gate hält seinen eigenen Zusammenhang fest (hub_connect, invoice_request,
    support_upload) — nicht einen fest eingetragenen."""
    r = _lauf("STATUS = {ok:true, documents:{}}; await zustimmungDann('hub', aktion);"
              "haken.forEach(h => h.checked = true); zustimmungHaken('hub');"
              "await zustimmungBestaetigen('hub');", kontext="support_upload")
    assert {p["context"] for p in r["aufrufe"]["post"]} == {"support_upload"}


_AUFRUF = re.compile(r'zustimmung_dialog\(\s*"([\w-]+)"\s*,\s*"[^"]*"\s*,\s*"([\w-]+)"\s*,\s*\[(.*?)\]\s*,', re.S)


def test_dialoge_verlangen_was_der_server_verlangt():
    """Jeder Dialog fragt GENAU die Dokumente ab, die legal_consent.CONTEXT_DOCUMENTS
    für seinen Kontext prüft — sonst meldet der Dialog „erledigt“ und der Server
    lehnt die Aktion trotzdem ab (oder umgekehrt). Und jede Seite, die
    zustimmungDann('x') ruft, bindet den Dialog x selbst ein."""
    import legal_consent
    gesehen = set()
    for p in sorted(VORLAGEN.glob("*.html")):
        t = re.sub(r"\{#.*?#\}", "", p.read_text(encoding="utf-8"), flags=re.S)   # Beispiele in Kommentaren
        dialoge = {}
        for kennung, kontext, docs in _AUFRUF.findall(t):
            ids = re.findall(r'\(\s*"([\w-]+)"', docs)
            assert ids == legal_consent.CONTEXT_DOCUMENTS[kontext], (p.name, kennung, ids)
            dialoge[kennung] = kontext
            gesehen.add(kontext)
        for kennung in re.findall(r"zustimmungDann\('([\w-]+)'", t):
            assert kennung in dialoge, f"{p.name}: zustimmungDann('{kennung}') ohne Dialog"
    assert {"hub_connect", "invoice_request", "support_upload"} <= gesehen
