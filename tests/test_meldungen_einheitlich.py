"""Meldungen: EINE showAlert/hideAlert (base.html), Farben nie aus JavaScript.

Anlass (2026-10-21): showAlert war sechsmal lokal überschrieben — drei Seiten
blendeten nach 6 s aus, drei nicht. Eine Fehlermeldung, die verschwindet,
während man wegsieht, verfehlt Speicher-Regel 3 („meldet am Ort, ob es gewirkt
hat“). Jetzt bleibt die Meldung überall stehen. Dazu setzten vier Meldungs-
funktionen ihre Farbe per JS — im Dunkelmodus wirkungslos (Dark-Mode-Regel 2).
"""
from __future__ import annotations

import re
from pathlib import Path

VORLAGEN = Path(__file__).parent.parent / "app/webui/templates"

# Farben aus JavaScript: bis 2026-10-21 als Stand je Vorlage (59), seither null.
# Erfasst werden Zuweisungen an style.color/background/…Color und style.cssText-
# Anweisungen (samt Folgezeilen), die eine Farbe enthalten. Zurücksetzen auf ''
# zählt nicht. Ausnahmen nur mit Grund.
_JS_FARBE = re.compile(r"\.style\.(color|background|backgroundColor|border\w*Color)\s*=(?!\s*['\"]['\"])")
_CSSTEXT = re.compile(r"style\.cssText\s*=")
_FARBANGABE = re.compile(r"(?<![-\w])(color|background(-color)?|border(-\w+)?-color)\s*:")
AUSNAHMEN = {
    ("portal.html", "iframe.style.cssText"):
        "Mailinhalt im Portal: Mails sind auf Weiß gestaltet, auch im Dunkelmodus",
}


def _anweisung(text: str, i: int) -> str:
    """Anweisung ab i bis Zeilenende, über Folgezeilen hinweg, solange eine Zeile mit + endet."""
    j = text.find("\n", i)
    teil = text[i:j if j >= 0 else None]
    while j >= 0 and teil.rstrip().endswith("+"):
        k = text.find("\n", j + 1)
        teil += text[j:k if k >= 0 else None]
        j = k
    return teil


def js_farben(text: str):
    for m in _JS_FARBE.finditer(text):
        yield m.start()
    for m in _CSSTEXT.finditer(text):
        if _FARBANGABE.search(_anweisung(text, m.start())):
            yield m.start()


def test_show_alert_nur_in_base():
    treffer = [p.name for p in sorted(VORLAGEN.glob("*.html"))
               if re.search(r"function\s+(showAlert|hideAlert)\s*\(", p.read_text(encoding="utf-8"))]
    assert treffer == ["base.html"], treffer


def test_zustimmungs_dialoge_melden_ueber_showmsg():
    """Die vier Meldungsfunktionen mit JS-Farbe (_dpaMsg, _dcMsg, _hcMsg, _invMsg):
    _dcMsg nutzt showMsg, die drei Dialog-Meldungen leben jetzt im Baustein."""
    sc = (VORLAGEN / "settings_connect.html").read_text(encoding="utf-8")
    zeile = next(z for z in sc.splitlines() if "function _dcMsg(" in z)
    assert "showMsg(" in zeile and "style.color" not in zeile, zeile
    assert re.search(r'id="dc-msg"[^>]*class="field-msg"', sc)
    for name in ("_dpaMsg", "_hcMsg", "_invMsg"):
        for datei in ("settings_connect.html", "advanced.html"):
            assert name not in (VORLAGEN / datei).read_text(encoding="utf-8"), (name, datei)
    zs = (VORLAGEN / "_zustimmung.html").read_text(encoding="utf-8")
    assert "showMsg(" in zs and 'class="field-msg zs-meldung"' in zs and "style.color" not in zs


def test_keine_farben_aus_javascript():
    funde = []
    for p in sorted(VORLAGEN.glob("*.html")):
        text = p.read_text(encoding="utf-8")
        for i in js_farben(text):
            zeile = text[text.rfind("\n", 0, i) + 1:text.find("\n", i)].strip()
            if any(p.name == d and k in zeile for d, k in AUSNAHMEN):
                continue
            funde.append(f"{p.name}:{text.count(chr(10), 0, i) + 1}: {zeile[:100]}")
    assert not funde, ("Farbe per JavaScript gesetzt (Dark-Mode-Regel 2): data-* setzen und im CSS "
                       "färben, Ergebnis per showMsg()/savePartial(), Umschalter per .btn-toggle:\n"
                       + "\n".join(funde))


def test_pruefer_erkennt_farben():
    assert list(js_farben("el.style.color = ok ? '#16a34a' : '#dc2626';"))
    assert list(js_farben("b.style.borderBottomColor = '#4f46e5';"))
    assert list(js_farben("x.style.cssText = 'padding:4px;color:#94a3b8';"))       # Farbe hinter ;
    assert list(js_farben("x.style.cssText = 'padding:4px;' +\n  'background:#eff6ff';"))  # Folgezeile
    assert not list(js_farben("el.style.color = '';"))
    assert not list(js_farben("el.style.color = '' ;"))
    assert not list(js_farben("x.style.cssText = 'display:flex;gap:4px';"))
