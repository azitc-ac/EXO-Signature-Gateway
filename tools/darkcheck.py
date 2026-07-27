#!/usr/bin/env python3
"""Dark-Mode-Abdeckung prüfen: helle Inline-Hintergründe ohne Override finden.

Hintergrund: Dark Mode läuft über Attribut-Selektoren
(`[data-theme="dark"] [style*="background:#eff6ff"] { … }`). Eine Farbe, die dort
nicht gelistet ist, wird nicht umgeschaltet und leuchtet im Dunkelmodus auf.
Dieses Skript findet solche Lücken, bevor der Nutzer sie sieht.

Aufruf (aus dem Repo-Wurzelverzeichnis):
    python3 tools/darkcheck.py app/webui/templates app/webui/static/dark-mode.css
    python3 tools/darkcheck.py ../sig-provider/app/webui/templates \
                               ../sig-provider/app/webui/templates/base.html

Exit-Code 1, wenn ungedeckte Farben gefunden wurden (CI-tauglich).

WICHTIG — zwei Fallstricke, die hier bewusst behandelt werden:
  1. CSS `*=` matcht TEILSTRINGS. `[style*="background:#fff"]` greift daher auch
     auf `background:#ffffff`. Ohne diese Logik meldet die Prüfung Dutzende
     Fehlalarme (Präfix-Vergleich statt Gleichheit, siehe `_covered`).
  2. Per JavaScript gesetzte Styles (`el.style.cssText = …`) normalisiert der
     Browser zu `rgb(…)` — Attribut-Selektoren greifen dort NIE. Solche Elemente
     müssen über `data-*`/ID im Dark-Mode-CSS angesprochen werden und tauchen
     hier trotzdem als Treffer auf. Erwartete Fälle unten in ACCEPTED eintragen.
"""
import re
import sys
from pathlib import Path

# Bekannte, bewusst akzeptierte Treffer: Dateiname → Grund
ACCEPTED = {
    "smime_selfservice.html": "eigenständige Endnutzer-Seite ohne Dark Mode",
    "portal.html": "eigenständiges Empfänger-Portal ohne Dark Mode (iframe rendert Mail auf Weiß)",
    "mailboxes.html": "Fair-Use-Badge wird per JS gesetzt → via data-fu-state gelöst",
    "settings.html": "Status-Textfarben (grün/rot) per JS — lesbar auf dunkel, bewusst belassen",
}

_DECL = re.compile(r"(background|color|border(?:-\w+)?)\s*:\s*(#[0-9a-fA-F]{3,6})")
_COVERED = re.compile(r'\[style\*="[^"]*?(#[0-9a-fA-F]{3,6})"\]')
# Nur INLINE-Styles zählen. Attribut-Selektoren greifen ausschließlich auf
# style="…"-Attribute — Farben in <style>-Blöcken (der Hub hat sein komplettes
# Stylesheet dort) werden über normale CSS-Regeln umgeschaltet und sind hier
# kein Befund. Ohne diese Trennung meldet die Prüfung die Light-Mode-Regeln
# der Seite selbst als Lücke.
_STYLE_ATTR = re.compile(r'style="([^"]*)"')
_STYLE_BLOCK = re.compile(r"<style\b[^>]*>.*?</style>", re.S | re.I)
# Regel 2: per JS gesetzte Farben. Der Browser normalisiert sie zu rgb(…),
# Attribut-Selektoren greifen NIE — sie brauchen eine data-*/ID-Lösung.
_JS_STYLE = re.compile(
    r"\.style\.(?:cssText|background(?:Color)?|color|borderColor)\s*=\s*"
    r"['\"]([^'\"]*#[0-9a-fA-F]{3,6}[^'\"]*)['\"]")


def _norm(h: str) -> str:
    """#abc → #aabbcc, alles klein."""
    h = h.lower()
    return "#" + "".join(c * 2 for c in h[1:]) if len(h) == 4 else h


def _is_light(h: str) -> bool:
    """Grobe Helligkeit; nur helle Hintergründe sind im Dark Mode ein Problem."""
    return len(h) == 7 and sum(int(h[i:i + 2], 16) for i in (1, 3, 5)) / 3 > 200


def _covered(hexv: str, covered: set[str]) -> bool:
    # Präfix statt Gleichheit — CSS *= matcht Teilstrings (#fff deckt #ffffff ab).
    return any(hexv.startswith(c) for c in covered)



