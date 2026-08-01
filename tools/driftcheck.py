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
    ("tools/hooks/pre-commit", "Commit-Hook (VERSION + Changelog-Pflicht)"),
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
#
# ⚠️ Erkennung am RUMPF, nicht am Namen. Bis 2026-07-29 verlangte der Ausdruck
# einen Namen, der mit „esc" BEGINNT — `_licEsc` in settings.html trug es in
# der Mitte und blieb jahrelang unentdeckt. Ausgerechnet dieser maskierte keine
# einfachen Anführungszeichen und stand in einem Inline-Handler.
#
# Was `&` durch `&amp;` und `<` durch `&lt;` ersetzt, ist ein HTML-Escaper —
# gleich, ob er escC, _licEsc, htmlSafe oder clean heisst. Der Name lässt sich
# beliebig wählen, die Aufgabe nicht.
FUNKTIONSKOPF = re.compile(
    r"(?:function\s+([A-Za-z_$][\w$]*)\s*\(|"
    r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:function\s*\(|\([^)]*\)\s*=>|[\w$]+\s*=>))")
# Beide Ersetzungen müssen vorkommen: `&amp;` allein steht auch in Fliesstext.
ESCAPER_RUMPF = ("&amp;", "&lt;")
RUMPF_FENSTER = 400          # Zeichen nach dem Funktionskopf


def check_escapers(rep: Report, roots: list[tuple[str, Path]]) -> None:
    found: list[tuple[str, str, str]] = []
    for app, root in roots:
        tpl = root / "app/webui/templates"
        if not tpl.is_dir():
            continue
        for f in sorted(tpl.glob("*.html")):
            text = f.read_text(encoding="utf-8", errors="replace")
            treffer = list(FUNKTIONSKOPF.finditer(text))
            for i, m in enumerate(treffer):
                name = m.group(1) or m.group(2)
                # Fenster ZUSÄTZLICH am nächsten Funktionskopf abschneiden.
                # Ohne das greift es in die folgende Funktion hinein: `_catMsg`
                # wurde gemeldet, weil kurz darauf `_catEsc` steht — ein
                # Fehlalarm, und Fehlalarme sind der sichere Weg, ein Prüfskript
                # abzuschalten.
                grenze = m.end() + RUMPF_FENSTER
                if i + 1 < len(treffer):
                    grenze = min(grenze, treffer[i + 1].start())
                rumpf = text[m.end():grenze]
                if all(z in rumpf for z in ESCAPER_RUMPF):
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


# ── 3b. Guthabenpruefung am gemeinsamen Weg vorbei ───────────────────────────
# Jede Zahlstelle im Hub muss billing.deckung_sicherstellen() benutzen. Wer das
# Guthaben selbst mit einem Preis vergleicht, umgeht die automatische
# Aufladung — und merkt es nicht, weil ohne Automatik dasselbe herauskommt.
#
# Anlass (2026-07-27): Der Lizenzkauf lehnte mit "Guthaben reicht nicht" ab,
# obwohl ein Zahlungsmittel hinterlegt war. Zertifikatsbestellung und
# Verlaengerung riefen ensure_balance(), der Kauf verglich `balance_cents`
# direkt. Ein Test der Hilfsfunktion faengt das NICHT — der Fehler sitzt an der
# Aufrufstelle, nicht in der Funktion.
# Stellen, die den Saldo vergleichen DÜRFEN, weil sie keine Zahlstelle sind.
# Schlüssel: "datei.py::funktion", Wert: die Begründung.
GUTHABEN_AUSNAHMEN: dict[str, str] = {
    "store.py::debit": "die Buchung selbst — letzte Verteidigungslinie. Ein Aufruf "
                       "von deckung_sicherstellen() wäre hier ein Zirkel, denn "
                       "billing ruft debit() auf. Wer ohne Deckung buchen muss, "
                       "setzt erzwingen=True und macht das an der Aufrufstelle sichtbar.",
}


