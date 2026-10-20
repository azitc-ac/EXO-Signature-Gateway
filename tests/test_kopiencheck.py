"""tools/kopiencheck.js findet Kopien auch bei umbenannten Variablen und
ignoriert Ähnlichkeit, die nur aus gleichen Bezeichnern besteht.

Anlass: Fenster-Öffnen in vier Bauweisen und eine vergessene 140-Zeilen-Kopie
(setup.html ↔ debug.html) fielen keiner Prüfung auf, weil die nur bekannte
Kategorien kannte. Der Finder sucht ohne Kategorie — und muss selbst geprüft sein.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WERKZEUG = Path(__file__).parent.parent / "tools/kopiencheck.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node fehlt")

KERN = """
  btn.disabled = true; btn.textContent = 'Lädt…';
  const r = await fetch('/api/x/' + encodeURIComponent(id));
  const d = await r.json().catch(() => ({}));
  btn.disabled = false; btn.textContent = 'Fertig';
  if (d.ok) { showAlert(res, 'success', 'Gespeichert: ' + (d.name || '?')); res.dataset.zustand = 'ok'; }
  else { showAlert(res, 'error', 'Fehler: ' + (d.detail || 'Unbekannt')); res.dataset.zustand = 'fehler'; }
"""


def _lauf(tmp_path, dateien: dict[str, str]):
    for name, inhalt in dateien.items():
        (tmp_path / name).write_text(inhalt, encoding="utf-8")
    out = subprocess.run(["node", str(WERKZEUG), "--json", "--min=40", str(tmp_path)],
                         capture_output=True, text=True, check=True,
                         cwd=WERKZEUG.parent.parent).stdout
    return json.loads(out)


def test_findet_kopie_mit_anderen_namen(tmp_path):
    umbenannt = KERN.replace("btn", "knopf").replace("res", "box").replace(" id)", " nr)")
    g = _lauf(tmp_path, {
        "a.html": "<p>x</p>\n<script>\nasync function eins(btn, res, id) {" + KERN + "}\n</script>",
        "b.html": "<div></div>\n<script>\n{# Jinja #}\nasync function zwei(knopf, box, nr) {"
                  + umbenannt + "}\n</script>",
    })
    assert len(g) == 1
    orte = {Path(o["datei"]).name: o for o in g[0]["orte"]}
    assert set(orte) == {"a.html", "b.html"}
    assert orte["a.html"]["von"] >= 3          # Zeilen beziehen sich auf die Datei, nicht den Block


def test_andere_eigenschaften_sind_keine_kopie(tmp_path):
    """Eigenschaftsnamen tragen die Bedeutung und werden NICHT gleichgemacht."""
    anders = (KERN.replace(".disabled", ".hidden").replace(".textContent", ".title")
                  .replace(".dataset.zustand", ".dataset.state").replace(".json()", ".text()"))
    g = _lauf(tmp_path, {
        "a.js": "async function eins(btn, res, id) {" + KERN + "}",
        "b.js": "async function zwei(btn, res, id) {" + anders + "}",
    })
    assert g == []


def test_vendor_wird_uebersprungen(tmp_path):
    (tmp_path / "vendor").mkdir()
    g = _lauf(tmp_path, {
        "a.js": "async function eins(btn, res, id) {" + KERN + "}",
        "vendor/b.js": "async function zwei(btn, res, id) {" + KERN + "}",
    })
    assert g == []
