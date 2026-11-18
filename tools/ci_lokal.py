#!/usr/bin/env python3
"""Die statischen Prüfungen der CI lokal — gelesen aus .github/workflows/ci.yml.

Warum aus der CI-Datei und nicht als eigene Liste: Eine abgeschriebene Liste
veraltet. Vor einem Push lief nachweislich nur ein Teil der Prüfungen
(tabellencheck, cssklassencheck, routencheck und begriffecheck fehlten), und
die CI meldete erst danach rot.

Genommen werden einzeilige `run: python tools/…` bzw. `run: node tools/…`.
Mehrzeilige Schritte (pytest, Abhängigkeiten, Drift mit Fremd-Repo) laufen
hier nicht — pytest gehört in den Container, nicht auf den Entwicklungsrechner.

Ausnahme NUR_GIT: Tests, die allein Git-Historie und JSON lesen. Im Container
fehlt die Historie — dort scheitern sie immer und gelten als „bekannt“, ein
echter Befund geht darin unter. Am 17.11.2026 so geschehen: Der Kopien-Stand
stieg ohne Begründung, lokal unsichtbar, die CI blieb rot, und weil der Test
JEDES Paar der Historie prüft, half nur das Umschreiben des Commits. Hier
laufen sie ohne pytest (Funktionen `test_*` direkt aufgerufen).

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


NUR_GIT = ["tests/test_kopien_stand.py"]


def nur_git(pfad: str) -> tuple[bool, str]:
    import importlib.util
    spec = importlib.util.spec_from_file_location("nur_git_" + Path(pfad).stem, WURZEL / pfad)
    modul = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(modul)
        for name in sorted(n for n in dir(modul) if n.startswith("test_")):
            getattr(modul, name)()
    except Exception as exc:                                     # noqa: BLE001 — Befund melden, nicht abbrechen
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


def main() -> int:
    rot = 0
    liste = schritte()
    for pfad in NUR_GIT:
        ok, ausgabe = nur_git(pfad)
        print(("OK   " if ok else "ROT  ") + pfad)
        if not ok:
            rot += 1
            print("\n".join("     " + z for z in ausgabe.strip().splitlines()[-15:]))
    liste_alle = len(liste) + len(NUR_GIT)
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
    print(f"{liste_alle - rot} von {liste_alle} Prüfungen grün")
    return 1 if rot or not liste else 0


if __name__ == "__main__":
    sys.exit(main())
