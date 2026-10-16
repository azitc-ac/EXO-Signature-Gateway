"""Der Aufgabenbereich darf getippten Text nie löschen.

ANLASS (16.10.2026, Classic Outlook, gemessen per COM)
Mit dem Verfassen-Add-in stand die Signatur schon in der Mail, darüber eine
getippte Zeile. Nach der Anmeldung im Aufgabenbereich war die Zeile weg (der
Text schrumpfte von 48 075 auf 20 604 Zeichen), nach „Einfügen“ ebenso — und
zwei Kennungen `exo-sig-s` steckten ineinander.

Drei Ursachen, je ein Test:
1. Die Erkennung verlangte `id="exo-sig-s"` MIT Anführungszeichen. Classic
   speichert `<div id=exo-sig-s>` ohne und entfernt den Kommentar
   `<!-- exo-sig-start -->` — die Signatur galt als nicht vorhanden.
2. Daraufhin setzte das automatische Einfügen den GANZEN Text auf die Signatur.
   Das automatische Einfügen übernimmt jetzt das Verfassen-Add-in; der Bereich
   fügt nur auf Klick ein.
3. „Einfügen“ griff, wenn es nichts fand, auf den zuletzt selbst gesetzten Text
   zurück — der kennt nichts, was danach getippt wurde. Jetzt setzt es mit
   Mailbox 1.10 nur den Signaturbereich (`setSignatureAsync`).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ADDIN = Path(__file__).resolve().parents[1] / "app/webui/templates/addin_compose.html"


def _quelle() -> str:
    return ADDIN.read_text(encoding="utf-8")


def _funktion(q: str, name: str) -> str:
    """Quelltext einer Funktion bis zur nächsten auf gleicher Einrückung."""
    i = q.index(f"    function {name}(")
    j = q.find("\n    function ", i + 1)
    return q[i:j if j != -1 else len(q)]


def _region(body: str):
    """`_findSigRegion` aus der Vorlage in Node ausführen — die echte Funktion,
    nicht eine Nachbildung."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node fehlt (in der CI vorhanden)")
    q = _quelle()
    js = ("var _MARKER_START = '<!-- exo-sig-start -->';\n"
          "var _MARKER_END = '<!-- exo-sig-end -->';\n"
          + _funktion(q, "_matchCloseDiv") + "\n" + _funktion(q, "_findSigRegion")
          + f"\nvar b = {json.dumps(body)};"
          + "\nprocess.stdout.write(JSON.stringify(_findSigRegion(b, b.length)));")
    out = subprocess.run([node, "-e", js], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


SIG = '<p>Alexander Zarenko - IT Consulting</p>'


@pytest.mark.parametrize("oeffner", [
    '<div id=exo-sig-s>',                              # Classic, so gemessen
    '<div id="exo-sig-s" class="exo-gateway-sig">',
    "<div id='exo-sig-s'>",
    '<div id="x_exo-sig-s">',                          # OWA
    '<div class=MsoNormal id=x_exo-sig-s>',
])
def test_signatur_wird_auch_ohne_anfuehrungszeichen_erkannt(oeffner):
    body = '<div class=WordSection1><p>Mein Text bleibt?</p>' + oeffner + SIG + '</div></div>'
    r = _region(body)
    assert r is not None, f"nicht erkannt: {oeffner}"
    assert body[r["start"]:r["end"]] == oeffner + SIG + "</div>"
    assert "Mein Text bleibt?" in body[:r["start"]]


@pytest.mark.parametrize("fremd", [
    '<div id=exo-sig-sX>', '<div id="exo-sig-start">', '<div data-id=exo-sig-s>',
])
def test_aehnliche_kennungen_zaehlen_nicht(fremd):
    assert _region('<p>Text</p>' + fremd + SIG + '</div>') is None


def test_laden_fuegt_nicht_selbst_ein():
    rumpf = _funktion(_quelle(), "loadSig")
    assert "if (onLoaded) onLoaded();" in rumpf
    assert "else" not in rumpf.split("if (onLoaded) onLoaded();")[1].split("\n")[0]
    for verboten in ("setAsync", "replaceSig(", "_autoInsert"):
        assert verboten not in rumpf, f"loadSig ruft {verboten}"


def test_nie_den_ganzen_text_durch_die_signatur_ersetzen():
    q = _quelle()
    import re
    assert not re.search(r"setAsync\(\s*_markedSig", q), "Signatur ersetzt den ganzen Text"
    assert "_lastSetBody" not in q, "Rückgriff auf früher gesetzten Text ist zurück"


def test_einfuegen_nutzt_den_signaturbereich_vor_dem_textumbau():
    rumpf = _funktion(_quelle(), "replaceSig")
    assert "if (_signaturbereich()) {" in rumpf
    i_sig = rumpf.index("setSignatureAsync(_markedSig")
    assert i_sig < rumpf.index("body.getAsync("), (
        "Der Signaturbereich muss der erste Weg sein; der Textumbau ist nur der "
        "Rückfall für Clients ohne Mailbox 1.10.")
    zweig = rumpf[rumpf.index("if (_signaturbereich()) {"):rumpf.index("body.getAsync(")]
    assert "_ersetztGerade = false;" in zweig and "return;" in zweig
    assert "isSetSupported('Mailbox', '1.10')" in _funktion(_quelle(), "_signaturbereich")
