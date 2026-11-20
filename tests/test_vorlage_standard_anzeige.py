"""Die Standardvorlage heißt intern „default“ und in der Oberfläche „Standard“.

Der Schlüssel bleibt, weil er in MAILBOX_CONFIG, Richtlinien, Gruppen-Slots,
Sicherungen und Add-in-Zwischenspeichern steht. Geprüft wird:
* Python (`signature_engine.anzeigename`) und JS (`static/vorlagen_name.js`)
  bilden gleich ab;
* jede Seite, die `vorlageAnzeige` ruft, lädt die Datei auch (sonst
  ReferenceError zur Laufzeit — jsscopecheck sieht Vorlagen einzeln);
* der Name „Standard“ lässt sich weder anlegen noch durch Umbenennen oder
  Kopieren erzeugen;
* die Aufgabe meldet eine vorhandene eigene Vorlage „Standard“.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import signature_engine  # noqa: E402

JS = WURZEL / "app/webui/static/vorlagen_name.js"
VORLAGEN = WURZEL / "app/webui/templates"
FAELLE = ["default", "Standard", "Minimal", "", "default-alt", "signature"]


def test_python_bildet_default_auf_standard_ab():
    assert signature_engine.anzeigename("default") == "Standard"
    assert signature_engine.anzeigename("default-alt") == "default-alt"
    assert signature_engine.anzeigename(None) == ""


@pytest.mark.skipif(shutil.which("node") is None, reason="node fehlt")
def test_js_und_python_bilden_gleich_ab():
    js = JS.read_text(encoding="utf-8") + "\nconsole.log(JSON.stringify(" \
        + json.dumps(FAELLE) + ".map(vorlageAnzeige)));"
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == [signature_engine.anzeigename(n) for n in FAELLE]


def _seite_mit_base(text: str) -> bool:
    return bool(re.search(r'{%\s*extends\s+"base\.html"', text))


def test_jede_nutzende_seite_laedt_die_datei():
    base = (VORLAGEN / "base.html").read_text(encoding="utf-8")
    assert "/static/vorlagen_name.js" in base
    nutzer = []
    for p in sorted(VORLAGEN.glob("*.html")):
        t = p.read_text(encoding="utf-8")
        if "vorlageAnzeige(" not in t:
            continue
        nutzer.append(p.name)
        assert _seite_mit_base(t) or "/static/vorlagen_name.js" in t, p.name
    # Die Stellen aus v1.12.113 — fällt eine weg, wurde die Anzeige zurückgebaut.
    for pflicht in ("mailboxes.html", "self.html", "addin_compose.html",
                    "richtlinienergebnis.html", "preview.html", "template_editor.html"):
        assert pflicht in nutzer, pflicht


def test_keine_auswahl_zeigt_den_rohen_schluessel():
    """Die Optionsbeschriftungen der Vorlagenauswahl laufen über die Abbildung."""
    t = (VORLAGEN / "mailboxes.html").read_text(encoding="utf-8")
    assert "opt.textContent = vorlageAnzeige(name)" in t
    assert "document.createTextNode(vorlageAnzeige(name))" in t
    assert "'Standard ('" not in t, "erbende Option heißt „Vorgabe (…)“"
    editor = (VORLAGEN / "template_editor.html").read_text(encoding="utf-8")
    assert "{{ t|vorlage }}</option>" in editor


@pytest.mark.parametrize("name", ["Standard", "standard", "STANDARD", "default", "signature"])
def test_name_ist_reserviert(name):
    assert signature_engine.name_reserviert(name)


@pytest.mark.parametrize("name", ["Standard-eigen", "Std", "Minimal", "default-alt"])
def test_aehnliche_namen_bleiben_frei(name):
    assert not signature_engine.name_reserviert(name)


@pytest.fixture
def client(tmp_path, monkeypatch):
    import config
    from fastapi.testclient import TestClient
    sys.path.insert(0, str(WURZEL / "app/webui"))
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    (tmp_path / "signature.html").write_text("<p>Std</p>")
    (tmp_path / "Kurz.html").write_text("<p>Kurz</p>")
    from webui.app import app
    from webui import deps
    app.dependency_overrides[deps._check_auth] = lambda: "admin"
    try:
        yield TestClient(app), tmp_path
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("weg,body", [
    ("/api/templates/standard/create", {"kind": "signatur"}),
    ("/api/templates/Kurz/rename", {"ziel": "Standard"}),
    ("/api/templates/Kurz/duplicate", {"ziel": "STANDARD"}),
])
def test_routen_verweigern_den_namen(client, weg, body):
    c, verz = client
    r = c.post(weg, json=body)
    assert r.status_code == 400, r.text
    assert "vorbehalten" in r.text
    assert not any(p.stem.lower() == "standard" for p in verz.iterdir())


def test_aufgabe_meldet_eigene_vorlage_standard(tmp_path, monkeypatch):
    import abnahme
    import config
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    (tmp_path / "signature.html").write_text("x")
    assert abnahme._vorlage_standard_doppelt()["zustand"] == abnahme.OK
    (tmp_path / "Standard.html").write_text("x")
    assert abnahme._vorlage_standard_doppelt()["zustand"] == abnahme.OFFEN
