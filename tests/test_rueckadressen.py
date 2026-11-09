"""Gemerkte und geprüfte Rückadressen haben EINE Definition (v1.10.93).

Die Einrichtung merkte sich nur `publicClient`, „Anmeldung prüfen“ verglich
`publicClient` + `web`. Weil die Einrichtung selbst die id-callback-Adresse
unter `web` setzt, meldete die Prüfung nach JEDER Einrichtung „Rückadressen
wichen ab und wurden nachgezogen“ — in Azure war nichts falsch.
"""
import re
from pathlib import Path

import setup_wizard

APP = Path(__file__).resolve().parent.parent / "app"


def test_beide_listen():
    obj = {"publicClient": {"redirectUris": ["http://localhost"]},
           "web": {"redirectUris": ["https://gw.example/auth/id-callback"]}}
    assert setup_wizard.rueckadressen(obj) == [
        "http://localhost", "https://gw.example/auth/id-callback"]
    assert setup_wizard.rueckadressen({}) == []


def test_jede_stelle_nutzt_die_eine_definition():
    """Wer BOOTSTRAP_REDIRECT_URIS schreibt oder damit vergleicht, rechnet über
    `rueckadressen()` — kein eigenes Zusammensetzen der Listen."""
    for datei in (APP / "setup_wizard.py", APP / "webui" / "routen" / "setup.py"):
        text = datei.read_text("utf-8")
        eigen = re.findall(r'get\("(?:publicClient|web)"\) or \{\}\)\.get\("redirectUris"\)', text)
        erlaubt = 2 if datei.name == "setup_wizard.py" else 0     # die Definition selbst
        assert len(eigen) == erlaubt, f"{datei.name}: eigene Zusammensetzung {eigen}"
    assert "setup_wizard.rueckadressen(app_obj)" in (APP / "webui/routen/setup.py").read_text()
    assert "BOOTSTRAP_REDIRECT_URIS\": rueckadressen(" in (APP / "setup_wizard.py").read_text()
