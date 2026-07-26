#!/usr/bin/env python3
"""jscheck — prüft das JavaScript in den Jinja-Vorlagen auf Syntaxfehler.

WARUM
-----
Das JS liegt inline in den Vorlagen und wird von niemandem übersetzt oder
gebündelt. Ein Tippfehler fällt deshalb erst auf, wenn ein Nutzer die Seite
öffnet und die Konsole öffnet — bis dahin tut die Schaltfläche einfach nichts.

In einer einzigen Sitzung (2026-07-26) sind zwei solche Fehler entstanden:
`escC()` als `ReferenceError` und ein `esc()`, das es in der Datei gar nicht
gab. Beide wurden nur gefunden, weil die Blöcke von Hand durch `node --check`
geschickt wurden. Dieses Skript macht daraus eine wiederholbare Prüfung.

GRENZEN
-------
`node --check` prüft nur die Syntax, nicht ob eine aufgerufene Funktion
existiert. Für Letzteres sorgt die Escaper-Prüfung in `driftcheck.py` — dort
liegt der wiederkehrende Fall.

AUFRUF
------
    python3 tools/jscheck.py                 # beide Anwendungen
    python3 tools/jscheck.py --gateway-only
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

GATEWAY = Path(__file__).resolve().parent.parent
HUB = GATEWAY.parent / "sig-provider"

SCRIPT = re.compile(r"<script([^>]*)>(.*?)</script>", re.S)
JINJA_AUSDRUCK = re.compile(r"\{\{.*?\}\}", re.S)
JINJA_BLOCK = re.compile(r"\{%.*?%\}", re.S)


def js_aus_vorlage(quelle: str) -> str:
    """Inline-Skripte zusammenfassen und Jinja durch Platzhalter ersetzen.

    `{{ x }}` wird zu `0`: die Vorlage ist kein gültiges JavaScript, solange
    die Ausdrücke drinstehen. Die Ersetzung durch ein Literal erhält die
    Struktur (Zuweisungen, Argumente) und macht den Rest prüfbar.
    """
    teile = []
    for attribute, koerper in SCRIPT.findall(quelle):
        if "src=" in attribute:
            continue                      # externe Datei, kein Inhalt zu prüfen
        koerper = JINJA_AUSDRUCK.sub("0", koerper)
        koerper = JINJA_BLOCK.sub("", koerper)
        teile.append(koerper)
    return "\n;\n".join(teile)


def pruefe(pfad: Path, node: str) -> str | None:
    """None, wenn in Ordnung — sonst die Fehlermeldung von node."""
    js = js_aus_vorlage(pfad.read_text(encoding="utf-8", errors="replace"))
    if not js.strip():
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(js)
        tmp = f.name
    try:
        r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
        if r.returncode == 0:
            return None
        # Der Pfad der Temp-Datei in der Meldung hilft niemandem.
        return (r.stderr or r.stdout).replace(tmp, pfad.name).strip()
    finally:
        Path(tmp).unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway-only", action="store_true")
    args = ap.parse_args()

    node = shutil.which("node") or shutil.which("nodejs")
    if not node:
        # Bewusst KEIN stilles Überspringen: eine Prüfung, die sich selbst
        # abschaltet, wiegt in Sicherheit, ohne zu prüfen.
        print("node nicht gefunden — für die JS-Prüfung wird Node.js benötigt "
              "(apt install nodejs)", file=sys.stderr)
        return 2

    baeume = [("Gateway", GATEWAY)]
    if not args.gateway_only and HUB.is_dir():
        baeume.append(("Hub", HUB))

    fehler: list[str] = []
    geprueft = 0
    for anwendung, wurzel in baeume:
        ordner = wurzel / "app/webui/templates"
        if not ordner.is_dir():
            continue
        for f in sorted(ordner.glob("*.html")):
            meldung = pruefe(f, node)
            geprueft += 1
            if meldung:
                fehler.append(f"{anwendung}/{f.name}:\n     "
                              + meldung.replace("\n", "\n     "))

    print(f"  {geprueft} Vorlage(n) geprüft")
    if fehler:
        print()
        for e in fehler:
            print(f"  {e}")
        print(f"\n{len(fehler)} Vorlage(n) mit JavaScript-Syntaxfehler.")
        return 1
    print("\nKein Syntaxfehler im Vorlagen-JavaScript.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
