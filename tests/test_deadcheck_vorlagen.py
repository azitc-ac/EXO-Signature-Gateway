"""deadcheck prüft auch Funktionen in den Vorlagen — je SEITE, nicht über alle Dateien.

Anlass (2026-10-21): 140 Zeilen Diagnose-Funktionen blieben in setup.html liegen,
als die Diagnose nach debug.html umzog. deadcheck prüfte nur static/*.js; eine
Textsuche über alle Dateien hätte die Kopie für benutzt gehalten, weil die
gleichnamigen Funktionen in debug.html leben. Der erste Lauf der geschärften
Prüfung fand fünf weitere tote Funktionen.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

WERKZEUG = Path(__file__).parent.parent / "tools/deadcheck.py"
_spec = importlib.util.spec_from_file_location("deadcheck", WERKZEUG)
deadcheck = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deadcheck)


def _baum(tmp_path, vorlagen: dict[str, str]) -> Path:
    t = tmp_path / "app/webui/templates"
    t.mkdir(parents=True)
    (tmp_path / "app/webui/static").mkdir(parents=True)
    for n, inhalt in vorlagen.items():
        (t / n).write_text(inhalt, encoding="utf-8")
    return tmp_path


def _befunde(tmp_path, vorlagen):
    return deadcheck.pruefe_vorlagen(_baum(tmp_path, vorlagen), "T")


def test_gleichnamige_funktion_auf_anderer_seite_haelt_nicht_am_leben(tmp_path):
    b = _befunde(tmp_path, {
        "base.html": "<html>{% block c %}{% endblock %}</html>",
        "setup.html": '{% extends "base.html" %}<script>\nasync function obsLesen() { return 1; }\n</script>',
        "debug.html": '{% extends "base.html" %}<button onclick="obsLesen()"></button>'
                      '<script>\nasync function obsLesen() { return 2; }\n</script>',
    })
    assert len(b) == 1 and "obsLesen() in setup.html:2" in b[0]


def test_aufruf_ueber_include_und_extends_zaehlt(tmp_path):
    b = _befunde(tmp_path, {
        "base.html": "<html><script>\nfunction basisHilfe() {}\n</script>{% block c %}{% endblock %}</html>",
        "_teil.html": "<script>\nfunction teilHilfe() { basisHilfe(); }\n</script>",
        "seite.html": '{% extends "base.html" %}{% include "_teil.html" %}<a onclick="teilHilfe()"></a>',
    })
    assert b == []


def test_unbenutzt_im_teil_wird_gemeldet(tmp_path):
    b = _befunde(tmp_path, {
        "_teil.html": "<script>\nfunction niemand() {}\n</script>",
        "seite.html": '{% include "_teil.html" %}',
    })
    assert len(b) == 1 and "niemand()" in b[0]
