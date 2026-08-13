"""Gemeinsame Helfer der Testsuite.

WARUM ES DIESE DATEI GIBT
-------------------------
Einige Prüfungen lesen den QUELLTEXT der Weboberfläche, statt die Anwendung zu
importieren — etwa „bewegt dieser Endpunkt Geld, ruft er auch `_zahlweg_gate()`
auf?". Solche Prüfungen sind wertvoll, weil sie eine Absicht festhalten, die
sich zur Laufzeit nicht abfragen lässt.

Sie haben aber eine Sollbruchstelle: `app/webui/app.py` wird seit dem
09.08.2026 in Routenmodule aufgeteilt. Jede Gruppe, die herauswandert, entzieht
sich einer Prüfung, die nur diese eine Datei liest. Am 11.08. ist genau das
passiert — `driftcheck` verlor seine Wirkung, als die Einstellungen nach
`routen/settings.py` zogen, und blieb dabei grün.

Deshalb liest hier niemand mehr eine einzelne Datei: `webui_quelltext()`
liefert die Oberfläche vollständig. `tests/test_routes.py` setzt das durch —
wer `app/webui/app.py` wieder fest verdrahtet, fällt dort auf.

⚠️ Prüfungen, die den Quelltext lesen, müssen BEIDE Dekorator-Formen kennen:
`@app.get(…)` in `app.py` und `@router.get(…)` in den Routenmodulen. Ein
Muster, das nur `@app\\.` sucht, findet eine ausgelagerte Gruppe nicht mehr —
und meldet dann „Endpunkt nicht gefunden" statt „Endpunkt ist ungeschützt".
"""
from __future__ import annotations

import re
from pathlib import Path

WEBUI = Path(__file__).resolve().parent.parent / "app" / "webui"

# Beide Formen: `@app.post("…")` in app.py, `@router.post("…")` im Routenmodul.
DEKORATOR = r"@(?:app|router)\."


def webui_quellen() -> list[Path]:
    """Alle Quelldateien der Oberfläche — `app.py`, Fundament, Routenmodule."""
    dateien = [WEBUI / "app.py", WEBUI / "deps.py", WEBUI / "hilfen.py"]
    dateien += sorted((WEBUI / "routen").glob("*.py"))
    return [d for d in dateien if d.is_file()]


def webui_quelltext() -> str:
    """Der Quelltext der gesamten Oberfläche, aneinandergehängt."""
    return "\n".join(d.read_text(encoding="utf-8") for d in webui_quellen())


def endpunkt_block(quelltext: str, methode: str, pfad: str) -> str | None:
    """Der Rumpf eines Endpunkts — vom Dekorator bis zum nächsten Dekorator.

    Ende ist `\\n@`, nicht `\\n@app.`: Der Quelltext ist aus mehreren Dateien
    zusammengesetzt, und der letzte Endpunkt einer Datei liefe sonst in die
    nächste hinein. Ein zu weiter Block macht die Prüfung nicht laut, sondern
    LEISE — er könnte den gesuchten Aufruf im Nachbarendpunkt finden.
    """
    treffer = re.search(
        rf'{DEKORATOR}{re.escape(methode)}\("{re.escape(pfad)}"\)(.*?)(?=\n@|\Z)',
        quelltext, re.S)
    return treffer.group(1) if treffer else None
