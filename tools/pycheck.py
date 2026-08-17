#!/usr/bin/env python3
"""Findet Namen, die zur Laufzeit nicht existieren — in beiden Anwendungen.

Anlass (18.08.2026): Die Zertifikatsbestellung im Hub brach mit HTTP 500 ab:

    File "/app/main.py", line 1239, in cert_order
        if prepaid and price_gross > 0:
    NameError: name 'prepaid' is not defined

Die Variable wurde an zwei Stellen benutzt und nirgends gesetzt. Python merkt
das erst, wenn die Zeile ausgeführt wird — und diese Zeile lief zum ersten Mal,
als ein Kunde ein Zertifikat bestellte. Kein Test schlug an, weil kein Test den
Bestellweg bis dorthin durchläuft.

Das ist die Python-Entsprechung zu `jsscopecheck.js`, der dasselbe für das
JavaScript in den Vorlagen tut (dort waren es zwei `ReferenceError` in einer
Sitzung). Beide Sprachen teilen die Eigenschaft, dass ein Tippfehler in einem
selten begangenen Zweig beliebig lange unentdeckt bleibt.

Aufruf (aus dem Gateway-Repo):
    python3 tools/pycheck.py                # beide Bäume
    python3 tools/pycheck.py --gateway-only # ohne Hub (z.B. in der Gateway-CI)

Exit-Code 1 bei Fund.
"""
import subprocess
import sys
from pathlib import Path

GATEWAY = Path(__file__).resolve().parent.parent
HUB = GATEWAY.parent / "sig-provider"

# Nur diese Meldungsart. pyflakes findet auch unbenutzte Importe und
# Schattierungen — beides ist Geschmackssache und würde die Prüfung so laut
# machen, dass niemand mehr hinsieht. „undefined name" dagegen ist immer ein
# Fehler: Der Name existiert zur Laufzeit nicht.
MUSTER = "undefined name"


def pruefe(wurzel: Path) -> list[str]:
    ziel = wurzel / "app"
    if not ziel.is_dir():
        return []
    try:
        ergebnis = subprocess.run(
            [sys.executable, "-m", "pyflakes", str(ziel)],
            capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        print("!! pyflakes nicht installiert:  pip install pyflakes")
        raise SystemExit(2)
    zeilen = (ergebnis.stdout + ergebnis.stderr).splitlines()
    if any("No module named" in z for z in zeilen):
        print("!! pyflakes nicht installiert:  pip install pyflakes")
        raise SystemExit(2)
    return [z for z in zeilen if MUSTER in z]


def main(argv: list[str]) -> int:
    nur_gateway = "--gateway-only" in argv
    baeume = [("Gateway", GATEWAY)]
    if not nur_gateway:
        if HUB.is_dir():
            baeume.append(("Hub", HUB))
        else:
            print(f"-- Hub-Repo nicht gefunden ({HUB}), übersprungen")

    treffer = 0
    for name, wurzel in baeume:
        funde = pruefe(wurzel)
        for f in funde:
            print(f"!! {name}: {f}")
        treffer += len(funde)
        if not funde:
            print(f"ok  {name}: keine undefinierten Namen")

    if treffer:
        print(f"\n{treffer} Name(n), die zur Laufzeit nicht existieren.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
