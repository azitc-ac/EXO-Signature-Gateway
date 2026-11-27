"""Links in HTML-Vorschauen öffnen in einem neuen Tab — ohne die Sandbox aufzuweichen.

Bis v1.12.174 öffnete ein Klick auf einen Link in einer Vorschau (Signatur,
Banner, Disclaimer, Abwesenheit, Warteschlange, Selbsttest, Portal,
Notbetrieb) die Seite IM Rahmen. Seither:

* Rahmen mit gleichem Ursprung (`allow-same-origin`): `VORSCHAU_SANDBOX` und
  `vorschauLinksNeuerTab()` aus static/sig_preview.js. Nur http(s)/mailto
  bekommen `_blank`; jeder andere Link (`javascript:` …) wird auf `_self`
  gezwungen — mit `allow-popups-to-escape-sandbox` liefe er sonst in einem
  Tab mit dem Ursprung des Gateways.
* Rahmen ohne Ursprung (Notbetrieb, `sandbox` ohne allow-same-origin): der
  Server setzt `<base target="_blank">` (notbetrieb_lesen.mit_neuem_tab), die
  durchgesetzte CSP bleibt ohne Skript.

Jeder Rahmen in Vorlagen und Skripten muss einem der beiden Wege folgen; wer
einen neuen ohne baut, scheitert hier. Rechte, die das Sicherheitsniveau
senken würden (Skripte, Formulare, Navigation der Hauptseite …), dürfen nie
hinzukommen.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parents[1]
WEBUI = WURZEL / "app" / "webui"
SIG_PREVIEW = (WEBUI / "static" / "sig_preview.js").read_text(encoding="utf-8")

POPUPS = {"allow-popups", "allow-popups-to-escape-sandbox"}
VERBOTEN = {"allow-scripts", "allow-forms", "allow-modals", "allow-top-navigation",
            "allow-top-navigation-by-user-activation", "allow-top-navigation-to-custom-protocols",
            "allow-pointer-lock", "allow-presentation", "allow-orientation-lock"}
# Rahmen ohne eigenen Ursprung, deren Inhalt der Server mit mit_neuem_tab ausliefert.
SERVER_SEITIG = {"notfall.html"}
HANDLER = 'onload="vorschauLinksNeuerTab(this.contentDocument)"'


def _dateien():
    for p in sorted((WEBUI / "templates").glob("*.html")):
        yield p
    for p in sorted((WEBUI / "static").glob("*.js")):
        yield p


def _iframe_tags(text: str) -> list[str]:
    """Jedes <iframe …> als Literal (in HTML wie in JS-Zeichenketten)."""
    return re.findall(r"<iframe\b[^>]*>", text)


def _sandbox(tag: str) -> set[str] | None:
    m = re.search(r'\bsandbox="([^"]*)"', tag)
    return None if m is None else set(m.group(1).split())


def test_jeder_rahmen_folgt_einem_der_beiden_wege():
    befunde, gesehen = [], 0
    for p in _dateien():
        text = p.read_text(encoding="utf-8")
        for tag in _iframe_tags(text):
            gesehen += 1
            rechte = _sandbox(tag)
            if rechte is None:
                befunde.append(f"{p.name}: Rahmen ohne sandbox: {tag[:90]}")
                continue
            if rechte & VERBOTEN:
                befunde.append(f"{p.name}: Sandbox aufgeweicht {sorted(rechte & VERBOTEN)}: {tag[:90]}")
            if not POPUPS <= rechte:
                befunde.append(f"{p.name}: ohne {sorted(POPUPS - rechte)} — Links bleiben im Rahmen: {tag[:90]}")
            if "allow-same-origin" in rechte:
                if HANDLER not in tag or "/static/sig_preview.js" not in text:
                    befunde.append(f"{p.name}: gleicher Ursprung ohne vorschauLinksNeuerTab: {tag[:90]}")
            elif p.name not in SERVER_SEITIG:
                befunde.append(f"{p.name}: Rahmen ohne Ursprung ausserhalb von {sorted(SERVER_SEITIG)} — "
                               f"liefert der Server den Inhalt mit mit_neuem_tab aus? {tag[:90]}")
        if re.search(r"createElement\(\s*['\"]iframe", text):
            gesehen += 1
            if not re.search(r"setAttribute\(\s*'sandbox',\s*VORSCHAU_SANDBOX\s*\)", text) \
                    or "vorschauLinksNeuerTab(" not in text:
                befunde.append(f"{p.name}: erzeugter Rahmen ohne VORSCHAU_SANDBOX + vorschauLinksNeuerTab")
            if p.name != "sig_preview.js" and "/static/sig_preview.js" not in text:
                befunde.append(f"{p.name}: nutzt den Baustein, lädt sig_preview.js aber nicht")
        # srcdoc nur an Rahmen, die den Weg haben (sonst eine Vorschau an der Wache vorbei)
        for ziel in re.findall(r"getElementById\('([\w-]+)'\)\.srcdoc\s*=", text):
            tag = next((t for t in _iframe_tags(text) if f'id="{ziel}"' in t), "")
            if HANDLER not in tag:
                befunde.append(f"{p.name}: srcdoc an #{ziel} ohne vorschauLinksNeuerTab")
    assert gesehen >= 8, f"nur {gesehen} Rahmen gefunden (Stand v1.12.175: 8) — Suchmuster veraltet?"
    assert not befunde, "\n".join(befunde)


def test_vorschau_sandbox_konstante():
    m = re.search(r"const VORSCHAU_SANDBOX = '([^']*)';", SIG_PREVIEW)
    assert m, "VORSCHAU_SANDBOX fehlt in sig_preview.js"
    assert set(m.group(1).split()) == {"allow-same-origin"} | POPUPS
    assert "setAttribute('sandbox', VORSCHAU_SANDBOX)" in SIG_PREVIEW
    # sigVorschau stellt die Links beim Laden um
    rumpf = SIG_PREVIEW.split("function sigVorschau", 1)[1].split("\nfunction ", 1)[0]
    assert "vorschauLinksNeuerTab(rahmen.contentDocument)" in rumpf


def _funktion(name: str) -> str:
    m = re.search(rf"\nfunction {name}\(.*?\n\}}\n", SIG_PREVIEW, re.S)
    assert m, name
    return m.group(0)


@pytest.mark.skipif(not shutil.which("node"), reason="node fehlt")
def test_nur_http_und_mailto_gehen_in_den_neuen_tab():
    konst = re.search(r"const _VORSCHAU_NEUER_TAB = [^\n]+\n", SIG_PREVIEW).group(0)
    faelle = ["https://exogateway.zarenko.net", "HTTP://beispiel.de/x", "  https://mit-leerraum.de",
              "mailto:max@firma.at", "javascript:alert(1)", " javascript:alert(1)", "JaVaScRiPt:x",
              "data:text/html,<b>x</b>", "#anker", "/relativ", "vbscript:x", ""]
    skript = konst + _funktion("vorschauLinksNeuerTab") + """
