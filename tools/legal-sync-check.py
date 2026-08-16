#!/usr/bin/env python3
"""Prüft, ob die Rechtsdokumente in Gateway und Hub identisch sind.

Hintergrund: Die Rechtstexte liegen doppelt — im Gateway-Repo (Anzeige in der
Oberfläche, Versionierung, Hash-Bindung der Zustimmung) und im Hub-Repo
(öffentliche Auslieferung unter /legal/…). Die Verträge verweisen auf die
Hub-Adresse, das Gateway zeigt denselben Text. Laufen die Kopien auseinander,
widersprechen sich zwei Dokumente, die identisch sein sollen — genau die
Fehlerklasse, die dieses Projekt schon zweimal getroffen hat.

⚠️ Die Liste der Dokumente wird NICHT gepflegt, sondern aus
`app/legal_consent.py` (`CURRENT_DOCUMENTS`) abgeleitet. Eine handgepflegte
zweite Liste wäre genau die Streuung, die dieses Skript verhindern soll: Bei
einer neuen Dokumentversion hätte man den Dateinamen an zwei Stellen ändern
müssen, und die vergessene Stelle wäre stillschweigend beim alten Stand
geblieben.

Nebenprodukt: `legal/index.json` — dieselbe Registry in maschinenlesbarer Form.
Der Hub liest sie zur Laufzeit und braucht deshalb keine eigene Zuordnung von
Kennung zu Dateiname; eine Versionserhöhung wirkt dort ohne Codeänderung.

Aufruf aus dem Gateway-Repo:
    python3 tools/legal-sync-check.py
    python3 tools/legal-sync-check.py --fix     # Hub-Kopie und index.json schreiben

Exit-Code 1 bei Abweichung (CI-tauglich).
"""
import ast
import hashlib
import json
import shutil
import sys
from pathlib import Path

GATEWAY = Path(__file__).resolve().parent.parent
HUB = GATEWAY.parent / "sig-provider"

INDEX_REL = "legal/index.json"

# Kurzadressen, die aus Bestandsschutz erhalten bleiben: Die Datenschutzerklärung
# lag vor der Umstellung unter /datenschutz, und laufende Verträge verweisen
# genau dorthin. Eine tote Vertragsadresse wäre ein echter Mangel.
ALIASES = {"datenschutz": "product-privacy"}


def registry() -> dict:
    """CURRENT_DOCUMENTS aus dem Quelltext lesen — ohne Import.

    `import legal_consent` zöge `config` nach und damit Umgebungsannahmen, die
    außerhalb des Containers nicht gelten. Die Registry ist ein reines Literal;
    sie lässt sich gefahrlos aus dem Syntaxbaum holen.
    """
    quelle = (GATEWAY / "app" / "legal_consent.py").read_text(encoding="utf-8")
    for knoten in ast.parse(quelle).body:
        ziele = getattr(knoten, "targets", []) or [getattr(knoten, "target", None)]
        for ziel in ziele:
            if isinstance(ziel, ast.Name) and ziel.id == "CURRENT_DOCUMENTS":
                return ast.literal_eval(knoten.value)
    raise SystemExit("!! CURRENT_DOCUMENTS in app/legal_consent.py nicht gefunden")


def index_bauen(docs: dict) -> str:
    """Registry → index.json (stabil sortiert, damit Diffs lesbar bleiben)."""
    daten = {
        "documents": {
            kennung: {
                "version": d["version"],
                "de": {"path": d["path_de"], "title": d["label_de"]},
                "en": {"path": d["path_en"], "title": d["label_en"]},
            }
            for kennung, d in sorted(docs.items())
        },
        "aliases": ALIASES,
    }
    return json.dumps(daten, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main(argv: list[str]) -> int:
    fix = "--fix" in argv
    docs = registry()

    paare = [(INDEX_REL, INDEX_REL)]
    for d in docs.values():
        for schluessel in ("path_de", "path_en"):
            rel = f"legal/{d[schluessel]}"
            paare.append((rel, rel))

    # index.json ist erzeugt, nicht geschrieben — vor dem Vergleich auffrischen.
    soll = index_bauen(docs)
    index = GATEWAY / INDEX_REL
    if not index.exists() or index.read_text(encoding="utf-8") != soll:
        if fix:
            index.write_text(soll, encoding="utf-8")
            print(f"++ erzeugt: {INDEX_REL}")
        else:
            print(f"!! {INDEX_REL} ist nicht auf dem Stand von CURRENT_DOCUMENTS"
                  f"  → mit --fix erzeugen")
            return 1

    if not HUB.is_dir():
        print(f"!! Hub-Repo nicht gefunden: {HUB}")
        return 2

    probleme = 0
    for gw_rel, hub_rel in paare:
        gw, hub = GATEWAY / gw_rel, HUB / hub_rel
        if not gw.exists():
            print(f"!! fehlt im Gateway: {gw_rel}")
            probleme += 1
            continue
        if not hub.exists():
            if fix:
                hub.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(gw, hub)
                print(f"++ in den Hub kopiert: {hub_rel}")
            else:
                print(f"!! fehlt im Hub: {hub_rel}")
                probleme += 1
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
            probleme += 1

    print(f"\n{len(paare)} Datei(en) geprüft, {probleme} Abweichung(en).")
    return 1 if probleme else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
