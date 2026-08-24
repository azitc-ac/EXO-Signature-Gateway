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

⚠️ ZWEI ARTEN, IN DENEN EIN ALTNAME AUFTAUCHT — und nur eine ist ein Fehler
---------------------------------------------------------------------------
Der erste Lauf meldete 12 Stellen. **Elf davon waren richtig**, und das ist
kein Zufall, sondern der Regelfall:

    if s.REINJECT_MODE in ('imap','smtp587')     ← MUSS so sein: Bestandsanlagen
                                                   tragen den Altnamen in ihrer
                                                   settings.json
    „Der Altname `smtp587` bezeichnet denselben   ← erklärt ihn gerade
     Modus."
    „einen 587-Modus gibt es nicht"               ← widerlegt ihn gerade

Ein Werkzeug, das elf richtige Stellen anmahnt, um eine falsche zu finden, wird
weggedrückt — dann hört man es auch, wenn es recht hat. Deshalb gelten zwei
Ausnahmen, beide unten im Code umgesetzt:

  * **Vergleich im Code** (`in (…)`, `== "…"`): Abwärtskompatibilität, nie ein
    Sprachfehler.
  * **Erklärende Umgebung**: steht in den Zeilen ringsum ein Signalwort
    (Altname, veraltet, „gibt es nicht" …), erklärt die Stelle den Begriff,
    statt ihn zu verwenden.

Nach dieser Verfeinerung blieb von zwölf Meldungen genau eine übrig — und die
war ein echter Fehler („Graph-only-Modus" in setup.html).

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
            "test_begriffe.py": "prüft diese Regel und zitiert dafür die "
                                "verbotenen Varianten",
        },
    ),
    Begriff(
        name="Altname smtp587",
        kanonisch="`imap` (der Altname `smtp587` wird noch angenommen)",
        verboten={
            # Der Altname darf vorkommen — aber nur, wo er auch erklärt oder
            # abwärtskompatibel verglichen wird. Beides entscheidet
            # `_wird_erklaert()` bzw. `_ist_vergleich()`; hier steht bewusst
            # nur das nackte Wort. Ein Lookahead an dieser Stelle sah aus wie
            # dieselbe Prüfung, sah aber nur NACH dem Treffer — die Erklärung
            # steht meist DAVOR (Kommentar über dem Code).
            r"smtp587":
                "`smtp587` ohne Hinweis darauf, dass es ein Altname für "
                "`imap` ist — der Modus macht IMAP APPEND, kein SMTP auf 587.",
        },
        ausnahmen={
            "CHANGELOG.md": "historische Einträge",
            "settings_store.py": "definiert den Altnamen und erklärt ihn im "
                                 "Kommentar darüber",
            "reinject.py": "nimmt den Altnamen entgegen und warnt zur Laufzeit",
            "begriffecheck.py": "beschreibt die Regel selbst",
            "test_begriffe.py": "prüft diese Regel und zitiert dafür die "
                                "verbotenen Varianten",
        },
    ),
    Begriff(
        name="aktiviert statt eingeschaltet",
        kanonisch="„für S/MIME aktiviert“ (ein Merkmal wird aktiviert)",
        verboten={
            # ⚠️ NUR die Fügung „für … eingeschaltet“. Ein SCHALTER ist sehr
            # wohl eingeschaltet — „Standard: eingeschaltet“ ist richtig und
            # darf nicht angemahnt werden. Der Unterschied ist das Objekt:
            # Ein Schalter wird eingeschaltet, ein Merkmal wird aktiviert.
            # Bis zu drei Wörter dazwischen: „für S/MIME eingeschaltet", aber
            # auch „für dieses Postfach nicht eingeschaltet". Die erste Fassung
            # erlaubte nur EINES — die Gegenprobe (Formulierung zurückgebaut)
            # blieb dadurch still, und das Werkzeug hätte den gemeldeten Satz
            # nicht wiedergefunden.
            r"für\s+(?:[A-Za-zÄÖÜäöüß/-]+\s+){1,3}eingeschaltet":
                "Ein Merkmal wird für ein Postfach AKTIVIERT, nicht "
                "eingeschaltet — eingeschaltet ist ein Schalter.",
        },
        ausnahmen={
            "CHANGELOG.md": "historische Einträge bleiben, wie sie geschrieben wurden",
            "begriffecheck.py": "beschreibt die Regel selbst",
            "test_begriffe.py": "prüft diese Regel und zitiert dafür die "
                                "verbotenen Varianten",
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


# Wörter, die anzeigen: Hier wird der Begriff ERKLÄRT, nicht verwendet.
SIGNALE = ("altname", "veraltet", "deprecated", "legacy", "gibt es nicht",
           "hiess frueher", "hieß früher", "frueher hiess", "früher hieß",
           "nicht mehr", "irrefuehrend", "irreführend", "bezeichnet denselben")


def _ist_vergleich(zeile: str, wort: str) -> bool:
    """`mode in ("imap","smtp587")` oder `x == "smtp587"` — Abwärtskompatibilität.

    Solche Stellen MÜSSEN den Altnamen tragen: In der `settings.json` einer
    Bestandsanlage steht er, und wer ihn aus dem Vergleich nimmt, bricht sie.
    Sechs der zwölf Erstmeldungen waren genau das.
    """
    w = re.escape(wort)
    return bool(
        re.search(rf"""\bin\s*[\(\[][^)\]]*['"]{w}['"]""", zeile, re.I)
        or re.search(rf"""[=!]=\s*['"]{w}['"]""", zeile, re.I)
        or re.search(rf"""['"]{w}['"]\s*[,)\]]""", zeile, re.I)   # Listenglied
    )


def _wird_erklaert(zeilen: list[str], nr: int) -> bool:
    """Steht in den Zeilen ringsum eine Erklärung statt einer Verwendung?

    Fenster statt Einzelzeile, weil die Erklärung oft eine Zeile davor oder
    danach steht (Kommentar über dem Code, Fortsetzung eines Satzes).
    """
    fenster = " ".join(zeilen[max(0, nr - 3):nr + 2]).lower()
    return any(s in fenster for s in SIGNALE)


def pruefe(wurzel: Path, name: str) -> list[str]:
    funde = []
    for pfad in dateien(wurzel):
        try:
            text = pfad.read_text("utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        zeilen = text.splitlines()
        for begriff in REGISTER:
            if pfad.name in begriff.ausnahmen:
                continue
            for muster, warum in begriff.verboten.items():
                for treffer in re.finditer(muster, text, re.IGNORECASE):
                    nr = text[:treffer.start()].count("\n") + 1
                    zeile = zeilen[nr - 1] if nr <= len(zeilen) else ""
                    if _ist_vergleich(zeile, treffer.group(0)):
                        continue
                    if _wird_erklaert(zeilen, nr - 1):
                        continue
                    rel = pfad.relative_to(wurzel)
                    funde.append(
                        f"{name}/{rel}:{nr}  „{treffer.group(0)}“\n"
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
