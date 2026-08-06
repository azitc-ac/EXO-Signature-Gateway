"""Feldnahe Meldungen zeigen auf Felder, die es gibt.

`fieldMsg(el, …)` und `fieldClear(el)` beginnen beide mit

    if (typeof el === 'string') el = document.getElementById(el);
    if (!el) return;

Ein Tippfehler in der Kennung bleibt damit vollkommen stumm: Die Rüge wird
nicht angezeigt, die Funktion kehrt zurück, der Vorgang bricht ohne jede
Begründung ab. Kein Syntaxfehler, kein ReferenceError, nichts im Protokoll —
weder `jscheck` noch `jsscopecheck` können das sehen, denn der Aufruf ist
einwandfrei, nur sein Argument geht ins Leere.

Geprüft werden nur Zeichenketten-Argumente. Wo eine Variable übergeben wird,
ist die Kennung zur Prüfzeit unbekannt.
"""
import re
from pathlib import Path

VORLAGEN = Path(__file__).resolve().parents[1] / "app" / "webui" / "templates"

AUFRUF = re.compile(r"\bfield(?:Msg|Clear)\(\s*(['\"])([A-Za-z0-9_\-]+)\1")


def _kennungen(text: str) -> set[str]:
    return set(re.findall(r"""\bid\s*=\s*["']([A-Za-z0-9_\-]+)["']""", text))


def test_jede_feldmeldung_trifft_ein_vorhandenes_feld():
    fehler = []
    geprueft = 0
    for f in sorted(VORLAGEN.glob("*.html")):
        text = f.read_text(encoding="utf-8")
        vorhanden = _kennungen(text)
        for m in AUFRUF.finditer(text):
            geprueft += 1
            kennung = m.group(2)
            if kennung not in vorhanden:
                zeile = text.count("\n", 0, m.start()) + 1
                fehler.append(f"{f.name}:{zeile} — fieldMsg/fieldClear auf "
                              f"'{kennung}', das es in dieser Vorlage nicht gibt")
    assert geprueft > 0, "keine Aufrufe gefunden — sucht der Test noch das Richtige?"
    assert not fehler, "\n".join(fehler)


def test_jede_ruege_wird_auch_wieder_geloescht():
    """Wer fieldMsg ruft, muss die Meldung beim nächsten Versuch löschen.

    Sonst bleibt die rote Rüge am Feld stehen, obwohl die Eingabe inzwischen
    stimmt — und behauptet einen Fehler, den es nicht mehr gibt.
    """
    fehler = []
    for f in sorted(VORLAGEN.glob("*.html")):
        text = f.read_text(encoding="utf-8")
        gerügt = {m.group(2) for m in AUFRUF.finditer(text) if "Msg" in m.group(0)}
        geleert = {m.group(2) for m in AUFRUF.finditer(text) if "Clear" in m.group(0)}
        for kennung in sorted(gerügt - geleert):
            fehler.append(f"{f.name}: '{kennung}' wird gerügt, aber nie geleert")
    assert not fehler, "\n".join(fehler)
