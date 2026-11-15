#!/usr/bin/env python3
"""Die statischen Prüfungen der CI lokal — gelesen aus .github/workflows/ci.yml.

Warum aus der CI-Datei und nicht als eigene Liste: Eine abgeschriebene Liste
veraltet. Vor einem Push lief nachweislich nur ein Teil der Prüfungen
(tabellencheck, cssklassencheck, routencheck und begriffecheck fehlten), und
die CI meldete erst danach rot.

Genommen werden einzeilige `run: python tools/…` bzw. `run: node tools/…`.
Mehrzeilige Schritte (pytest, Abhängigkeiten, Drift mit Fremd-Repo) laufen
hier nicht — pytest gehört in den Container, nicht auf den Entwicklungsrechner.

    python3 tools/ci_lokal.py            # alle
    NODE_PATH=…/node_modules python3 tools/ci_lokal.py   # wenn acorn woanders liegt
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
CI = WURZEL / ".github" / "workflows" / "ci.yml"


def schritte() -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"^\s*run:\s*((?:python|node) tools/\S.*)$",
                                                     CI.read_text("utf-8"), re.M)]


def main() -> int:
    rot = 0
    liste = schritte()
    for befehl in liste:
        teile = befehl.split()
        if teile[0] == "python":
            teile[0] = sys.executable
        try:
            erg = subprocess.run(teile, cwd=WURZEL, capture_output=True, text=True, timeout=300)
            ok, ausgabe = erg.returncode == 0, erg.stdout + erg.stderr
        except subprocess.TimeoutExpired:
            ok, ausgabe = False, "Zeitüberschreitung (300 s)"
        print(("OK   " if ok else "ROT  ") + " ".join(befehl.split()[:2]))
        if not ok:
            rot += 1
            print("\n".join("     " + z for z in ausgabe.strip().splitlines()[-15:]))
    print(f"{len(liste) - rot} von {len(liste)} Prüfungen grün")
    return 1 if rot or not liste else 0


if __name__ == "__main__":
    sys.exit(main())
