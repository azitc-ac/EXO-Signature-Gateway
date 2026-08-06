#!/usr/bin/env python3
"""deadcheck — findet Funktionen im gemeinsamen JavaScript, die niemand ruft.

ANLASS (2026-08-06)
-------------------
`setState()` stand in `common.js` beider Anwendungen: definiert, mit
Erklärung versehen, gespiegelt, von `driftcheck` auf Gleichheit überwacht — und
nirgends aufgerufen, nicht einmal innerhalb derselben Datei. `showMsg()` setzt
`dataset.state` selbst.

Die vorhandenen Prüfungen konnten das nicht sehen, weil sie alle die
GEGENRICHTUNG suchen: `jsscopecheck.js` findet Bezeichner ohne Deklaration
(ReferenceError zur Laufzeit), `jscheck.py` Syntaxfehler. Eine Deklaration ohne
Verwendung ist für beide vollkommen unauffällig — sie ist ja gültiges,
lauffähiges JavaScript.

Im Changelog stehen fünf einzeln von Hand gefundene Fälle dieser Art
(„verwaiste Funktion entfernt", „war dort toter Code"). Fünf Befunde ohne
Durchsetzung; genau davor warnt CLAUDE.md Regel 4.

WAS GEMELDET WIRD
-----------------
Eine Funktion gilt als unbenutzt, wenn ihr Name außerhalb der eigenen
Definitionszeile in KEINER geprüften Datei vorkommt — weder in einer Vorlage
noch in einer anderen Skriptdatei noch in derselben Datei. Das ist bewusst die
strengste Lesart mit der geringsten Falschmeldungsrate: Ein einziger Verweis
irgendwo genügt, um als benutzt zu gelten.

GRENZEN (bewusst in Kauf genommen)
----------------------------------
Die Suche ist eine Textsuche, keine Programmanalyse. Steht ein Funktionsname
nur in einem Kommentar, gilt er als benutzt — der Fund bleibt aus. Ein
AST-Lauf über alle Vorlagen wäre genauer, aber Vorlagen enthalten Jinja und
sind nicht ohne Weiteres zu parsen; `jsscopecheck.js` löst das nur für die
Skriptblöcke.

Die Richtung ist absichtlich so gewählt: lieber einen Fund verpassen als einen
falschen melden. Ein Prüfwerkzeug, dessen Meldungen man wegdrückt, hört man
auch dann nicht mehr, wenn es recht hat.

WAS NICHT GEMELDET WIRD
-----------------------
Eine Funktion, die zu selten gerufen wird — etwa nur auf einer von siebzehn
Seiten, obwohl die Regel überall gelten soll. Dieser Fall (initHintClamps,
v1.7.96 bis v1.7.152) ist hiermit NICHT zu finden: Ein Aufruf ist ein Aufruf.
Wo eine Sollmenge gilt, muss ein Test sie festhalten — siehe
`tests/test_erklaertexte_gekuerzt.py`.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Bewusste Ausnahmen: Name → Begründung. Wer hier etwas einträgt, muss sagen,
# warum die Funktion trotz fehlender Verwendung bleiben soll.
#
# Erwarteter Fall: eine gespiegelte Funktion, die AUSSCHLIESSLICH der Hub
# benutzt. Der Gateway-Lauf in der CI hat das Hub-Repository nicht vor sich
# (privat, nicht ausgecheckt) und meldet sie deshalb — zu Recht aus seiner
# Sicht, zu Unrecht in der Sache. Solche Namen gehören hierher, mit Verweis auf
# die Verwendungsstelle im Hub. Der lokale Lauf über beide Bäume erkennt sie
# von selbst und braucht den Eintrag nicht.
ACCEPTED: dict[str, str] = {}

DEF = re.compile(r"^\s*function\s+([A-Za-z_$][\w$]*)\s*\(", re.M)


def _dateien(wurzel: Path) -> tuple[list[Path], list[Path]]:
    """(Skripte, in denen definiert wird; alle Dateien, die verwenden können)"""
    statisch = wurzel / "app" / "webui" / "static"
    vorlagen = wurzel / "app" / "webui" / "templates"
    skripte = sorted(statisch.glob("*.js")) if statisch.is_dir() else []
    nutzer = skripte + (sorted(vorlagen.glob("*.html")) if vorlagen.is_dir() else [])
    return skripte, nutzer


def pruefe(wurzel: Path, name: str, fremde: dict[Path, str] | None = None) -> list[str]:
    """fremde: Dateien der jeweils ANDEREN Anwendung.

    `common.js` ist in beiden Anwendungen inhaltsgleich zu halten
    (`driftcheck` erzwingt das). Eine Funktion, die nur das Gateway braucht,
    steht deshalb zwangsläufig auch im Hub herum — dort ist sie kein toter
    Code, sondern der Preis der Spiegelung. Gemeldet wird nur, was in KEINER
    der beiden Anwendungen jemand verwendet.
    """
    skripte, nutzer = _dateien(wurzel)
    if not skripte:
        return []
    inhalte = {f: f.read_text(encoding="utf-8") for f in nutzer}
    inhalte.update(fremde or {})
    befunde = []
    for skript in skripte:
        text = inhalte[skript]
        for m in DEF.finditer(text):
            fn = m.group(1)
            if fn in ACCEPTED:
                continue
            # Alle Vorkommen des Namens zählen, die eigene Definitionszeile
            # ausgenommen. Ein Treffer irgendwo genügt als Verwendung.
            wort = re.compile(r"\b" + re.escape(fn) + r"\b")
            # Jede Definition dieses Namens ist keine Verwendung — auch die in
            # der gespiegelten Fassung der anderen Anwendung nicht. Ohne diese
            # Ausnahme hielte sich jede gespiegelte Funktion selbst am Leben.
            eigene_def = re.compile(r"function\s+" + re.escape(fn) + r"\s*\(")
            zeile_der_def = text.count("\n", 0, m.start())
            benutzt = False
            for f, inhalt in inhalte.items():
                for t in wort.finditer(inhalt):
                    zeilenanfang = inhalt.rfind("\n", 0, t.start()) + 1
                    zeilenende = inhalt.find("\n", t.start())
                    zeile = inhalt[zeilenanfang:zeilenende if zeilenende >= 0 else None]
                    if eigene_def.search(zeile):
                        continue
                    benutzt = True
                    break
                if benutzt:
                    break
            if not benutzt:
                befunde.append(
                    f"  {name}: {fn}() in {skript.name}:{zeile_der_def + 1} "
                    f"wird nirgends verwendet")
    return befunde


def main() -> int:
    hier = Path(__file__).resolve().parents[1]
    baeume = [(hier, "Gateway")]
    hub = hier.parent / "sig-provider"
    if "--gateway-only" not in sys.argv and hub.is_dir():
        baeume.append((hub, "Hub"))

    alle = []
    for wurzel, name in baeume:
        fremde: dict[Path, str] = {}
        for andere, _ in baeume:
            if andere == wurzel:
                continue
            for f in _dateien(andere)[1]:
                fremde[f] = f.read_text(encoding="utf-8")
        alle += pruefe(wurzel, name, fremde)

    if alle:
        print("Unbenutzte Funktionen im gemeinsamen JavaScript:")
        print("\n".join(alle))
        print(f"\n{len(alle)} Befund(e). Entfernen — oder mit Begründung in "
              f"ACCEPTED eintragen ({Path(__file__).name}).")
        return 1
    geprueft = ", ".join(n for _, n in baeume)
    print(f"\nKeine unbenutzten Funktionen ({geprueft}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
