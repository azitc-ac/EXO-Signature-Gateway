"""S/MIME ist Opt-in — der Haken muss aber AKTIVIERBAR bleiben.

Regression: Der Opt-in-Default (S/MIME aus) war korrekt, aber die
Speichern-Funktion des Modus-Schritts schrieb den Checkbox-Wert nicht mit.
Default aus + Save ignoriert Haken = S/MIME nie aktivierbar. Diese Tests
sichern beide Hälften: Default aus UND der Wert wird geschrieben und
gerendert.
"""
import re
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

SETUP = (WURZEL / "app" / "webui" / "templates" / "setup.html").read_text()


def test_smime_enabled_ist_persistente_einstellung():
    import settings_store
    assert "SMIME_ENABLED" in settings_store.DEFAULTS
    assert settings_store.DEFAULTS["SMIME_ENABLED"] is False  # Opt-in


def test_smime_enabled_ist_im_register():
    from einstellungen_register import REGISTER
    assert "SMIME_ENABLED" in REGISTER
    assert REGISTER["SMIME_ENABLED"].ort  # bedienbar → braucht einen Ort


def test_savemode_schreibt_smime_enabled_mit():
    """Der Modus-Schritt muss den Haken mitschreiben, sonst bleibt S/MIME aus."""
    m = re.search(r"async function saveModeSelection\(\).*?\n\}", SETUP, re.S)
    assert m, "saveModeSelection nicht gefunden"
    rumpf = m.group(0)
    assert "SMIME_ENABLED" in rumpf, (
        "saveModeSelection schreibt SMIME_ENABLED nicht — S/MIME wäre nicht "
        "aktivierbar (Regression).")


def test_checkbox_haengt_am_persistenten_wert():
    """Der Haken wird aus dem gespeicherten Wert (oder bereits angelegten
    Regeln) gerendert, sonst geht die Wahl beim Neuladen verloren."""
    assert "s.SMIME_ENABLED" in SETUP
