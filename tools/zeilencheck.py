#!/usr/bin/env python3
"""Eine Einstellungszeile hat genau eine von zwei Formen.

ANLASS (2026-08-25)
-------------------
Der Nutzer mit Bildschirmfoto: *„schau dir mal die einstellungen->Signatur
seite an bei 960 px. kraut und rüben!"* — und, wichtiger:

    „ich fürchte, ich überfordere dich mit wünschen zum layout die eine sache
     fixen, aber an anderer stelle neue löcher reißen. Können wir das bitte
     systematisch angehen"

Er hat recht. Jede Layoutmeldung wurde einzeln behoben, und weil es keine Regel
gab, gegen die sich prüfen liess, entstand mit jeder Korrektur eine neue Lücke.
Dieses Skript hält die Regel fest — dasselbe Vorgehen wie bei der
Speichern-Linie, dem Dunkelmodus und den Begriffen.

DIE ZWEI FORMEN
---------------
    SCHALTER   [x] Beschriftung [Zusatzfeld]
               Beginnt mit `<label class="checkbox-label">`. Der Text steht
               NEBEN dem Kästchen und darf die ganze Zeile nehmen.

    FELD       Beschriftung │ Eingabe
               Beginnt mit `<label>`, danach die Eingabe (meist in
               `.settings-control`). Die Beschriftung steht in einer festen
               Spalte, damit die Eingaben untereinander fluchten.

WAS DIESES SKRIPT MELDET
------------------------
1. Eine Zeile, deren Beschriftung KEINE eigene Eingabe hat, sondern nur ein
   Kästchen mit eigenem Text daneben. Das ist eine Überschrift, die sich als
   Zeile ausgibt — die Beschriftung links und der Text am Kästchen sagen dann
   zweimal dasselbe, in zwei verschiedenen Spalten.

2. Eine Zeile, die die Beschriftungsspalte von Hand nachbaut
   (`min-width:200px` in einem eigenen `<div>`) statt die gemeinsame Klasse zu
   nutzen. Solche Nachbauten gehen bei jeder Änderung an der echten Spalte
   verloren.

⚠️ WAS ES NICHT MELDET — und warum
Ein KURZER Zusatz neben dem Schalter (`<span class="hint">Benötigt …</span>`)
ist zulässig. Gemessen sind das rund 480px, also nichts Gequetschtes; ihn in
eine eigene Zeile zu zwingen, machte die Seite länger ohne sie klarer zu
machen. Beanstandet wird nur, was den Text in eine 200px-Spalte presst.

Aufruf:
    python3 tools/zeilencheck.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

GATEWAY = Path(__file__).resolve().parent.parent
VORLAGEN = GATEWAY / "app" / "webui" / "templates"

# Zeilen, die bewusst abweichen — mit Grund. Ohne Grund ist es keine Ausnahme.
ERLAUBT: dict[str, str] = {}

_ZEILE = re.compile(r'<div class="settings-row"[^>]*>(.*?)\n\s*</div>\s*\n', re.S)


def _blocktext(block: str) -> str:
    """Sichtbarer Text ohne Jinja und Markup."""
    t = re.sub(r"\{[%{].*?[%}]\}", " ", block, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def pruefe(wurzel: Path) -> list[str]:
    meldungen = []
    for datei in sorted((wurzel / "app" / "webui" / "templates").glob("*.html")):
        text = datei.read_text("utf-8")
        for m in _ZEILE.finditer(text):
            block = m.group(1)
            nr = text[: m.start()].count("\n") + 1
            ort = f"{datei.name}:{nr}"
            if ort in ERLAUBT:
                continue

            # [2] Nachgebaute Beschriftungsspalte
            #
            # ⚠️ Bis 2026-08-26 wurde nur `<div style="min-width:200px">`
            # gesucht. Der teurere Nachbau steht aber am `<label>` selbst, und
            # zwar in zwei Schreibweisen:
            #
            #     <label class="checkbox-label" style="flex:0 0 200px">
            #     <label style="min-width:200px;flex-shrink:0">
            #
            # Beides ist ein INLINE-Stil und schlägt damit jede Regel — auch die
            # Medienabfrage, die die 200px-Basis unter 600px zurücknimmt, weil
            # aus ihr dort eine HÖHE wird. Auf dem Telefon klafften deshalb
            # 200px Leere unter „Indikator: verschlüsselt" und „Indikator:
            # signiert"; gemessen wurde die Zeile 274px hoch bei 56px Inhalt.
            #
            # Die Prüfung fand das nicht, weil sie nach `div` suchte und nach
            # `min-width` — der Fall stand als `label` und als `flex` da.
            if re.search(r'<(?:div|label)[^>]*style="[^"]*'
                         r'(?:min-width:\s*200px|flex:\s*[01]\s+[01]\s+200px)', block):
                meldungen.append(
                    f"   {ort}  baut die Beschriftungsspalte von Hand nach\n"
                    f"          → `<label class=\"checkbox-label\">` bzw. ein "
                    f"schlichtes `<label>` verwenden")
                continue

            # [1] Beschriftung ohne eigene Eingabe, daneben ein Kästchen mit Text
            beginnt_mit_label = re.match(r'\s*<label(?![^>]*class="checkbox-label")', block)
            if not beginnt_mit_label or 'type="checkbox"' not in block:
                continue
            # Hat die Zeile ausser dem Kästchen ein eigenes Eingabefeld?
            ohne_kaestchen = re.sub(r'<input type="checkbox"[^>]*>', " ", block)
            eigene_eingabe = re.search(r"<(input|select|textarea)\b", ohne_kaestchen)
            if eigene_eingabe:
                continue        # „Beschriftung │ Feld + Häkchen" ist zulässig
            # Trägt das Kästchen einen eigenen Text? Dann ist die Beschriftung
            # links eine Überschrift.
            kasten_text = re.search(
                r'<label[^>]*class="checkbox-label"[^>]*>(.*?)</label>', block, re.S)
            if not kasten_text:
                kasten_text = re.search(
                    r'<input type="checkbox"[^>]*>\s*(?:\n\s*)?<label[^>]*>(.*?)</label>',
                    block, re.S)
            if kasten_text and len(_blocktext(kasten_text.group(1))) > 3:
                links = _blocktext(block).split(_blocktext(kasten_text.group(1)))[0]
                meldungen.append(
                    f"   {ort}  „{links.strip()[:40]}\" ist eine Überschrift, "
                    f"keine Zeilenbeschriftung\n"
                    f"          → `<h3>` davor, dann die Schalter-Form")
    return meldungen


def main() -> int:
    treffer = pruefe(GATEWAY)
    print(("ok  " if not treffer else "!!  ")
          + f"Gateway: {len(treffer)} Zeile(n) ausserhalb der zwei Formen")
    if not treffer:
        return 0
    print("\nEine Einstellungszeile hat genau eine von zwei Formen:\n"
          "  SCHALTER   [x] Beschriftung [Zusatzfeld]   — ganze Zeile\n"
          "  FELD       Beschriftung │ Eingabe          — feste Spalte links\n")
    print("\n".join(treffer))
    return 1


if __name__ == "__main__":
    sys.exit(main())
