"""Eine fehlende Nur-Text-Fassung ist sichtbar, nicht nur protokolliert.

ANLASS (24.08.2026)
-------------------
Im Protokoll der Produktions-VM stand 16-mal:

    signature_engine: Template Blog-Banner-Orange.txt not found,
    falling back to signature.txt

`Blog-Banner-Orange.html` lag vor, die zugehörige `.txt` nicht. Wer diese
Vorlage zugewiesen bekam, trug im Nur-Text-Teil seiner Nachrichten die
STANDARDSIGNATUR — nicht die zugewiesene. Bemerkt hat das niemand: Der
Bearbeiter sieht im Editor ein leeres Textfeld, und ein leeres Feld sieht aus
wie eine Entscheidung.

Dieselbe Bauart wie die Fälle in CLAUDE.md Regel 8: Ein Ersatz greift still,
sein Wirken ist nirgends sichtbar, also fällt er nicht auf. Die Warnung im
Protokoll erreicht niemanden, der Vorlagen pflegt.

Der Rückfall selbst bleibt — er ist richtig, eine Nachricht ohne Textteil wäre
schlechter. Neu ist allein, dass er dort steht, wo man ihn herbeiführt.
"""
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import signature_engine  # noqa: E402


@pytest.fixture
def vorlagenordner(tmp_path, monkeypatch):
    """Vorlagenverzeichnis umlenken — und die Jinja-Umgebung mitnehmen.

    ⚠️ `_env` wird beim ersten Rendern gebaut und hält seinen `FileSystemLoader`
    samt Pfad fest. Ein blosser Patch von `TEMPLATE_DIR` wirkt deshalb NICHT,
    sobald irgendein früherer Test schon einmal gerendert hat. Genau daran ist
    dieser Test zuerst gescheitert: allein grün, im vollen Lauf rot — der
    unangenehmste Fall, weil ein isoliert grüner Test nichts beweist.

    `monkeypatch.setattr(..., "_env", None)` erzwingt den Neubau und stellt
    hinterher den vorherigen Stand wieder her, sodass die Reihenfolge auch für
    die nachfolgenden Tests ohne Belang bleibt.
    """
    monkeypatch.setattr(signature_engine.config, "TEMPLATE_DIR", str(tmp_path))
    monkeypatch.setattr(signature_engine, "_env", None)
    return tmp_path


def test_meldet_fehlende_textfassung(vorlagenordner):
    (vorlagenordner / "Blog-Banner-Orange.html").write_text("<p>Banner</p>", encoding="utf-8")
    assert signature_engine.textfassung_fehlt("Blog-Banner-Orange"), (
        "Ohne .txt fällt render() auf signature.txt zurück — das muss die "
        "Oberfläche sagen können.")


def test_schweigt_wenn_die_textfassung_da_ist(vorlagenordner):
    (vorlagenordner / "Blog-Banner.html").write_text("<p>Banner</p>", encoding="utf-8")
    (vorlagenordner / "Blog-Banner.txt").write_text("Banner", encoding="utf-8")
    assert not signature_engine.textfassung_fehlt("Blog-Banner")


def test_leere_datei_zaehlt_als_vorhanden(vorlagenordner):
    """Wer die Datei anlegt und leer lässt, hat entschieden — das ist etwas
    anderes als eine Datei, die es nie gab."""
    (vorlagenordner / "Leer.html").write_text("<p>x</p>", encoding="utf-8")
    (vorlagenordner / "Leer.txt").write_text("", encoding="utf-8")
    assert not signature_engine.textfassung_fehlt("Leer")


@pytest.mark.parametrize("name", ["", "default"])
def test_standardsignatur_liegt_unter_signature(vorlagenordner, name):
    """„default" ist der Anzeigename, die Datei heisst `signature.txt`.

    Ohne diese Übersetzung meldete die Prüfung die Standardsignatur selbst als
    unvollständig — und ausgerechnet die ist das Ziel des Rückfalls.
    """
    (vorlagenordner / "signature.txt").write_text("Gruss", encoding="utf-8")
    assert not signature_engine.textfassung_fehlt(name)


def test_render_faellt_zurueck_und_sagt_es(vorlagenordner, caplog):
    """Der Rückfall bleibt bestehen — geprüft, damit ihn niemand „mitfixt"."""
    (vorlagenordner / "signature.html").write_text("<p>Standard</p>", encoding="utf-8")
    (vorlagenordner / "signature.txt").write_text("Standard-Text", encoding="utf-8")
    (vorlagenordner / "NurHtml.html").write_text("<p>Besonders</p>", encoding="utf-8")

    class _User:
        custom = {}
        def __getattr__(self, _):
            return ""

    with caplog.at_level("WARNING"):
        html, txt = signature_engine.render(_User(), "NurHtml")
    assert "Besonders" in html
    assert "Standard-Text" in txt, "Der Rückfall auf signature.txt muss greifen"
    assert "NurHtml.txt" in caplog.text, "und er muss protokolliert werden"