def _cssom_gefaehrdet(template_dir: Path, css: str) -> list[str]:
    """Elemente mit heller Inline-Hintergrundfarbe, deren `style` von JS
    angefasst wird — ohne eigene Regel auf die ID.

    Warum das eine eigene Prüfung braucht: Die Sammelregeln greifen über
    [style*="background:#hex"], also über den TEXT des Attributs. Setzt
    JavaScript irgendeine Eigenschaft desselben Elements — `el.style.display`
    genügt —, schreibt der Browser das ganze Attribut neu und serialisiert
    Farben als rgb(…). Der Selektor findet die Hex-Schreibweise dann nicht mehr.

    Für die bestehende Prüfung ist so ein Element unauffällig: die Farbe steht
    in der Palette UND im Attribut. Genau deshalb blieb der
    Verlängerungskasten der Lizenz am 27.07.2026 hell.

    Abhilfe ist eine Regel auf die ID (`[data-theme="dark"] #id { … }`), die
    unabhängig von der Serialisierung trägt. Diese Prüfung verlangt sie.
    """
    id_regeln = set(re.findall(r'\[data-theme="dark"\]\s*#([\w-]+)', css))
    mit_id_und_bg = re.compile(
        r'<[a-z]+\b(?=[^>]*\bid=["\']([\w-]+)["\'])(?=[^>]*\bstyle=["\']([^"\']*)["\'])',
        re.I)
    fehlend = []
    for f in sorted(template_dir.rglob("*.html")):
        html = f.read_text(encoding="utf-8")
        for m in mit_id_und_bg.finditer(html):
            eid, stil = m.group(1), m.group(2)
            bg = re.search(r"background:\s*(#[0-9a-fA-F]{3,6})", stil)
            if not bg or not _is_light(_norm(bg.group(1))):
                continue
            if eid in id_regeln:
                continue
            beruehrt = re.search(
                r"getElementById\(\s*['\"`]" + re.escape(eid) + r"['\"`]\s*\)\s*\.style", html)
            if not beruehrt:
                zuw = re.search(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
                                r"document\.getElementById\(\s*['\"`]" + re.escape(eid) + r"['\"`]", html)
                if zuw:
                    beruehrt = re.search(r"\b" + re.escape(zuw.group(1)) + r"\.style\b", html)
            if beruehrt:
                fehlend.append(f"{f.name}: #{eid} (background:{bg.group(1)})")
    return fehlend


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    template_dir, css_files = argv[1], argv[2:]
    css = "\n".join(Path(p).read_text(encoding="utf-8") for p in css_files)
    covered = {_norm(h) for h in _COVERED.findall(css)}

    # Auch JS-Dateien unter static/ prüfen: seit common.js einen Markdown-Wandler
    # enthält, gibt dort JavaScript selbst style="…"-Attribute aus. Ohne diese
    # Erweiterung blieben genau die Farben ungeprüft, die eine gemeinsame Datei
    # auf ALLEN Seiten beider Anwendungen erzeugt.
    _statisch = Path(template_dir).parent / "static"
    dateien = sorted(Path(template_dir).rglob("*.html"))
    if _statisch.is_dir():
        dateien += sorted(_statisch.rglob("*.js"))

    misses: dict[str, set[str]] = {}
    for f in dateien:
        html = _STYLE_BLOCK.sub("", f.read_text(encoding="utf-8"))
        for attr in _STYLE_ATTR.findall(html):
            for prop, hexv in _DECL.findall(attr):
                h = _norm(hexv)
                if prop == "background" and _is_light(h) and not _covered(h, covered):
                    misses.setdefault(h, set()).add(f.name)

    # Regel 2: JS-gefärbte Elemente einsammeln (unabhängig von der Palette —
    # sie sind IMMER ein Fall für data-*/ID, egal welche Farbe).
    js_hits: dict[str, set[str]] = {}
    for f in sorted(Path(template_dir).rglob("*.html")):
        for decl in _JS_STYLE.findall(f.read_text(encoding="utf-8")):
            for _prop, hexv in _DECL.findall(decl):
                js_hits.setdefault(f.name, set()).add(_norm(hexv))

    if js_hits:
        print("JS-gesetzte Farben (brauchen data-*/ID-Regel, siehe CLAUDE.md Regel 2):")
        for fname, hexes in sorted(js_hits.items()):
            mark = "ok " if fname in ACCEPTED else "!! "
            note = f"  ({ACCEPTED[fname]})" if fname in ACCEPTED else ""
            print(f"  {mark}{fname}: {', '.join(sorted(hexes))}{note}")
        print()

    unexpected = sum(1 for f in js_hits if f not in ACCEPTED)
    for h, files in sorted(misses.items(), key=lambda kv: -len(kv[1])):
        known = all(f in ACCEPTED for f in files)
        mark = "ok " if known else "!! "
        note = f"  ({ACCEPTED[sorted(files)[0]]})" if known else ""
        print(f"  {mark}{h}  {', '.join(sorted(files))}{note}")
        if not known:
            unexpected += 1

    gefaehrdet = _cssom_gefaehrdet(Path(template_dir), css)
    if gefaehrdet:
        print("\nInline-Hintergrund + JS fasst style an, aber keine ID-Regel:")
        for g in gefaehrdet:
            print(f"  !! {g}")
        print("  → Regel [data-theme=\"dark\"] #id { background: … } ergänzen; der "
              "Attributselektor greift nach der Neuserialisierung nicht mehr.")

    print(f"\n{len(covered)} Farben im Dark-Mode-CSS abgedeckt, "
          f"{unexpected} unerwartete Lücke(n), "
          f"{len(gefaehrdet)} Element(e) ohne ID-Regel.")
    return 1 if (unexpected or gefaehrdet) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
