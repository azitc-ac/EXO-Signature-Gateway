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

# Bestand an Farben aus JavaScript (style.color/background/… = Wert, Zurücksetzen
# auf '' ausgenommen) je Vorlage am 2026-10-21. Darf nur SINKEN — sinkt eine Zahl,
# hier nachziehen. Neue Stellen: data-* setzen und im CSS färben (Dark-Mode-Regel 2).
JS_FARBEN_STAND = {
    "advanced.html": 11, "base.html": 1, "dashboard.html": 2, "mailboxes.html": 1,
    "portal.html": 2, "settings.html": 7, "settings_connect.html": 3,
    "settings_signature.html": 1, "settings_smime.html": 7, "setup.html": 13,
    "smime.html": 7, "template_editor.html": 4,
}
_JS_FARBE = re.compile(r"\.style\.(color|background|backgroundColor|borderColor)\s*=\s*(?!['\"]['\"])")


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


def test_keine_neuen_farben_aus_javascript():
    zuviel = []
    for p in sorted(VORLAGEN.glob("*.html")):
        n = len(_JS_FARBE.findall(p.read_text(encoding="utf-8")))
        if n > JS_FARBEN_STAND.get(p.name, 0):
            zuviel.append(f"{p.name}: {JS_FARBEN_STAND.get(p.name, 0)} → {n}")
    assert not zuviel, ("Farbe per JavaScript gesetzt (Dark-Mode-Regel 2): data-* setzen und "
                        "im CSS färben, oder showMsg() nutzen:\n" + "\n".join(zuviel))
