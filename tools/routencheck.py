#!/usr/bin/env python3
"""routencheck — findet API-Endpunkte, die in der Oberfläche niemand aufruft.

ANLASS (2026-08-24)
-------------------
Der Auftrag lautete: *„Jedes Stück Code soll seine Existenzberechtigung durch
ein Feature haben, das im UI repräsentiert ist. Nichts soll unter der Oberfläche
schlummern."* — und zwar vorwärts UND rückwärts.

Vorwärts ist abgedeckt: `jsscopecheck.js` findet Bedienelemente, die eine
Funktion rufen, die es nicht gibt. Rückwärts gab es nichts. `deadcheck.py`
prüft ausdrücklich nur JavaScript; ein Endpunkt sieht durch seinen Dekorator
immer benutzt aus.

Der erste Lauf fand vier tote Endpunkte, darunter einen (`/api/system/update/
whats-new`), der dieselbe Changelog-Auswertung ein zweites Mal enthielt —
mitsamt eigenem Versionsvergleich, der von dem in `update_core.py` abweichen
konnte. Die Oberfläche benutzte längst die andere Fassung.

⚠️ DIE PRÄFIX-FALLE — in beide Richtungen
-----------------------------------------
Aufrufe werden zur Laufzeit zusammengesetzt:

    fetch('/api/setup/verify/' + type)

Eine Suche nach `/api/setup/verify/azure` findet nichts, obwohl der Endpunkt
bedient wird. Beim ersten Anlauf waren **vier von zehn Funden** genau das —
Fehlalarm. Ein Werkzeug, dessen Meldungen man wegdrückt, hört man auch dann
nicht mehr, wenn es recht hat.

Der Gegenfehler kam prompt: Der zweite Entwurf liess deshalb JEDEN Präfixtreffer
als „zu prüfen" durchgehen, ohne den Rückgabewert zu setzen. Die Gegenprobe
zeigte, was das wert war — ein wieder eingebautes, totes `/api/logs/files` blieb
unbeanstandet, weil daneben `/api/logs/search` bedient wird. Also genau der
häufige Fall: ein toter Endpunkt neben lebenden Geschwistern.

Deshalb gilt jetzt: Ein Präfix entlastet **nur**, wenn dort auch zusammengesetzt
wird (`'…/' + x` oder `` `…/${x}` ``). `/api/logs/search` beweist über
`/api/logs/files` nichts. Zwei Ausgänge, keine Grauzone — was nicht belegt ist,
ist ein Fund und muss angeschlossen, entfernt oder in ACCEPTED begründet werden.
"""
import ast
import re
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
WEBUI = WURZEL / "app" / "webui"

# Endpunkte ohne Aufrufer in der Oberfläche, die es trotzdem geben soll.
# ⚠️ Mit Begründung, sonst ist es keine Ausnahme, sondern ein vergessener Rest.
ACCEPTED: dict[str, str] = {
    "/api/addin/manifest.xml":
        "Outlook lädt das Manifest selbst, nicht die Oberfläche.",
    "/api/addin/commands.html":
        "Wird vom Add-in im Outlook-Client geladen.",
    "/api/addin/taskpane.html":
        "Wird vom Add-in im Outlook-Client geladen.",
    "/api/watchdog/heartbeat":
        "Der externe Bypass-Wächter ruft ihn auf (Token im Kopffeld), nicht die Oberfläche.",
    "/api/watchdog/status":
        "Dashboard-/Wizard-Kachel folgt in der nächsten Phase des Bypass-Wächters; Endpunkt steht bereit.",
    "/api/watchdog/token/rotate":
        "Wizard-Schritt folgt in der nächsten Phase des Bypass-Wächters; Endpunkt steht bereit.",
}


