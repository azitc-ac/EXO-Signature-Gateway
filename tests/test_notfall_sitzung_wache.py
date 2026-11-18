"""Webmail: Eine abgelaufene Sitzung führt bei JEDEM Inhaltsabruf zur Anmeldung.

Befund (Klicktest Teams, 18.11.2026): Nach einem Neustart des Gateways zeigte nur
die Ordnerleiste „Nicht angemeldet.“, Kopf, Liste und Befehlsleiste standen mit
dem alten Stand weiter da. Die Wache in notfall.html (SITZUNG-WACHE) hängt sich
an getJSON/postJSON. Hier in Node mit nachgespielten Abrufen ausgeführt.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

SEITE = Path(__file__).resolve().parent.parent / "app" / "webui" / "templates" / "notfall.html"


def _block() -> str:
    s = SEITE.read_text(encoding="utf-8")
    a = s.index("// SITZUNG-WACHE BEGINN")
    b = s.index("// SITZUNG-WACHE ENDE")
    return s[a:b]


def _lauf(urls_mit_status):
    node = shutil.which("node")
    if not node:
        pytest.skip("node fehlt")
    skript = """
let nb = {postfach: 'pf@firma.at', ordner: 'ordner-7'};
let neu = 0; const speicher = {};
const location = {reload() { neu++; }};
const sessionStorage = {setItem(k, v) { speicher[k] = v; }, getItem(k) { return speicher[k] ?? null; },
                        removeItem(k) { delete speicher[k]; }};
let antwort = {};
async function getJSON(url) { return antwort; }
async function postJSON(url, body) { return antwort; }
%s
(async () => {
  const fall = %s; const erg = [];
  for (const [url, status, art] of fall) {
    _sitzungWeg = false; neu = 0;
    antwort = status === 200 ? {ok: true} : {ok: false, error: 'x', http: status};
    await (art === 'post' ? postJSON(url, {}) : getJSON(url));
    erg.push([url, neu]);
  }
  _sitzungWeg = false;
  antwort = {ok: false, http: 401};
  await getJSON('/notfall/api/liste?x=1');
  await getJSON('/notfall/api/mail?id=2');      // zweiter Fehlschlag: nur EIN Neuladen
  erg.push(['doppelt', neu]);
  erg.push(['gemerkt', sitzungZurueck()]);
  erg.push(['danach', sitzungZurueck()]);
  console.log(JSON.stringify(erg));
})();
""" % (_block().replace("getJSON = async", "getJSON = async").replace("const _getJSON", "var _getJSON"),
       json.dumps(urls_mit_status))
    # Die Seite deklariert getJSON/postJSON neu; im Test sind sie `function`s — Zuweisung geht.
    out = subprocess.run([node, "-e", skript], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_abgelaufene_sitzung_laedt_neu_und_merkt_den_ordner():
    erg = _lauf([
        ["/notfall/api/liste?postfach=a", 401, "get"],
        ["/notfall/api/senden", 401, "post"],
        ["/notfall/api/kalender?x=1", 401, "get"],
        ["/notfall/api/ich", 401, "get"],             # start() prüft selbst
        ["/notfall/api/faktor", 401, "get"],          # Einrichtung (Microsoft)
        ["/notfall/api/faktor/codes", 401, "post"],
        ["/notfall/anmelden", 401, "post"],           # falsches Kennwort
        ["/notfall/api/liste?postfach=a", 403, "get"],
        ["/notfall/api/liste?postfach=a", 200, "get"],
    ])
    neu = {f"{u}": n for u, n in erg[:9]}
    assert erg[0][1] == 1 and erg[1][1] == 1 and erg[2][1] == 1, erg
    assert [n for _u, n in erg[3:9]] == [0, 0, 0, 0, 0, 0], neu
    assert erg[9] == ["doppelt", 1]
    assert erg[10] == ["gemerkt", {"postfach": "pf@firma.at", "ordner": "ordner-7"}]
    assert erg[11] == ["danach", None]                # einmal gelesen, dann weg


def test_start_kehrt_in_den_gemerkten_ordner_zurueck():
    s = SEITE.read_text(encoding="utf-8")
    start = s[s.index("async function start()"):s.index("function anmeldungZeigen()")]
    assert "sitzungZurueck()" in start and "postfachWaehlen(pf, zurueck && zurueck.ordner)" in start
    wahl = s[s.index("async function postfachWaehlen(pf, ordnerWunsch)"):]
    wahl = wahl[:wahl.index("\n}\n")]
    assert "ordnerWaehlen(gewuenscht || erster.id)" in wahl
