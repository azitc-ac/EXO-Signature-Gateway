"""Abrufe in der Oberfläche laufen über getJSON/postJSON/sendJSON/sendeFormular.

Bis 2026-10-21 bauten rund 150 Stellen ihren Abruf von Hand
(`await (await fetch(u)).json()`). Bei einer Antwort ohne JSON — etwa einer
502-Seite des Proxys oder einem Neustart — wirft `json()`, und je nach
Umgebung blieb die Anzeige still leer oder ein Knopf hing. Die Helfer aus
common.js werfen nie und liefern immer `{ok, …}`; `errText(d)` holt die
Meldung. Rohes `fetch` bleibt nur, wo ein Helfer nicht passt — mit Grund.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_knopf_aktion import common_funktion

WURZEL = Path(__file__).parent.parent
VORLAGEN = WURZEL / "app/webui/templates"
STATIC = WURZEL / "app/webui/static"

# Seiten ohne common.js — dort gibt es die Helfer nicht.
OHNE_COMMON_JS = {
    "portal.html": "Empfängerseite des Portals, eigenständig",
    "self.html": "Self-Service in Teams/Outlook, eigenständig",
    "addin_compose.html": "Outlook-Add-in, eigenständig (office.js)",
    "smime_selfservice.html": "eigenständige Seite",
    "verfassen_ereignis.js": "läuft in der Ereignis-Laufzeit von Outlook, ohne DOM und common.js",
    "common.js": "die Helfer selbst",
}

_FETCH = re.compile(r"(?<![\w.$])fetch\(")
_FUNKTION = re.compile(r"(?:function\s+(\w+)|(?:window\.)?(\w+)\s*=\s*(?:async\s+)?(?:function\b|\([^()]*\)\s*=>))")

# (Datei, Funktion) → Grund, warum dort kein Helfer passt.
AUSNAHMEN: dict[tuple[str, str], str] = {
    ("base.html", "neustartAnstossen"):
        "die Verbindung bricht beim Neustart ab; die Antwort wird bewusst nicht gelesen",
    ("base.html", "check"):
        "Online-Punkt: nur r.ok von /health, mit cache:'no-store' (die Helfer setzen keine Optionen)",
    ("debug.html", "loadHealthInline"):
        "zeigt den Rumpf roh als JSON an; getJSON schriebe ok:true hinein",
    ("mailboxes.html", "loadGroups"):
        "Rumpf ist ein Wörterbuch Gruppenname → Mitglieder; getJSON schriebe eine Scheingruppe 'ok' "
        "hinein, die saveGroups zurückschriebe",
    ("setup.html", "validateAddinManifest"): "liest XML per .text()",
}


def _quellen():
    yield from sorted(VORLAGEN.glob("*.html"))
    yield from sorted(STATIC.glob("*.js"))


def _rohe_abrufe():
    for p in _quellen():
        if p.name in OHNE_COMMON_JS:
            continue
        text = p.read_text(encoding="utf-8")
        for m in _FETCH.finditer(text):
            funktionen = [f.group(1) or f.group(2) for f in _FUNKTION.finditer(text, 0, m.start())]
            yield (p.name, funktionen[-1] if funktionen else "?"), text.count("\n", 0, m.start()) + 1


def test_kein_roher_abruf():
    neu = [f"{d}:{z} in {fn}()" for (d, fn), z in _rohe_abrufe() if (d, fn) not in AUSNAHMEN]
    assert not neu, ("fetch() von Hand — getJSON/postJSON/sendJSON/sendeFormular aus "
                     "common.js nutzen (oder mit Grund in AUSNAHMEN):\n" + "\n".join(neu))


def test_ausnahmen_sind_nicht_veraltet():
    gefunden = {k for k, _ in _rohe_abrufe()}
    veraltet = [k for k in AUSNAHMEN if k not in gefunden]
    assert not veraltet, "Ausnahme ohne Fundstelle — streichen:\n" + "\n".join(map(str, veraltet))


def test_seiten_ohne_common_js_laden_es_wirklich_nicht():
    """Sonst wäre die Befreiung oben ein Schlupfloch."""
    for name in OHNE_COMMON_JS:
        p = VORLAGEN / name
        if p.exists():
            t = p.read_text(encoding="utf-8")
            assert "common.js" not in t.replace("ohne common.js", ""), name
            assert "{% extends" not in t, name


def test_pruefer_erkennt_muster():
    t = "async function ladeX(){ const d = await (await fetch('/x')).json(); }"
    assert _FETCH.search(t)
    assert not _FETCH.search("postJSON('/x'); obj.fetch('/y'); _fetch('/z')")


# ── Verhalten der Helfer: gescheiterter Abruf ≠ fachliche Antwort ok:false ────

_SZENARIEN = r"""
function antwort(status, ct, rumpf) {
  return {ok: status >= 200 && status < 300, status,
          headers: {get: k => k.toLowerCase() === 'content-type' ? ct : null},
          json: async () => { if (rumpf instanceof Error) throw rumpf; return JSON.parse(JSON.stringify(rumpf)); }};
}
const FAELLE = {
  netz:        () => { throw new TypeError('Failed to fetch'); },
  proxy502:    () => antwort(502, 'text/html', null),
  anmeldeseite:() => antwort(200, 'text/html; charset=utf-8', null),
  kaputt:      () => antwort(200, 'application/json', new SyntaxError('Unexpected token')),
  fachlich:    () => antwort(200, 'application/json', {ok: false, reason: 'kein Token'}),
  http400:     () => antwort(400, 'application/json', {detail: 'Azure-Zugriff fehlt'}),
  erfolg:      () => antwort(200, 'application/json', {wert: 1}),
};
globalThis.fetch = async (url) => FAELLE[url]();
(async () => {
  const aus = {};
  for (const fall of Object.keys(FAELLE)) {
    aus[fall] = {get: await getJSON(fall), post: await postJSON(fall, {})};
  }
  console.log(JSON.stringify(aus));
})();
"""


@pytest.fixture(scope="module")
def helfer():
    if not shutil.which("node"):
        pytest.skip("node fehlt")
    js = "\n".join(common_funktion(n) for n in ("_abrufFehler", "_anfrage", "getJSON", "postJSON"))
    aus = subprocess.run(["node", "-e", js + _SZENARIEN], capture_output=True, text=True, check=True)
    return json.loads(aus.stdout)


@pytest.mark.parametrize("fall", ["netz", "proxy502", "kaputt"])
def test_gescheiterter_abruf_ist_markiert(helfer, fall):
    for weg in ("get", "post"):
        d = helfer[fall][weg]
        assert d["ok"] is False and d["abrufFehler"] is True and d["error"], (fall, weg, d)
    assert helfer["netz"]["get"]["http"] == 0 and helfer["proxy502"]["get"]["http"] == 502


def test_anmeldeseite_ist_fuer_getjson_kein_erfolg(helfer):
    """Eine HTML-Seite mit 200 (etwa nach Weiterleitung zur Anmeldung) war bis
    2026-10-21 ein leerer Erfolg {ok:true}."""
    assert helfer["anmeldeseite"]["get"]["abrufFehler"] is True


def test_fachliche_antwort_ist_kein_abruffehler(helfer):
    for weg in ("get", "post"):
        d = helfer["fachlich"][weg]
        assert d == {"ok": False, "reason": "kein Token"}, (weg, d)
        e = helfer["erfolg"][weg]
        assert e == {"wert": 1, "ok": True}, (weg, e)


def test_http_fehler_mit_meldung(helfer):
    g, p = helfer["http400"]["get"], helfer["http400"]["post"]
    assert g == {"ok": False, "error": "Azure-Zugriff fehlt", "http": 400}
    assert p["ok"] is False and p["http"] == 400 and p["detail"] == "Azure-Zugriff fehlt"
    assert "abrufFehler" not in g and "abrufFehler" not in p
