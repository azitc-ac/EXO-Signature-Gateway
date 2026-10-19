#!/usr/bin/env python3
"""Rendert das Zeichen des Gateways (app/webui/static/favicon.svg) in alle
Rastergrößen, die ausserhalb des Browsers gebraucht werden.

    .venv/bin/python tools/symbole_rendern.py

Erzeugt:
  static/favicon.ico            16/32/48 (Browser, Fehlerseiten)
  static/apple-touch-icon.png   180, auf weissem Grund (iOS füllt Transparenz schwarz)
  static/symbol/<n>.png         16 32 64 80 128 192 — Add-in-Symbole (Menüband,
                                Add-in-Liste) und Teams-App (color.png = 192)
  static/symbol/quelle.sha256   Prüfsumme des SVG, aus dem gerendert wurde —
                                tests/test_favicon.py schlägt an, wenn das SVG
                                geändert, aber nicht neu gerendert wurde.

Braucht Playwright mit Chromium (wie tools/readme_screenshots.py); das Image
selbst rendert nichts, es liefert die Dateien nur aus.
"""
from __future__ import annotations

import base64
import hashlib
import struct
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parents[1]
STATIC = WURZEL / "app" / "webui" / "static"
QUELLE = STATIC / "favicon.svg"
SYMBOL = STATIC / "symbol"
ADDIN_GROESSEN = (16, 32, 64, 80, 128, 192)
ICO_GROESSEN = (16, 32, 48)


def _png(seite, svg_b64: str, n: int, grund: str = "transparent", rand: int = 0) -> bytes:
    innen = n - 2 * rand
    seite.set_content(
        f'<html><body style="margin:0;background:{grund}">'
        f'<div style="width:{n}px;height:{n}px;display:grid;place-items:center">'
        f'<img src="data:image/svg+xml;base64,{svg_b64}" width="{innen}" height="{innen}">'
        f'</div></body></html>')
    seite.wait_for_function("document.images[0].complete")
    return seite.screenshot(clip={"x": 0, "y": 0, "width": n, "height": n},
                            omit_background=(grund == "transparent"))


def _ico(bilder: dict[int, bytes]) -> bytes:
    """ICO mit eingebetteten PNGs (seit Windows Vista zulässig)."""
    kopf = struct.pack("<HHH", 0, 1, len(bilder))
    eintraege, daten = b"", b""
    versatz = 6 + 16 * len(bilder)
    for n, png in sorted(bilder.items()):
        eintraege += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(png), versatz)
        daten += png
        versatz += len(png)
    return kopf + eintraege + daten


def main() -> int:
    from playwright.sync_api import sync_playwright
    svg = QUELLE.read_bytes()
    b64 = base64.b64encode(svg).decode()
    SYMBOL.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        seite = browser.new_page(device_scale_factor=1)
        for n in ADDIN_GROESSEN:
            (SYMBOL / f"{n}.png").write_bytes(_png(seite, b64, n))
        (STATIC / "favicon.ico").write_bytes(_ico({n: _png(seite, b64, n) for n in ICO_GROESSEN}))
        (STATIC / "apple-touch-icon.png").write_bytes(_png(seite, b64, 180, "#ffffff", rand=14))
        browser.close()
    (SYMBOL / "quelle.sha256").write_text(hashlib.sha256(svg).hexdigest() + "\n")
    print("gerendert:", ", ".join(f"{n}px" for n in ADDIN_GROESSEN), "+ favicon.ico + apple-touch-icon.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
