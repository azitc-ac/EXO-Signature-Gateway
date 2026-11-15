"""settings_store.update() verwirft Schlüssel, die nicht in DEFAULTS stehen — still.

Interne Laufzeitschlüssel (INTERNAL_KEYS, meist mit „_“ am Anfang) gehören in
force_update(). Bis v1.12.34 schrieb die Einrichtung der S/MIME-Regeln ihren
Merker `_SMIME_REGELN_STAND` per update(); er ging verloren, und die Aufgabe
blieb nach dem Nachhol-Knopf offen. Der zugehörige Test hatte update() durch
dict.update ersetzt und sah den Filter nie.

Dieser Test prüft jeden update()-Aufruf mit einem Wörterbuch-Literal im Code.
"""
import ast
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import settings_store  # noqa: E402


def _aufrufe():
    for datei in sorted((WURZEL / "app").rglob("*.py")):
        try:
            baum = ast.parse(datei.read_text("utf-8"))
        except SyntaxError:
            continue
        for knoten in ast.walk(baum):
            if (isinstance(knoten, ast.Call) and isinstance(knoten.func, ast.Attribute)
                    and knoten.func.attr == "update"
                    and isinstance(knoten.func.value, ast.Name)
                    and knoten.func.value.id in ("settings_store", "_ss", "ss")
                    and knoten.args and isinstance(knoten.args[0], ast.Dict)):
                for k in knoten.args[0].keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        yield datei.relative_to(WURZEL), knoten.lineno, k.value


def test_update_bekommt_nur_defaults_schluessel():
    aufrufe = list(_aufrufe())
    assert len(aufrufe) > 20, "Testaufbau: kaum update()-Aufrufe gefunden — Suchmuster prüfen"
    verworfen = [f"{d}:{z} {k}" for d, z, k in aufrufe if k not in settings_store.DEFAULTS]
    assert not verworfen, (
        "settings_store.update() verwirft diese Schlüssel still (nicht in DEFAULTS). "
        "Interne Laufzeitschlüssel per force_update() schreiben:\n" + "\n".join(verworfen))
