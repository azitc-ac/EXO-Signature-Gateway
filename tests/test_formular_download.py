"""Uploads (FormData) und Downloads (Blob): je EIN Weg in common.js.

Bis 2026-10-21 baute jede der zehn Upload-Stellen fetch + json().catch + ihre
eigene ok-Deutung; manche warfen bei Netzwerkfehlern, eine las r.json() ohne
Absicherung. Die beiden Downloads waren Zeile für Zeile kopiert.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_knopf_aktion import common_funktion

VORLAGEN = Path(__file__).parent.parent / "app/webui/templates"

# Eigenständige Seite ohne common.js (Erneuerung per Link)
OHNE_COMMON = {"smime_selfservice.html"}

_FETCH = r"""
let ANTWORT;
function antwort(status, json, ct) {
  return {ok: status < 400, status, headers: {get: k => k === 'content-type' ? (ct || 'application/json') : null},
          json: async () => json, blob: async () => ({size: 2048})};
}
globalThis.fetch = async () => { if (ANTWORT === 'netz') throw new Error('offline'); return ANTWORT; };
const log = {};
"""


def _node(szenario: str) -> dict:
    js = (_FETCH + common_funktion("_anfrage") + "\n" + common_funktion("sendeFormular") + "\n" + common_funktion("dateiHerunterladen")
          + "\n(async () => {\n" + szenario + "\nconsole.log(JSON.stringify(log));\n})();")
    return json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True,
                                     check=True).stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node fehlt")
def test_sende_formular_wirft_nie_und_deutet_ok():
    log = _node("""
      ANTWORT = antwort(200, {imported: 3});           log.ok = await sendeFormular('/x', {});
      ANTWORT = antwort(400, {detail: 'kaputt'});      log.detail = await sendeFormular('/x', {});
      ANTWORT = antwort(502, null, 'text/html');       log.html = await sendeFormular('/x', {});
      ANTWORT = antwort(200, {ok: false, error: 'n'}); log.falsch = await sendeFormular('/x', {});
      ANTWORT = 'netz';                                log.netz = await sendeFormular('/x', {});""")
    assert log["ok"] == {"imported": 3, "ok": True}
    assert log["detail"] == {"detail": "kaputt", "ok": False}
    assert log["html"] == {"ok": False, "error": "HTTP 502"}
    assert log["falsch"]["ok"] is False                 # ok aus der Antwort gilt, nicht der Status
    assert log["netz"]["ok"] is False and "offline" in log["netz"]["error"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node fehlt")
def test_datei_herunterladen_wirft_nie():
    log = _node("""
      const geklickt = [];
      globalThis.URL = {createObjectURL: () => 'blob:1', revokeObjectURL: () => {}};
      globalThis.document = {createElement: () => ({click() { geklickt.push(this.download); }})};
      ANTWORT = antwort(200, null);
      ANTWORT.headers.get = k => k === 'Content-Disposition' ? 'attachment; filename="b-1.zip"' : null;
      log.ok = await dateiHerunterladen('/x', 'vorgabe.zip');
      ANTWORT = antwort(404, null);  log.fehlt = await dateiHerunterladen('/x', 'v.zip');
      ANTWORT = 'netz';              log.netz = await dateiHerunterladen('/x', 'v.zip');
      log.geklickt = geklickt;""")
    assert log["ok"] == {"ok": True, "name": "b-1.zip", "groesse": 2048}
    assert log["geklickt"] == ["b-1.zip"]                 # nur einmal, nur bei Erfolg
    assert log["fehlt"] == {"ok": False, "error": "HTTP 404"}
    assert log["netz"]["ok"] is False


def test_kein_handgebauter_upload_oder_download():
    funde = []
    for p in sorted(VORLAGEN.glob("*.html")):
        if p.name in OHNE_COMMON:
            continue
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r"body:\s*(?:fd|form\w*)\b|\.blob\(\)", text):
            funde.append(f"{p.name}:{text.count(chr(10), 0, m.start()) + 1}: {m.group(0)}")
    assert not funde, "sendeFormular()/dateiHerunterladen() aus common.js nutzen:\n" + "\n".join(funde)


def test_neustart_nur_ueber_base_html():
    funde = [p.name for p in sorted(VORLAGEN.glob("*.html"))
             if p.name != "base.html" and "fetch('/api/restart'" in p.read_text(encoding="utf-8")]
    assert not funde, "neustartAnstossen() aus base.html nutzen: " + ", ".join(funde)
