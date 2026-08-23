#!/usr/bin/env python3
"""Ein Begriff, eine Schreibweise — überall.

ANLASS (2026-08-23): Der Nutzer fragte, warum das Projekt „von Port 587
abgekommen" sei. Die Antwort kostete zwei falsche Anläufe und eine
Stunde Messen, weil dasselbe Ding vier Namen trug:

    settings.json      REINJECT_MODE = "smtp587"      (Altname)
    Code               mode in ("imap", "smtp587")
    Oberfläche         „IMAP + Graph"
    Erklärtext         „Im SMTP- bzw. 587-Modus …"    (einen 587-Modus gibt es nicht)

Kein Test schlug an. Alle prüften Verhalten — keiner prüfte, ob das, was
danebensteht, dasselbe meint. Der Nutzer: „Wir haben 100e Tests und dennoch
ist alles Kraut und Rüben."

WAS DIESES SKRIPT TUT
---------------------
Es führt ein Register verbindlicher Begriffe. Zu jedem Begriff gehört:

  * die kanonische Schreibweise,
  * verbotene Varianten (Tippfehler, Altnamen, erfundene Bezeichnungen),
  * wo eine Ausnahme erlaubt ist — mit Begründung.

Geprüft werden Quelltext, Vorlagen, Changelog und alle Textdateien im
Repository. Exit 1 bei Fund.

WAS ES NICHT TUT
----------------
Es prüft keine Aussagen, nur Wörter. Ein Satz kann in kanonischen Begriffen
formuliert und trotzdem falsch sein — dafür braucht es einen Test, der das
behauptete Verhalten nachstellt (siehe tests/test_erklaertexte_stimmen.py).

Aufruf:
    python3 tools/begriffecheck.py
    python3 tools/begriffecheck.py --hub     # auch das Hub-Repo
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

GATEWAY = Path(__file__).resolve().parent.parent
HUB = GATEWAY.parent / "sig-provider"

# Dateiendungen, in denen Begriffe vorkommen können.
ENDUNGEN = {".py", ".html", ".js", ".css", ".md", ".txt", ".yml", ".yaml"}

# Verzeichnisse, die nie geprüft werden.
AUS = {".git", ".claude", "node_modules", ".venv", "__pycache__", "data",
       "tools", "backups", ".pytest_cache", ".github", "worktrees"}


class Begriff:
    """Ein verbindlicher Begriff samt seiner verbotenen Varianten.

    `ausnahmen` bildet Datei → Grund ab. Eine Ausnahme ohne Grund ist keine;
    sie wird beim nächsten Durchsehen niemand mehr einordnen können.
    """

    def __init__(self, name: str, kanonisch: str, verboten: dict[str, str],
                 ausnahmen: dict[str, str] | None = None):
        self.name = name
        self.kanonisch = kanonisch
        self.verboten = verboten            # Muster → warum es falsch ist
        self.ausnahmen = ausnahmen or {}


REGISTER = [
    Begriff(
        name="Rückweg-Modi",
        kanonisch="`smtp` (Port 25) · `graph` · `imap`",
        verboten={
            r"587[- ]Modus": "Einen 587-Modus gibt es nicht. Port 587 ist ein "
                             "Sonderweg innerhalb von `graph` und `imap`.",
            r"Graph[- ]only[- ]Modus": "Der Modus heisst `graph`; „Graph-only“ "
                                       "suggeriert, es liefe ausschliesslich "
                                       "über Graph — 587 kommt dort ebenfalls "
                                       "zum Zug.",
        },
        ausnahmen={
            "CHANGELOG.md": "Historische Einträge bleiben, wie sie geschrieben "
                            "wurden — sie beschreiben den Stand ihres Tages.",
        },
    ),
    Begriff(
        name="Altname smtp587",
        kanonisch="`imap` (der Altname `smtp587` wird noch angenommen)",
        verboten={
            # Der Altname darf vorkommen — aber nur, wo auch erklärt wird,
            # dass es einer ist. Geprüft wird die Nähe zum Wort „Altname“
            # bzw. „legacy“ in derselben oder der vorigen Zeile.
            r"smtp587(?!.{0,200}?(?:Altname|legacy|deprecated|veraltet))":
                "`smtp587` ohne Hinweis darauf, dass es ein Altname für "
                "`imap` ist — der Modus macht IMAP APPEND, kein SMTP auf 587.",
        },
        ausnahmen={
            "CHANGELOG.md": "historische Einträge",
            "settings_store.py": "definiert den Altnamen und erklärt ihn im "
                                 "Kommentar darüber",
            "reinject.py": "nimmt den Altnamen entgegen und warnt zur Laufzeit",
            "begriffecheck.py": "beschreibt die Regel selbst",
        },
    ),
]


def dateien(wurzel: Path):
    for pfad in sorted(wurzel.rglob("*")):
        if not pfad.is_file() or pfad.suffix not in ENDUNGEN:
            continue
        if any(teil in AUS for teil in pfad.parts):
            continue
        yield pfad


def pruefe(wurzel: Path, name: str) -> list[str]:
    funde = []
    for pfad in dateien(wurzel):
        try:
            text = pfad.read_text("utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for begriff in REGISTER:
            if pfad.name in begriff.ausnahmen:
                continue
            for muster, warum in begriff.verboten.items():
                for treffer in re.finditer(muster, text, re.IGNORECASE):
                    zeile = text[:treffer.start()].count("\n") + 1
                    rel = pfad.relative_to(wurzel)
                    funde.append(
                        f"{name}/{rel}:{zeile}  „{treffer.group(0)}“\n"
                        f"      {warum}\n"
                        f"      richtig: {begriff.kanonisch}")
    return funde


def main(argv: list[str]) -> int:
    baeume = [("Gateway", GATEWAY)]
    if "--hub" in argv and HUB.is_dir():
        baeume.append(("Hub", HUB))

    alle = []
    for name, wurzel in baeume:
        funde = pruefe(wurzel, name)
        alle += funde
        print(f"{'!!' if funde else 'ok '} {name}: {len(funde)} Abweichung(en)")

    for f in alle:
        print("   " + f)
    if alle:
        print(f"\n{len(alle)} Stelle(n) benutzen einen Begriff, den es so nicht gibt.")
        print("Entweder die Stelle berichtigen — oder, wenn sie recht hat, das "
              "Register in tools/begriffecheck.py anpassen.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
