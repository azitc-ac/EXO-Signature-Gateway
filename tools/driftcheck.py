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
    ("app/secure_io.py", "Schreiben von Geheimnissen (600/700, atomar)"),
    ("app/update_core.py", "Selbst-Update, Container-Seite"),
]

# Dateinamen, die ein Geheimnis enthalten. Wer eine davon schreibt, muss
# secure_io benutzen — sonst entstehen sie mit umask-Rechten (meist 644).
# Grundlage: Audit 2026-07-26, das S/MIME-Privatschluessel mit 644 fand.
SECRET_FILE_HINTS = ("key.pem", "auth.pfx", ".p12", ".pfx",
                     "account_key", "private_key",
                     "settings.json", "customers.json", "hub_settings.json")


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
def check_mirrored(rep: Report, hub_verfuegbar: bool = True) -> None:
    if not hub_verfuegbar:
        # In der CI des Gateways liegt das (private) Hub-Repository nicht vor.
        # Die Spiegelung wird beim Hub-Lauf geprueft, wo beide Baeume da sind.
        rep.note(f"Spiegelung uebersprungen ({len(MIRRORED)} Dateien) — "
                 f"Hub-Baum nicht vorhanden")
        return
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


# ── 5. Geheimnisse ohne secure_io schreiben ──────────────────────────────────
def check_secret_writes(rep: Report, roots: list[tuple[str, Path]]) -> None:
    ok = 0
    for app, root in roots:
        for f in sorted((root / "app").rglob("*.py")):
            if f.name == "secure_io.py":
                continue
            for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                if ".write_bytes(" not in line and ".write_text(" not in line:
                    continue
                low = line.lower()
                if not any(h in low for h in SECRET_FILE_HINTS):
                    continue
                if "secure_io" in line:
                    ok += 1
                    continue
                rep.fail(f"{app}/{f.relative_to(root)}:{i}: schreibt ein Geheimnis "
                         f"ohne secure_io → entsteht mit umask-Rechten (meist 644)"
                         f"\n       {line.strip()[:96]}")
    if ok:
        rep.note(f"{ok} Geheimnis-Schreibvorgänge, alle über secure_io")


# ── 6. Gateway: Geheimnisse in Vorlagen ──────────────────────────────────────
# Der Gateway reicht settings_store.get_all() UNMASKIERT an die Vorlagen. Heute
# rendert keine ein Geheimnis (geprüft), aber ein einziges {{ s.CLIENT_SECRET }}
# würde es in den HTML-Quelltext schreiben.
def check_gateway_template_secrets(rep: Report) -> None:
    ss = GATEWAY / "app/settings_store.py"
    if not ss.is_file():
        return
    src = ss.read_text()
    # Aus der Deklaration lesen, nicht nach Namen raten. Die frühere Heuristik
    # hätte KV_KEY_MODE (kein Geheimnis) mitgezählt und HUB_CLAIM_TOKEN oder
    # LICENSE_KEY je nach Schreibweise verfehlt.
    m = re.search(r"SECRET_KEYS\s*=\s*frozenset\(\{(.*?)\}\)", src, re.S)
    if not m:
        rep.fail("Gateway: settings_store.SECRET_KEYS fehlt — die Geheimnis-"
                 "Klassifizierung ist die Grundlage von public_view() und "
                 "_EXPORT_EXCLUDE")
        return
    secretish = re.findall(r'"([A-Z0-9_]+)"', m.group(1))
    leaks = 0
    for f in sorted((GATEWAY / "app/webui/templates").glob("*.html")):
        t = f.read_text(errors="replace")
        for k in secretish:
            # Ausgabe als Wert ist der Fehler; {% if s.X %} als Bedingung ist ok.
            if re.search(r"\{\{\s*s\." + k + r"\b", t):
                rep.fail(f"Gateway/{f.name}: gibt Geheimnis s.{k} im HTML aus")
                leaks += 1
    # Zusatzprüfung: reichen die Vorlagen-Kontexte den Klartext durch?
    appy = (GATEWAY / "app/webui/app.py")
    if appy.is_file():
        for i, line in enumerate(appy.read_text().splitlines(), 1):
            if '"s": settings_store.get_all()' in line:
                rep.fail(f"Gateway/app/webui/app.py:{i}: reicht Klartext-Einstellungen "
                         f"an eine Vorlage → settings_store.public_view() verwenden")
                leaks += 1
    if not leaks:
        rep.note(f"Gateway-Vorlagen: keines der {len(secretish)} deklarierten "
                 f"Geheimnisse wird ausgegeben, Kontexte sind maskiert")


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

    hub_da = any(app == "Hub" for app, _ in roots)
    rep = Report()
    check_escapers(rep, roots)
    check_atomic_writes(rep, roots)
    check_mirrored(rep, hub_verfuegbar=hub_da)
    check_secret_writes(rep, roots)
    check_gateway_template_secrets(rep)
    if hub_da:
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
