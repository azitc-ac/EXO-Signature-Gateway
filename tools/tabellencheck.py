#!/usr/bin/env python3
"""Jede breite Tabelle gehört in einen waagerecht rollbaren Bereich.

ANLASS (2026-08-25)
-------------------
Der Nutzer am Telefon: *„die Tabelle ist nicht scrollbar ich komme auf dem
Handy kaum an die Buttons rechts wenn der Modus aktiv ist"* — die
Warteschlange des Wartungsmodus. Gemessen bei 393px: Der Löschen-Knopf saß
bei x=896 bei einem 393px breiten Fenster, und der Bereich liess sich nicht
schieben. Die Knöpfe waren schlicht nicht erreichbar.

Die Klasse `.tabellen-rollbereich` gab es zu dem Zeitpunkt bereits — genutzt
haben sie **zwei** Vorlagen, beide erst kurz zuvor gebaut. Alle älteren
Tabellen standen daneben. Das ist der Befund „X ist der einzige, der Y macht",
und CLAUDE.md verlangt dafür eine Struktur statt eines Vermerks.

WAS GEPRÜFT WIRD
----------------
Eine Tabelle gilt als breit, wenn ihre Kopfzeile drei oder mehr Spalten hat
oder wenn in ihren Zeilen Knöpfe stehen. Eine solche Tabelle braucht:

  1. einen Elternteil mit `.tabellen-rollbereich` (oder `overflow-x:auto`), und
  2. eine `min-width` an der Tabelle selbst.

Beides zusammen, und das ist der Punkt: Ohne den Bereich läuft der Inhalt aus
dem Bild, ohne die Mindestbreite quetscht `width:100%` alle Spalten in die
Fensterbreite, statt rollen zu lassen — aus Adressen und Betreffzeilen werden
dann Ellipsen, und die Tabelle sagt nichts mehr.

⚠️ WAS NICHT GEMELDET WIRD
Zwei Spalten ohne Knöpfe (Schlüssel/Wert-Aufstellungen wie `.kv-table`) passen
auf 393px. Sie in einen Rollbereich zu zwingen, brächte einen Rollbalken, der
nie gebraucht wird.

Aufruf:
    python3 tools/tabellencheck.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

GATEWAY = Path(__file__).resolve().parent.parent

# Bewusste Ausnahmen — mit Grund. Ohne Grund ist es keine Ausnahme.
ERLAUBT: dict[str, str] = {
    "smime_selfservice.html": "eigenständige Endnutzer-Seite, eigenes Layout",
    "portal.html": "eigenständiges Empfänger-Portal, eigenes Layout",
    "addin_compose.html": "Add-in-Fenster in Outlook, feste schmale Breite",
    "config.html": "reine Textausgabe der Konfiguration, keine Bedienelemente",
}

_TABELLE = re.compile(r"<table\b[^>]*>")
_ROLLBAR = ("tabellen-rollbereich", "overflow-x:auto", "overflow-x: auto",
            "overflow:auto", "overflow: auto")


def _steht_im_rollbereich(text: str, pos: int) -> bool:
    """Steht vor der Tabelle ein öffnender Rollbereich, der noch offen ist?

    Gesucht wird rückwärts im Umfeld — bei per JavaScript zusammengesetzten
    Tabellen steht der Bereich als Zeichenkette unmittelbar davor, bei
    HTML-Tabellen als Element ein oder zwei Zeilen höher.
    """
    fenster = text[max(0, pos - 1200):pos]
    return any(m in fenster for m in _ROLLBAR)


def pruefe(wurzel: Path) -> list[str]:
    meldungen = []
    for datei in sorted((wurzel / "app" / "webui" / "templates").glob("*.html")):
        if datei.name in ERLAUBT:
            continue
        text = datei.read_text("utf-8")
        for m in _TABELLE.finditer(text):
            nr = text[: m.start()].count("\n") + 1
            ende = text.find("</table>", m.end())
            rumpf = text[m.end(): ende if ende > 0 else m.end() + 4000]

            kopf = rumpf[: rumpf.find("</tr>")] if "</tr>" in rumpf else rumpf
            spalten = len(re.findall(r"<t[hd]\b", kopf))
            knoepfe = len(re.findall(r"<button", rumpf))
            if spalten < 3 and not knoepfe:
                continue

            fehlt = []
            if not _steht_im_rollbereich(text, m.start()):
                fehlt.append("kein Rollbereich")
            if "min-width" not in m.group(0):
                fehlt.append("keine min-width")
            if not fehlt:
                continue
            meldungen.append(
                f"   {datei.name}:{nr}  {spalten} Spalten"
                f"{f', {knoepfe} Knöpfe' if knoepfe else ''} — {', '.join(fehlt)}")
    return meldungen


def main() -> int:
    treffer = pruefe(GATEWAY)
    print(("ok  " if not treffer else "!!  ")
          + f"Gateway: {len(treffer)} breite Tabelle(n) ohne Rollbereich")
    if not treffer:
        return 0
    print("\nEine breite Tabelle braucht beides:\n"
          "  <div class=\"tabellen-rollbereich\">\n"
          "    <table style=\"width:100%;min-width:…px;…\">\n"
          "  Ohne Bereich läuft sie aus dem Bild, ohne Mindestbreite quetscht\n"
          "  sie sich zusammen statt zu rollen.\n")
    print("\n".join(treffer))
    return 1


if __name__ == "__main__":
    sys.exit(main())
