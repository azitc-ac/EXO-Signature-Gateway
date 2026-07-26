#!/usr/bin/env python3
"""driftcheck — findet auseinanderlaufende Umsetzungen derselben Sache.

WARUM
-----
Wiederkehrendes Muster in diesem Projekt: "X ist der einzige, der Y nicht macht."
Elf handgeschriebene HTML-Escaper. Zwei Einstellungsspeicher mit demselben
644-Rechte-Fehler, einmal am 25.07. und einmal am 26.07. behoben. Stripe-Schlüssel
als einziger Zugangsdatensatz nur aus der Umgebung lesbar.

Die zwei Bereiche, die NICHT driften, sind genau die zwei mit einem Prüfskript:
Dark Mode (darkcheck.py) und Rechtstexte (legal-sync-check.py). Eine Regel ohne
Prüfung wird gebrochen; eine Prüfung ohne Regel erklärt nicht, warum. Deshalb
beides: die Regeln stehen in CLAUDE.md, hier ist die Durchsetzung.

AUFRUF
------
    python3 tools/driftcheck.py                 # beide Anwendungen
    python3 tools/driftcheck.py --gateway-only

Rückgabe 1, wenn eine echte Lücke gefunden wurde. Bekannte, bewusst akzeptierte
Fälle stehen in ACCEPTED — mit Begründung, nicht nur mit Namen.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

GATEWAY = Path(__file__).resolve().parent.parent
HUB = GATEWAY.parent / "sig-provider"

# Bewusst akzeptierte Ausnahmen: Datei → Grund. Wer hier etwas einträgt, muss
# den Grund hinschreiben; "später" ist kein Grund.
ACCEPTED: dict[str, str] = {
    "portal.html": "eigenständiges Empfänger-Portal, lädt bewusst kein Gateway-JS "
                   "(fremde Browser, minimale Angriffsfläche)",
    "smime_selfservice.html": "eigenständige Seite ohne gemeinsames JS",
}

# Dateien, die in beiden Anwendungen inhaltsgleich sein MÜSSEN. Der Nutzer hat
# sich bewusst für "geprüfte Kopie" statt git-subtree entschieden: der Deploy-Weg
# (update-watcher.sh) bleibt unangetastet, die Gleichheit wird hier erzwungen.
MIRRORED: list[tuple[str, str]] = [
    ("app/webui/static/common.js", "gemeinsame Frontend-Helfer (esc() usw.)"),
]


class Report:
    def __init__(self) -> None:
        self.problems: list[str] = []
        self.notes: list[str] = []

    def fail(self, msg: str) -> None:
        self.problems.append(msg)

    def note(self, msg: str) -> None:
        self.notes.append(msg)


# ── 1. Handgeschriebene HTML-Escaper ─────────────────────────────────────────
# Jeder davon ist eine Gelegenheit, den Namen falsch zu schreiben (escC war in
# einer Session ein ReferenceError, esc() in derselben Session der nächste).
ESCAPER_DEF = re.compile(
    r"function\s+(_?esc[A-Za-z]*)\s*\(", re.I)


def check_escapers(rep: Report, roots: list[tuple[str, Path]]) -> None:
    found: list[tuple[str, str, str]] = []
    for app, root in roots:
        tpl = root / "app/webui/templates"
        if not tpl.is_dir():
            continue
        for f in sorted(tpl.glob("*.html")):
            for name in ESCAPER_DEF.findall(f.read_text(encoding="utf-8", errors="replace")):
                found.append((app, f.name, name))
    if not found:
        rep.note("Keine handgeschriebenen Escaper — alle nutzen common.js")
        return
    remaining = [(a, f, n) for a, f, n in found if f not in ACCEPTED]
    accepted = [(a, f, n) for a, f, n in found if f in ACCEPTED]
    for a, f, n in accepted:
        rep.note(f"ok {a}/{f}: {n}()  ({ACCEPTED[f]})")
    if remaining:
        rep.fail(f"{len(remaining)} handgeschriebene(r) HTML-Escaper — "
                 f"stattdessen esc() aus common.js verwenden:")
        for a, f, n in remaining:
            rep.problems.append(f"     {a}/{f}: function {n}()")


# ── 2. Atomares Schreiben ohne Rechte auf der Temp-Datei ─────────────────────
# rename() übernimmt die Rechte der QUELLdatei; die entsteht mit umask-Default
# (meist 644). Ein chmod auf dem Ziel wird beim nächsten Speichern still
# zurückgesetzt. Dieser Fehler trat zweimal unabhängig auf.
def check_atomic_writes(rep: Report, roots: list[tuple[str, Path]]) -> None:
    hits = 0
    for app, root in roots:
        for f in sorted((root / "app").rglob("*.py")):
            src = f.read_text(encoding="utf-8", errors="replace")
            if ".replace(" not in src:
                continue
            for m in re.finditer(r"^(.*)\.replace\(\s*([A-Za-z_][\w.]*)\s*\)", src, re.M):
                tmp_var = m.group(1).strip().split()[-1]
                if "tmp" not in tmp_var.lower() and "temp" not in tmp_var.lower():
                    continue
                hits += 1
                # Steht in den ~15 Zeilen davor ein chmod auf derselben Variablen?
                start = max(0, src.rfind("\n", 0, m.start()) - 800)
                window = src[start:m.start()]
                if f"{tmp_var}.chmod(" not in window:
                    rep.fail(f"{app}/{f.relative_to(root)}: atomares Schreiben "
                             f"({tmp_var}.replace(...)) ohne {tmp_var}.chmod(0o600) davor — "
                             f"die Zieldatei erbt umask-Rechte (meist 644)")
    if hits and not rep.problems:
        rep.note(f"{hits} atomare Schreibvorgänge, alle mit chmod auf der Temp-Datei")


# ── 3. Einstellungen am deklarierten Weg vorbei ──────────────────────────────
# Im Hub ist settings_schema die einzige Quelle. Ein direkter hub_settings_store-
# Zugriff auf einen Schlüssel umgeht Rangfolge, Typprüfung und Maskierung.
def check_settings_registry(rep: Report) -> None:
    schema_file = HUB / "app/settings_schema.py"
    if not schema_file.is_file():
        rep.fail("Hub: app/settings_schema.py fehlt — Registry ist die Quelle der Wahrheit")
        return
    declared = set(re.findall(r'_s\(\s*"([A-Z0-9_]+)"', schema_file.read_text()))
    rep.note(f"Hub-Registry: {len(declared)} Schlüssel deklariert")

    # Direktzugriffe hs.get("KEY") außerhalb der Registry selbst
    allowed_direct = {"settings_schema.py", "hub_settings_store.py"}
    for f in sorted((HUB / "app").rglob("*.py")):
        if f.name in allowed_direct:
            continue
        src = f.read_text(encoding="utf-8", errors="replace")
        for key in re.findall(r'hs\.get\(\s*"([A-Z0-9_]+)"', src):
            rep.fail(f"Hub/{f.relative_to(HUB)}: hs.get(\"{key}\") umgeht die Registry — "
                     f"settings_schema.get() bzw. get_bool() verwenden")

    # Geheimnisse dürfen nicht in Templates gerendert werden: masked() liefert
    # dort Punkte, die beim Speichern zurückkämen und das Passwort ersetzen.
    secrets = set(re.findall(r'_s\(\s*"([A-Z0-9_]+)"[^)]*secret=True', schema_file.read_text()))
    tpl = HUB / "app/webui/templates"
    for f in sorted(tpl.glob("*.html")):
        src = f.read_text(encoding="utf-8", errors="replace")
        for key in secrets:
            if f"cfg.{key}" in src:
                rep.fail(f"Hub/{f.name}: rendert Geheimnis cfg.{key} — "
                         f"Geheimnisfelder bleiben leer (placeholder statt value)")


# ── 4. Gespiegelte Dateien ───────────────────────────────────────────────────
def check_mirrored(rep: Report) -> None:
    for rel, why in MIRRORED:
        a, b = GATEWAY / rel, HUB / rel
        if not a.is_file() and not b.is_file():
            rep.note(f"— {rel} existiert noch in keiner Anwendung ({why})")
            continue
        if not a.is_file() or not b.is_file():
            missing = "Gateway" if not a.is_file() else "Hub"
            rep.fail(f"{rel} fehlt in {missing} — muss in beiden gleich sein ({why})")
            continue
        ha = hashlib.sha256(a.read_bytes()).hexdigest()
        hb = hashlib.sha256(b.read_bytes()).hexdigest()
        if ha != hb:
            rep.fail(f"{rel} weicht ab (Gateway {ha[:8]} / Hub {hb[:8]}) — "
                     f"eine Fassung in die andere kopieren ({why})")
        else:
            rep.note(f"{rel}: identisch ({ha[:8]})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway-only", action="store_true")
    args = ap.parse_args()

    roots = [("Gateway", GATEWAY)]
    if not args.gateway_only:
        if not HUB.is_dir():
            print(f"Hub nicht gefunden unter {HUB} — nur Gateway geprüft", file=sys.stderr)
        else:
            roots.append(("Hub", HUB))

    rep = Report()
    check_escapers(rep, roots)
    check_atomic_writes(rep, roots)
    check_mirrored(rep)
    if any(app == "Hub" for app, _ in roots):
        check_settings_registry(rep)

    for n in rep.notes:
        print(f"  {n}")
    if rep.problems:
        print()
        for p in rep.problems:
            print(f"  {p}")
        print(f"\n{len([p for p in rep.problems if not p.startswith('    ')])} Lücke(n) gefunden.")
        return 1
    print("\nKeine Drift gefunden.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
