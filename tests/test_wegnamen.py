"""Ein Weg in der Oberfläche heißt überall so, wie der Knopf beschriftet ist.

Anlass (2026-11-08): Die Aufgabe „Anmeldung per Zertifikat“ verwies auf
„Erneut anmelden“, der sichtbare Knopf in Schritt 5 hieß aber „App-Registrierung
neu einrichten“ — derselbe Weg unter zwei Namen. Wer der Anleitung folgte,
suchte einen Knopf, den es so nicht gab. Geprüft wird: jeder in Meldungen
genannte Weg der Form `→ „Beschriftung“` steht als Beschriftung eines
Bedienelements in den Vorlagen.
"""
import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
VORLAGEN = APP / "webui" / "templates"

# Kurz, einzeilig, ohne Ziffern: eine Beschriftung, kein Zitat oder Datum.
_VERWEIS = re.compile(r"→ „([^“\"\n\d]{1,50})“")


def _verweise() -> dict[str, list[str]]:
    aus: dict[str, list[str]] = {}
    for p in APP.rglob("*.py"):
        for name in _VERWEIS.findall(p.read_text("utf-8")):
            aus.setdefault(name, []).append(p.relative_to(APP).as_posix())
    return aus


def _beschriftungen() -> str:
    return "\n".join(p.read_text("utf-8") for p in VORLAGEN.rglob("*.html"))


def test_es_gibt_verweise():
    assert "Erneut anmelden" in _verweise(), "Muster greift nicht — Test wäre still grün"


def test_jeder_verweis_hat_ein_bedienelement():
    html = _beschriftungen()
    fehlt = {name: orte for name, orte in _verweise().items()
             # `>Text<` im HTML oder `'Text</a>` in einem Skript-Baustein
             if not re.search(r"[>']\s*(?:↩\s*)?" + re.escape(name) + r"\b", html)}
    assert not fehlt, f"Weg genannt, aber kein Knopf/Link mit dieser Beschriftung: {fehlt}"


def test_schritt5_knopf_heisst_wie_die_anleitung():
    html = (VORLAGEN / "setup.html").read_text("utf-8")
    assert 'onclick="rerunAzureSetup()">Erneut anmelden</button>' in html
