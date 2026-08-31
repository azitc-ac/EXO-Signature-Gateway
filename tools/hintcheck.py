#!/usr/bin/env python3
"""hintcheck — misst im Browser, ob die „mehr"-Schalter halten, was sie versprechen.

WOFÜR
-----
`initHintClamps()` kürzt lange Erklärtexte auf zwei Zeilen und hängt einen
Schalter „mehr" an. Ob ein Text tatsächlich abgeschnitten wird, hängt an der
Breite seines Kastens — das ist im Quelltext nicht zu sehen und mit pytest
nicht zu prüfen. Es braucht ein Layout.

Am 06.08.2026 fiel dem Betreiber auf: „jetzt steht oft mehr da — auch da wo gar
nicht mehr steht." Die erste Fassung entschied nach Textlänge (ab 150 Zeichen).
Gemessen wurden daraufhin 23 Schalter, von denen 8 nichts verbargen und 3
weitere beim Klick exakt dasselbe zeigten (2px Überlauf durch Rundung).

Dieses Werkzeug prüft drei Dinge, die alle still fehlschlagen können:

  1. Schalter ohne Inhalt     — verspricht mehr, zeigt nichts
  2. Abschnitt ohne Schalter  — Text abgeschnitten und nicht aufklappbar
  3. Schalter ohne Wirkung    — Klick ändert die Höhe nicht

VORAUSSETZUNG
-------------
Laufender Gateway-Container. Die Sitzung wird selbst signiert, ein Kennwort ist
nicht nötig und liegt hier auch nicht vor.

    python3 tools/hintcheck.py
"""
from __future__ import annotations

import asyncio
import subprocess
import sys

CONTAINER = "exo-signature-gateway"
BASIS = "https://127.0.0.1"
SEITEN = ["/settings", "/settings/signature", "/settings/smime", "/settings/connect",
          "/mailboxes", "/advanced", "/backup", "/setup", "/smime"]

MESSUNG = """() => {
  const schalter = [...document.querySelectorAll('.hint-toggle')]
        .filter(b => b.offsetParent !== null);
  const leer = [], wirkungslos = [];
  schalter.forEach(b => {
    const p = b.previousElementSibling;
    if (!p) return;
    if (p.scrollHeight <= p.clientHeight + 1) leer.push(p.textContent.trim().slice(0, 60));
    const h1 = p.clientHeight;
    b.click();
    const wirkt = p.clientHeight > h1 && b.textContent === 'weniger';
    b.click();
    if (!wirkt) wirkungslos.push(p.textContent.trim().slice(0, 60));
  });
  const stumm = [...document.querySelectorAll('.hint[data-clamp="zu"]')]
        .filter(p => { const s = p.nextElementSibling;
                       return !(s && s.classList.contains('hint-toggle')); })
        .map(p => p.textContent.trim().slice(0, 60));
  return {schalter: schalter.length, leer, wirkungslos, stumm};
}"""


def _sitzung() -> tuple[str, str]:
    aus = subprocess.run(
        ["docker", "exec", CONTAINER, "python3", "-c",
         "import sys;sys.path.insert(0,'/app');import settings_store;"
         "settings_store.init();import sso;print(sso.SESSION_COOKIE);"
         "print(sso.create_session_cookie('admin', local=True))"],
        capture_output=True, text=True, timeout=60)
    if aus.returncode != 0:
        raise SystemExit(f"Container {CONTAINER} nicht erreichbar:\n{aus.stderr.strip()}")
    zeilen = aus.stdout.strip().splitlines()
    return zeilen[0], zeilen[1]


async def _lauf() -> int:
    from playwright.async_api import async_playwright   # nur hier nötig

    name, wert = _sitzung()
    befunde = 0
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(ignore_https_errors=True,
                                        viewport={"width": 1280, "height": 900})
        await ctx.add_cookies([{"name": name, "value": wert, "domain": "127.0.0.1",
                                "path": "/", "secure": True}])
        seite = await ctx.new_page()
        gesamt = 0
        for pfad in SEITEN:
            try:
                # networkidle scheitert auf Seiten, die dauerhaft abfragen
                await seite.goto(BASIS + pfad, wait_until="domcontentloaded", timeout=25000)
                await seite.wait_for_timeout(1800)
                d = await seite.evaluate(MESSUNG)
            except Exception as e:                       # noqa: BLE001
                print(f"  {pfad}: nicht messbar ({type(e).__name__})")
                continue
            gesamt += d["schalter"]
            for art, texte in (("verspricht mehr, zeigt nichts", d["leer"]),
                               ("Klick ohne Wirkung", d["wirkungslos"]),
                               ("abgeschnitten ohne Schalter", d["stumm"])):
                for t in texte:
                    befunde += 1
                    print(f"  {pfad}: {art} — „{t}…\"")
        print(f"\n{gesamt} Schalter geprüft, {befunde} Befund(e).")
        await browser.close()
    return 1 if befunde else 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(_lauf()))
    except ImportError:
        print("playwright fehlt — pip install playwright && playwright install chromium",
              file=sys.stderr)
        raise SystemExit(2)
