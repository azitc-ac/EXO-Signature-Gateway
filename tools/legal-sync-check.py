#!/usr/bin/env python3
"""Prüft, ob die Rechtsdokumente in Gateway und Hub identisch sind.

Hintergrund: Die Datenschutzerklärung liegt doppelt — im Gateway-Repo (Anzeige
in der Oberfläche, Versionierung, Hash-Bindung) und im Hub-Repo (öffentliche
Auslieferung unter /datenschutz). Die Verträge verweisen auf die Hub-URL, das
Gateway zeigt denselben Text. Laufen die Kopien auseinander, widersprechen sich
zwei Dokumente, die identisch sein sollen — genau die Fehlerklasse, die dieses
Projekt schon zweimal getroffen hat.

Aufruf aus dem Gateway-Repo:
    python3 tools/legal-sync-check.py
    python3 tools/legal-sync-check.py --fix     # Hub-Kopie überschreiben

Exit-Code 1 bei Abweichung (CI-tauglich).
"""
import hashlib
import shutil
import sys
from pathlib import Path

GATEWAY = Path(__file__).resolve().parent.parent
HUB = GATEWAY.parent / "sig-provider"

# (Pfad im Gateway, Pfad im Hub) — relativ zum jeweiligen Repo-Wurzelverzeichnis
PAIRS = [
    ("legal/de/produkt-datenschutz-v1.0.md", "legal/de/produkt-datenschutz-v1.0.md"),
    ("legal/en/product-privacy-policy-v1.0.md", "legal/en/product-privacy-policy-v1.0.md"),
]


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main(argv: list[str]) -> int:
    fix = "--fix" in argv
    if not HUB.is_dir():
        print(f"!! Hub-Repo nicht gefunden: {HUB}")
        return 2
    problems = 0
    for gw_rel, hub_rel in PAIRS:
        gw, hub = GATEWAY / gw_rel, HUB / hub_rel
        if not gw.exists():
            print(f"!! fehlt im Gateway: {gw_rel}")
            problems += 1
            continue
        if not hub.exists():
            if fix:
                hub.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(gw, hub)
                print(f"++ in den Hub kopiert: {hub_rel}")
            else:
                print(f"!! fehlt im Hub: {hub_rel}")
                problems += 1
            continue
        a, b = _sha(gw), _sha(hub)
        if a == b:
            print(f"ok  {gw_rel}  ({a[:12]})")
        elif fix:
            shutil.copy2(gw, hub)
            print(f"++ Hub-Kopie aktualisiert: {hub_rel}")
        else:
            print(f"!!  {gw_rel}")
            print(f"      Gateway {a[:12]} != Hub {b[:12]}  → mit --fix angleichen")
            problems += 1
    print(f"\n{problems} Abweichung(en).")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
