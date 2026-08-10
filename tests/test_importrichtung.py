"""Die Importrichtung der Weboberfläche — durchgesetzt, nicht nur vermerkt.

WOFÜR
-----
Die Aufteilung von `app.py` steht und fällt mit einer Richtung:

    app.py  ──bindet ein──>  routen/*.py  ──importieren──>  deps.py, hilfen.py

Wird sie verletzt, gibt es zwei Ausgänge, und beide sind unangenehm. Ein echter
Ringschluss (`routen/x.py` importiert `webui.app`) fällt erst beim Start auf,
also im Betrieb statt beim Schreiben. Der Rückimport (`app.py` holt einen Namen
aus einem Routenmodul) läuft dagegen klaglos — und ist genau deshalb gefährlich:
Er macht `app.py` von seinen eigenen Bausteinen abhängig und dreht die Richtung
für jeden um, der sie danach liest.

ANLASS
------
Beim Add-in-Modul blieb ein solcher Rückimport stehen (`_addin_base_url`), weil
die Einstellungsseite die Funktion ebenfalls braucht. Er wurde als Unsauberkeit
notiert — und stand beim S/MIME-Modul prompt ein zweites Mal an
(`_cert_expiry`). Ein zweiter Einzelfall ist kein Einzelfall mehr, sondern ein
Muster: Beide Helfer liegen jetzt in `hilfen.py`, und diese Datei sorgt dafür,
dass es dabei bleibt.

Ein Befund ohne Durchsetzung wiederholt sich. Deshalb eine Prüfung statt eines
Absatzes in der Übergabe.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

WEBUI = Path(__file__).resolve().parent.parent / "app" / "webui"
FUNDAMENT = ("deps.py", "hilfen.py")


def _importe(datei: Path) -> list[tuple[str, str, int]]:
    """(modul, name, zeile) für jeden Import der Datei — auch für lokale.

    Lokale Importe innerhalb einer Funktion sind hier ausdrücklich mitgemeint:
    Ein Ringschluss, den jemand in eine Funktion verlegt, ist genau derselbe
    Fehler, nur später sichtbar.
    """
    baum = ast.parse(datei.read_text())
    treffer = []
    for knoten in ast.walk(baum):
        if isinstance(knoten, ast.Import):
            for a in knoten.names:
                treffer.append((a.name, "", knoten.lineno))
        elif isinstance(knoten, ast.ImportFrom) and knoten.module:
            for a in knoten.names:
                treffer.append((knoten.module, a.name, knoten.lineno))
    return treffer


def _routenmodule() -> list[Path]:
    return sorted(p for p in (WEBUI / "routen").glob("*.py")
                  if p.name != "__init__.py")


def test_es_gibt_ueberhaupt_routenmodule():
    """Sonst liefen die Prüfungen darunter über eine leere Liste und wären grün,
    ohne etwas geprüft zu haben."""
    assert _routenmodule(), "keine Routenmodule gefunden — stimmt der Pfad noch?"


@pytest.mark.parametrize("name", FUNDAMENT)
def test_fundament_kennt_weder_app_noch_routen(name):
    """`deps.py` und `hilfen.py` liegen UNTER allem anderen.

    Griffen sie nach oben, wäre der Ringschluss vollständig: `app.py` bindet
    Routenmodule ein, die das Fundament importieren, das wieder `app.py` lädt.
    """
    datei = WEBUI / name
    verstoesse = [f"{name}:{zeile} → {modul}"
                  for modul, _, zeile in _importe(datei)
                  if modul == "webui.app" or modul.startswith("webui.routen")]
    assert not verstoesse, (
        "Das Fundament darf nichts aus der Anwendung oder den Routenmodulen "
        "holen:\n  " + "\n  ".join(verstoesse))


def test_routenmodule_kennen_app_py_nicht():
    """Ein Routenmodul importiert `deps`/`hilfen` — niemals `webui.app`."""
    verstoesse = [f"{p.name}:{zeile} → {modul}"
                  for p in _routenmodule()
                  for modul, _, zeile in _importe(p)
                  if modul == "webui.app"]
    assert not verstoesse, (
        "Ringschluss: `app.py` bindet diese Module ein, sie dürfen es nicht "
        "ihrerseits importieren:\n  " + "\n  ".join(verstoesse))


def test_app_py_holt_keine_namen_aus_routenmodulen():
    """`app.py` darf Routenmodule EINBINDEN, aber nichts aus ihnen entnehmen.

    Erlaubt:  `from webui.routen import smime as _routen_smime`
    Verboten: `from webui.routen.smime import _irgendein_helfer`

    Wird ein Helfer an zwei Stellen gebraucht, gehört er nach `hilfen.py`.
    Der Unterschied ist im Syntaxbaum sauber zu greifen: Beim erlaubten Fall
    ist das Modul exakt `webui.routen`, beim verbotenen `webui.routen.<etwas>`.
    """
    verstoesse = [f"app.py:{zeile} → from {modul} import {name}"
                  for modul, name, zeile in _importe(WEBUI / "app.py")
                  if modul.startswith("webui.routen.")]
    assert not verstoesse, (
        "`app.py` entnimmt Namen aus einem Routenmodul — falsche Richtung.\n  "
        + "\n  ".join(verstoesse)
        + "\n\nGeteilte Helfer gehören nach `webui/hilfen.py`.")