def routen() -> list[tuple[str, str, str, str]]:
    """(VERB, Pfad, Datei, Funktionsname) aller API-Endpunkte."""
    aus = []
    for pfad in WEBUI.rglob("*.py"):
        quelle = pfad.read_text("utf-8")
        try:
            baum = ast.parse(quelle)
        except SyntaxError:
            continue
        for n in ast.walk(baum):
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for d in n.decorator_list:
                if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                        and d.func.attr in ("get", "post", "put", "delete", "patch")
                        and d.args and isinstance(d.args[0], ast.Constant)
                        and str(d.args[0].value).startswith("/api/")):
                    aus.append((d.func.attr.upper(), d.args[0].value,
                                pfad.name, n.name))
    return aus


def oberflaeche() -> str:
    teile = [p.read_text("utf-8") for p in (WEBUI / "templates").glob("*.html")]
    teile += [p.read_text("utf-8") for p in (WEBUI / "static").glob("*.js")]
    return "\n".join(teile)


def _wird_zusammengesetzt(praefix: str, text: str) -> bool:
    """Endet ein Zeichenkettenliteral auf diesem Präfix und geht dann weiter?

    Trifft `fetch('/api/setup/verify/' + type)` und
    `` fetch(`/api/smime/keyvault/migrate/${email}`) `` — beides sind Aufrufe,
    die eine reine Textsuche nach dem vollen Pfad nie findet. Ohne diese
    Erkennung meldete der erste Entwurf vier bediente Endpunkte als verdächtig,
    und sechs Dauermeldungen liest nach einer Woche niemand mehr.
    """
    p = re.escape(praefix)
    return bool(
        re.search(rf"""{p}/?['"`]\s*\+""", text)      # '…/' + variable
        or re.search(rf"{p}/?\$\{{", text)            # `…/${variable}`
    )


def einordnen(route: str, text: str) -> tuple[str, str]:
    """('ok'|'fund', gefundenes Präfix).

    ⚠️ Ein blosser Präfixtreffer entlastet NICHT. Der erste Entwurf liess ihn
    als „zu prüfen" durchgehen, ohne den Rückgabewert zu setzen — und die
    Gegenprobe zeigte, was das wert ist: Ein wieder eingebautes, totes
    `/api/logs/files` blieb unbeanstandet, weil das Geschwister
    `/api/logs/search` bedient wird. Genau der häufige Fall wäre durchgerutscht.

    Entlastend ist ein Präfix nur, wenn dort auch zusammengesetzt wird. Sonst
    beweist `/api/logs/search` über `/api/logs/files` nichts.

    """
    stamm = re.split(r"\{[^}]*\}", route)[0].rstrip("/")
    if stamm and stamm in text:
        return "ok", stamm
    teile = stamm.strip("/").split("/")
    for i in range(len(teile) - 1, 1, -1):     # längstes Präfix zuerst
        p = "/" + "/".join(teile[:i])
        if p not in text:
            continue
        if _wird_zusammengesetzt(p, text):
            return "ok", p
        return "fund", p                       # Geschwister sind kein Beleg
    return "fund", ""


def main() -> int:
    text = oberflaeche()
    alle = routen()
    funde = []
    for verb, route, datei, fn in sorted(alle, key=lambda x: x[1]):
        if route in ACCEPTED:
            continue
        art, praefix = einordnen(route, text)
        if art == "fund":
            funde.append((verb, route, datei, fn, praefix))

    if funde:
        print(f"FUND ({len(funde)}) — die Oberfläche ruft das nirgends auf:\n")
        for verb, route, datei, fn, praefix in funde:
            nachbar = (f"  (nur '{praefix}' kommt vor — ein bedientes "
                       "Geschwister ist kein Beleg)") if praefix else ""
            print(f"  !! {verb:<6} {route:<44} ({datei}::{fn}){nachbar}")
        print("\nEntweder an ein Bedienelement anschließen, entfernen, oder mit "
              "Begründung nach ACCEPTED in diesem Skript eintragen.")
        return 1

    print(f"{len(alle)} API-Endpunkte geprüft, {len(ACCEPTED)} begründete "
          f"Ausnahmen, 0 ohne Aufrufer.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