def _umgebende_funktion(zeilen: list[str], nr: int) -> str:
    """Name der def, in der Zeile `nr` (1-basiert) steht — oder ""."""
    for i in range(nr - 1, -1, -1):
        m = re.match(r"def\s+([A-Za-z_]\w*)\s*\(", zeilen[i])
        if m:
            return m.group(1)
    return ""


def check_guthaben_gate(rep: Report) -> None:
    ziel = HUB / "app"
    if not ziel.is_dir():
        rep.note("Guthabenpruefung uebersprungen — Hub-Baum nicht vorhanden")
        return
    # Vergleichsoperator im selben Ausdruck wie balance_cents
    muster = re.compile(r"balance_cents[^\n]{0,80}?(?:<=|>=|<|>)|"
                        r"(?:<=|>=|<|>)[^\n]{0,80}?balance_cents")
    treffer = 0
    for f in sorted(ziel.rglob("*.py")):
        if f.name == "billing.py":          # dort GEHOEREN sie hin
            continue
        zeilen = f.read_text(encoding="utf-8", errors="replace").splitlines()
        for nr, zeile in enumerate(zeilen, 1):
            if zeile.lstrip().startswith("#"):
                continue
            if not muster.search(zeile):
                continue
            # Eine Stelle, die auto_topup_armed() in der Naehe prueft, hat die
            # Automatik BEDACHT — etwa die Freischaltung des Zertifikatsbezugs
            # oder deren Vorpruefung. Nur wer den Saldo blind vergleicht, faellt
            # auf. Ohne diese Unterscheidung meldet die Regel zwei richtige
            # Stellen und wird deshalb ignoriert.
            umfeld = "\n".join(zeilen[max(0, nr - 12):nr + 8])
            if "auto_topup_armed" in umfeld or "deckung_sicherstellen" in umfeld:
                continue
            schluessel = f"{f.name}::{_umgebende_funktion(zeilen, nr)}"
            if schluessel in GUTHABEN_AUSNAHMEN:
                rep.note(f"ok Hub/{f.relative_to(HUB)}:{nr} ({schluessel}): "
                         f"{GUTHABEN_AUSNAHMEN[schluessel]}")
                continue
            rep.fail(f"Hub/{f.relative_to(HUB)}:{nr}: vergleicht balance_cents direkt — "
                     f"billing.deckung_sicherstellen() verwenden, sonst greift die "
                     f"automatische Aufladung nicht")
            treffer += 1
    if not treffer:
        rep.note("Guthaben: keine Zahlstelle vergleicht den Saldo an billing.py vorbei")


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


# ── 7. Abhängigkeiten müssen exakt gepinnt sein ──────────────────────────────
# Offene Angaben (>=, ~=, ohne Fassung) machen Builds unreproduzierbar: was ein
# Kunde bekommt, hängt vom Tag des Builds ab. Der Abstand war real — gefordert
# `fastapi>=0.104.0`, installiert lief `0.139.0`.
REQUIREMENTS = [("Gateway", "app/requirements.txt"), ("Hub", "requirements.txt")]


def check_pinned_requirements(rep: Report, roots: list[tuple[str, Path]]) -> None:
    bekannt = {app for app, _ in roots}
    gesamt = 0
    for app, root in roots:
        for anwendung, rel in REQUIREMENTS:
            if anwendung != app:
                continue
            f = root / rel
            if not f.is_file():
                continue
            for nr, zeile in enumerate(f.read_text().splitlines(), 1):
                z = zeile.split("#")[0].strip()
                if not z or z.startswith("-"):
                    continue
                gesamt += 1
                if "==" in z:
                    continue
                rep.fail(f"{app}/{rel}:{nr}: nicht exakt gepinnt → \"{z}\" — "
                         f"ein Build von morgen zieht möglicherweise etwas anderes")
    if gesamt and not any("gepinnt" in p for p in rep.problems):
        rep.note(f"{gesamt} Abhängigkeiten, alle exakt gepinnt")


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
    check_pinned_requirements(rep, roots)
    if hub_da:
        check_settings_registry(rep)
    check_guthaben_gate(rep)

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
