"""Zeitraum der Abwesenheit: EIN Baustein für Selbstbedienung und Admin-Seite.

Anlass (26.11.2026, Outlook iOS, Add-in „Signatur & Abwesenheit“): Von-Datum,
Uhrzeit und Bis-Datum standen gequetscht in einer Zeile, die Bis-Uhrzeit
rutschte allein in die nächste. Ursache: vier Kästchen in einer umbrechenden
Flex-Zeile — derselbe Aufbau zweimal von Hand, in self.html und
abwesenheit.html.

Invarianten:

1. Beide Seiten nutzen `_zeitraum_felder.html`; kein Template baut ein
   Datumsfeld mit Uhrzeit-Auswahl von Hand nach.
2. Der Baustein ordnet je Zeile Beschriftung, Datum und Uhrzeit an (Von, Bis);
   die Uhrzeit-Kästchen sind ganztägig ausgeblendet (`hidden`).
3. Die Kennungen, die die Seiten-Skripte schalten, gibt es.

Die Anordnung im Browser (Telefonbreite) prüft
tests/test_ui_browser.py::test_zeitraum_je_zeile_datum_und_uhrzeit.
"""
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

VORLAGEN = Path(__file__).resolve().parent.parent / "app" / "webui" / "templates"
NUTZER = {"self.html": ("von", "bis"), "abwesenheit.html": ("abm-von", "abm-bis")}


def baustein(von: str, bis: str) -> str:
    env = Environment(loader=FileSystemLoader(str(VORLAGEN)), autoescape=True)
    return env.from_string(
        '{% from "_zeitraum_felder.html" import zeitraum_felder %}'
        '{{ zeitraum_felder(von, bis) }}').render(von=von, bis=bis)


def test_beide_seiten_nutzen_den_baustein():
    for name, (von, bis) in NUTZER.items():
        text = (VORLAGEN / name).read_text(encoding="utf-8")
        assert re.search(r'{%-? from "_zeitraum_felder.html" import zeitraum_felder', text), name
        assert f'zeitraum_felder("{von}", "{bis}")' in text, name


def test_kein_nachbau_von_datum_mit_uhrzeit():
    """Geschwister: Ein Template mit Datumsfeld UND eigener Uhrzeit-Auswahl
    (`<select id="…-zeit">`) baut den Baustein nach."""
    funde = []
    for p in sorted(VORLAGEN.glob("*.html")):
        if p.name == "_zeitraum_felder.html":
            continue
        text = p.read_text(encoding="utf-8")
        if 'type="date"' in text and re.search(r'<select id="[\w-]*-zeit"', text):
            funde.append(p.name)
    assert not funde, f"Datum + Uhrzeit von Hand statt _zeitraum_felder.html: {funde}"


def test_je_zeile_beschriftung_datum_uhrzeit():
    html = baustein("von", "bis")
    for feld, text in (("von", "Von"), ("bis", "Bis")):
        zeile = re.search(
            rf'<label for="{feld}"[^>]*>{text}</label>\s*'
            rf'<div style="display:flex[^>]*>\s*'
            rf'<input type="date" id="{feld}"[^>]*>\s*'
            rf'<span id="{feld}-zeit-box" hidden><select id="{feld}-zeit"', html)
        assert zeile, f"{feld}: Datum und Uhrzeit nicht in einer Zeile mit Beschriftung\n{html}"


def test_kennungen_der_seitenskripte_vorhanden():
    for name, (von, bis) in NUTZER.items():
        html = baustein(von, bis)
        skript = (VORLAGEN / name).read_text(encoding="utf-8")
        for feld in (von, bis):
            for kennung in (feld, f"{feld}-zeit", f"{feld}-zeit-box"):
                assert f'id="{kennung}"' in html
            assert f"{feld}-zeit-box" in skript, f"{name} schaltet {feld}-zeit-box nicht"
