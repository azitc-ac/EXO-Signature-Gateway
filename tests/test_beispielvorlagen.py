"""Beispielvorlagen für die Webseite (contrib/beispielvorlagen, seit v1.12.126).

Das Paket ist ein Versprechen nach aussen: Es muss sich importieren lassen,
rendern, und HTML/Text müssen zur Bausteinliste passen — sonst zeigt der
Baukasten nach dem Import etwas anderes, als versandt wird. Wer die Bausteine
in erzeugen.py ändert, ohne neu zu erzeugen, fällt hier auf."""
import json
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))
import config  # noqa: E402
import signature_engine  # noqa: E402
import template_builder as tb  # noqa: E402
import vorlagen_paket as vp  # noqa: E402

PAKET = WURZEL / "contrib" / "beispielvorlagen"
NAMEN = sorted(p.name[:-len(".meta.json")] for p in (PAKET / "templates").glob("*.meta.json"))


def test_paket_ist_vollstaendig():
    assert len(NAMEN) >= 8, NAMEN
    for n in NAMEN:
        for endung in (".html", ".txt"):
            assert (PAKET / "templates" / f"{n}{endung}").exists(), (n, endung)
        assert (PAKET / "vorschau" / f"{n}.png").exists(), n
        assert " " not in n


@pytest.mark.parametrize("name", NAMEN)
def test_html_und_text_passen_zur_bausteinliste(name):
    meta = json.loads((PAKET / "templates" / f"{name}.meta.json").read_text(encoding="utf-8"))
    assert meta.get("text_eigen") is False          # Textfassung bleibt automatisch
    assert (PAKET / "templates" / f"{name}.html").read_text(encoding="utf-8") == tb.render_html(meta)
    assert (PAKET / "templates" / f"{name}.txt").read_text(encoding="utf-8") == tb.render_txt(meta)


def test_import_ohne_fehler_und_renderbar(tmp_path, monkeypatch):
    import zipfile
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    (tmp_path / "signature.html").write_text("<p>x</p>", encoding="utf-8")
    with zipfile.ZipFile(PAKET / "beispielvorlagen.zip") as z:
        eingaben = [(i.filename, z.read(i)) for i in z.infolist()]
    gelesen = vp.lesen(eingaben)
    assert [v["name"] for v in gelesen["vorlagen"] if v["fehler"]] == []
    assert sorted(v["name"] for v in gelesen["vorlagen"]) == NAMEN
    vp.importieren(eingaben, {n: {"aktion": "neu"} for n in NAMEN}, "test")
    from graph_client import UserData
    person = UserData(displayName="Max Mustermann", mail="max@example.com", phone="+49 1")
    for n in NAMEN:
        assert signature_engine.render_fehler(person, n) is None, n


def test_neutral_keine_echten_namen():
    verboten = ("contoso", "fabrikam", "azitc", "zarenko")
    for f in (PAKET / "templates").iterdir():
        text = f.read_text(encoding="utf-8").lower()
        assert not any(v in text for v in verboten), f.name
