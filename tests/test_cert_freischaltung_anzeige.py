"""Zertifikatsbezug nicht freigeschaltet: Die Oberfläche nennt den Grund vom Hub.

Der Hub schaltet den Bezug von selbst frei (Zustimmung + Guthaben bzw.
automatische Aufladung) und schickt in `reason` den fehlenden Schritt. Bis
v1.10.95 zeigte `certLoadEligibility()` diesen Grund nur, wenn der Bezug schon
freigeschaltet war; sonst blieb das Feld leer und das Kopfzeichen sagte
„noch nicht aktiv“, ohne den Weg zu nennen.

Gespielt wird die Funktion in Node mit der Antwort, die eine Installation
ohne Guthaben tatsächlich bekam.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WURZEL = Path(__file__).parent.parent
SEITE = (WURZEL / "app/webui/templates/settings_connect.html").read_text(encoding="utf-8")

GRUND = ("Für den Zertifikatsbezug fehlt noch: Guthaben aufladen oder die "
         "automatische Aufladung einrichten. Danach wird er automatisch "
         "freigeschaltet — ein Antrag ist nicht nötig.")


def _funktion() -> str:
    m = re.search(r"async function certLoadEligibility\(manual\) \{.*?\n\}\n", SEITE, re.S)
    assert m, "certLoadEligibility nicht gefunden"
    return m.group(0)


def _spielen(antwort: dict) -> dict:
    if not shutil.which("node"):
        pytest.skip("node fehlt")
    js = """
const els = {};
function el(id){ return els[id] || (els[id] = {id, innerHTML:'', textContent:'', className:'', style:{}}); }
const document = { getElementById: el };
function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;'); }
function errText(d){ return 'x'; }
function _renderCertSteps(){} function _renderCertDomains(){} function _certMsg(){}
const ANTWORT = %s;
async function getJSON(){ return ANTWORT; }
%s
certLoadEligibility(false).then(() => console.log(JSON.stringify({
  feld: el('cert-eligibility').innerHTML, zeichen: el('cert-header-badge').textContent })));
""" % (json.dumps(antwort), _funktion())
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_nicht_freigeschaltet_zeigt_den_grund():
    aus = _spielen({"ok": False, "cert_issuing_enabled": False, "terms_accepted": True,
                    "reason": GRUND})
    assert "Guthaben aufladen" in aus["feld"], aus
    assert "Voraussetzung" in aus["zeichen"], aus


def test_freigeschaltet_bleibt_wie_es_war():
    aus = _spielen({"ok": True, "cert_issuing_enabled": True, "used_this_month": 0,
                    "monthly_limit": 0, "verified_domains": []})
    assert "aktiviert" in aus["feld"] and "aktiviert" in aus["zeichen"]


def test_ohne_grund_kein_leerer_hinweis():
    aus = _spielen({"ok": False, "cert_issuing_enabled": False})
    assert aus["feld"] == "" and aus["zeichen"] == "⚠ noch nicht aktiv"