const faelle = %s;
const links = faelle.map(h => { const a = {attr: {href: h, target: '_top'}};
  a.getAttribute = k => a.attr[k]; a.setAttribute = (k, v) => { a.attr[k] = v; }; return a; });
const n = vorschauLinksNeuerTab({querySelectorAll: s => {
  if (s !== 'a[href], area[href]') throw new Error(s); return links; }});
console.log(JSON.stringify({n, ziele: links.map(a => [a.attr.target, a.attr.rel || ''])}));
""" % json.dumps(faelle)
    aus = json.loads(subprocess.run(["node", "-e", skript], capture_output=True, text=True,
                                    check=True).stdout)
    erwartet = [("_blank", "noopener noreferrer")] * 4 + [("_self", "")] * 8
    assert [tuple(z) for z in aus["ziele"]] == erwartet, list(zip(faelle, aus["ziele"]))
    assert aus["n"] == 4


def test_notbetrieb_mit_neuem_tab():
    sys.path.insert(0, str(WURZEL / "app"))
    import notbetrieb_lesen as nl
    b = nl.NEUER_TAB
    assert b == '<base target="_blank">'
    assert nl.mit_neuem_tab("<html><HEAD lang=de><title>t</title></HEAD><body>x</body></html>") \
        == f"<html><HEAD lang=de>{b}<title>t</title></HEAD><body>x</body></html>"
    # <header> ist kein Kopf
    assert nl.mit_neuem_tab("<html><body><header>h</header></body></html>") \
        == f"<html>{b}<body><header>h</header></body></html>"
    assert nl.mit_neuem_tab("<!DOCTYPE html><p>x</p>") == f"<!DOCTYPE html>{b}<p>x</p>"
    assert nl.mit_neuem_tab("<p>x</p>") == f"{b}<p>x</p>"
    # eine eigene <base target> der Mail steht dahinter — die erste zählt
    roh = '<html><head><base target="_self"></head><body><a href="https://x.de">x</a></body></html>'
    aus = nl.mit_neuem_tab(roh)
    assert aus.index(b) < aus.index('<base target="_self">')


def test_notbetrieb_csp_nur_um_popups_erweitert():
    sys.path.insert(0, str(WURZEL / "app"))
    import notbetrieb_lesen as nl
    teile = {t.strip().split(" ", 1)[0]: t.strip() for t in nl.INHALT_CSP.split(";")}
    assert set(teile["sandbox"].split()[1:]) == POPUPS
    assert teile["default-src"] == "default-src 'none'" and "script-src" not in teile
    assert teile["form-action"] == "form-action 'none'" and teile["base-uri"] == "base-uri 'none'"
