"""Vorlagen importieren/exportieren (vorlagen_paket.py, seit v1.12.123).

Geprüft wird, was ein Paket aus dem Netz nicht darf: Pfade ausserhalb des
Vorlagenordners erreichen, still überschreiben, den Speicher fluten, kaputte
oder fremdartige Vorlagen einschleusen."""
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import config  # noqa: E402
import vorlagen_paket as vp  # noqa: E402

META = json.dumps({"version": 1, "kind": "signatur", "blocks": [{"type": "name"}]}).encode()


@pytest.fixture
def verz(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    (tmp_path / "signature.html").write_text("<p>Standard</p>", encoding="utf-8")
    return tmp_path


def _zip(dateien: dict[str, bytes]) -> bytes:
    p = io.BytesIO()
    with zipfile.ZipFile(p, "w") as z:
        for n, i in dateien.items():
            z.writestr(n, i)
    return p.getvalue()


def _eingaben(dateien):
    return vp.eingaben_aus_upload([("paket.zip", _zip(dateien))])


def test_nur_geprüfte_namen_kein_pfad_aus_dem_paket(verz):
    e = _eingaben({"templates/Gut.html": b"<p>{{ user.displayName }}</p>",
                   "templates/../Boese.html": b"x", "templates/sub/Tief.html": b"x",
                   "/abs.html": b"x", "templates/Leer Zeichen.html": b"x",
                   "templates/Skript.js": b"x"})
    r = vp.lesen(e)
    assert [v["name"] for v in r["vorlagen"]] == ["Gut"]
    assert len(r["uebersprungen"]) == 5, r["uebersprungen"]
    gruende = {u["datei"]: u["grund"] for u in r["uebersprungen"]}
    assert "Unterordner" in gruende["templates/sub/Tief.html"]          # Grund, den die Auswahl zeigt
    assert "Unterordner" in gruende["templates/../Boese.html"]
    assert "keine Vorlagendatei" in gruende["templates/Skript.js"]
    ent = {"Gut": {"aktion": "neu"}, "../Boese": {"aktion": "neu"}, "Boese": {"aktion": "neu"}}
    vp.importieren(e, ent, "test")
    assert sorted(p.name for p in verz.iterdir()) == ["Gut.html", "signature.html"]
    assert not (verz.parent / "Boese.html").exists()


def test_vorhandene_werden_nie_still_ueberschrieben(verz):
    (verz / "Kompakt.html").write_text("ALT", encoding="utf-8")
    e = _eingaben({"templates/Kompakt.html": b"NEU"})
    r = vp.lesen(e)
    assert r["vorlagen"][0]["vorhanden"] is True
    erg = vp.importieren(e, {"Kompakt": {"aktion": "neu"}}, "t")["ergebnis"][0]
    assert erg["ok"] is False and (verz / "Kompakt.html").read_text() == "ALT"
    # Ohne Entscheidung: auslassen
    assert vp.importieren(e, {}, "t")["ergebnis"] == []
    assert (verz / "Kompakt.html").read_text() == "ALT"


def test_umbenennen_und_ueberschreiben_mit_sicherung(verz):
    (verz / "Kompakt.html").write_text("ALT", encoding="utf-8")
    (verz / "Kompakt.txt").write_text("ALT-TEXT", encoding="utf-8")
    e = _eingaben({"templates/Kompakt.html": b"NEU", "templates/Kompakt.meta.json": META})
    assert vp.freier_name("Kompakt") == "Kompakt-2"
    r = vp.importieren(e, {"Kompakt": {"aktion": "umbenennen", "ziel": "Kompakt-2"}}, "t")
    assert r["ergebnis"][0]["ok"] and (verz / "Kompakt-2.html").read_text() == "NEU"
    assert (verz / "Kompakt.html").read_text() == "ALT"
    # Ziel belegt → abgelehnt
    r = vp.importieren(e, {"Kompakt": {"aktion": "umbenennen", "ziel": "Kompakt-2"}}, "t")
    assert r["ergebnis"][0]["ok"] is False
    r = vp.importieren(e, {"Kompakt": {"aktion": "ueberschreiben"}}, "t")
    assert r["ergebnis"][0]["ok"]
    assert (verz / "Kompakt.html").read_text() == "NEU"
    assert (verz / "Kompakt.html.bak").read_text() == "ALT"
    # Die alte Textfassung gehörte zur alten Vorlage — sie darf nicht bleiben.
    assert not (verz / "Kompakt.txt").exists()
    assert (verz / "Kompakt.meta.json").read_bytes() == META


def test_standardvorlage_und_reservierte_namen(verz):
    e = _eingaben({"templates/signature.html": b"NEU", "templates/Standard.html": b"X"})
    r = {v["name"]: v for v in vp.lesen(e)["vorlagen"]}
    assert r["default"]["vorhanden"] and r["Standard"]["reserviert"]
    assert vp.freier_name("default") == "Standard-2"
    erg = vp.importieren(e, {"default": {"aktion": "neu"}, "Standard": {"aktion": "neu"}}, "t")["ergebnis"]
    assert [x["ok"] for x in erg] == [False, False]
    erg = vp.importieren(e, {"Standard": {"aktion": "ueberschreiben"}}, "t")["ergebnis"]
    assert erg[0]["ok"] is False and not (verz / "Standard.html").exists()
    erg = vp.importieren(e, {"default": {"aktion": "umbenennen", "ziel": "standard"}}, "t")["ergebnis"]
    assert erg[0]["ok"] is False                       # Ziel ebenfalls vorbehalten
    assert (verz / "signature.html").read_text() == "<p>Standard</p>"


def test_kaputte_und_fremdartige_vorlagen(verz):
    e = _eingaben({"templates/Kaputt.html": b"{% if %}", "templates/Ohne.txt": b"nur Text",
                   "templates/Post.html": b"x",
                   "templates/Post.meta.json": json.dumps({"kind": "usermail", "blocks": []}).encode(),
                   "templates/Json.html": b"x", "templates/Json.meta.json": b"{kaputt",
                   "templates/Latin.html": "Grüße".encode("latin-1")})
    r = {v["name"]: v["fehler"] for v in vp.lesen(e)["vorlagen"]}
    assert all(r.values()), r
    vp.importieren(e, {n: {"aktion": "neu"} for n in r}, "t")
    assert sorted(p.name for p in verz.iterdir()) == ["signature.html"]


def test_grenzen_gegen_zip_bomben(verz):
    gross = b"0" * (vp.MAX_DATEI + 1)
    with pytest.raises(vp.PaketFehler):
        _eingaben({"templates/Gross.html": gross})
    with pytest.raises(vp.PaketFehler):
        _eingaben({f"templates/V{i}.html": b"x" for i in range(vp.MAX_DATEIEN + 1)})
    with pytest.raises(vp.PaketFehler):
        vp.eingaben_aus_upload([("x.zip", b"kein zip")])


def test_einzelne_dateien_statt_zip(verz):
    e = vp.eingaben_aus_upload([("C:\\\\Downloads\\\\Neu.html", b"<p>x</p>"), ("Neu.txt", b"x")])
    r = vp.lesen(e)["vorlagen"]
    assert r[0]["name"] == "Neu" and r[0]["dateien"] == ["Neu.html", "Neu.txt"]


def test_export_und_reimport_sind_gleich(verz, monkeypatch):
    (verz / "Rund.html").write_text("<p>R</p>", encoding="utf-8")
    (verz / "Rund.meta.json").write_bytes(META)
    (verz / "Rund.html.bak").write_text("alt", encoding="utf-8")
    daten = vp.exportieren(["Rund", "default", "../x", "Fehlt"])
    with zipfile.ZipFile(io.BytesIO(daten)) as z:
        assert sorted(z.namelist()) == ["LIESMICH.txt", "templates/Rund.html",
                                        "templates/Rund.meta.json", "templates/signature.html"]
    ziel = verz / "neu"
    ziel.mkdir()
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(ziel))
    e = vp.eingaben_aus_upload([("v.zip", daten)])
    vp.importieren(e, {"Rund": {"aktion": "neu"}}, "t")
    assert (ziel / "Rund.meta.json").read_bytes() == META
    assert (ziel / "Rund.html").read_text() == "<p>R</p>"


def test_grenzen_gelten_ueber_mehrere_zips(verz):
    """Review 20.11.: Die Grenzen galten je ZIP — 5 ZIPs ergaben 90 MB entpackt."""
    eins = io.BytesIO()
    with zipfile.ZipFile(eins, "w") as z:
        for i in range(9):
            z.writestr(f"templates/V{i}.html", b"0" * (vp.MAX_DATEI - 10))
    with pytest.raises(vp.PaketFehler):
        vp.eingaben_aus_upload([(f"p{i}.zip", eins.getvalue()) for i in range(3)])


def test_name_mit_zeilenumbruch_und_nutzermails_bleiben_draussen(verz):
    (verz / "usermail_cert_ready.html").write_text("<p>angepasst</p>", encoding="utf-8")
    e = _eingaben({"templates/abc\n.html": b"x", "templates/usermail_cert_ready.html": b"<p>boese</p>",
                   "templates/default.html": b"<p>d</p>"})
    r = vp.lesen(e)
    assert [v["name"] for v in r["vorlagen"]] == ["default"] and r["vorlagen"][0]["reserviert"]
    gruende = {u["datei"]: u["grund"] for u in r["uebersprungen"]}
    assert "Postfachinhaber" in gruende["templates/usermail_cert_ready.html"]
    assert "templates/abc\n.html" in gruende
    vp.importieren(e, {"usermail_cert_ready": {"aktion": "ueberschreiben"},
                       "default": {"aktion": "ueberschreiben"}}, "t")
    assert (verz / "usermail_cert_ready.html").read_text() == "<p>angepasst</p>"
    assert (verz / "signature.html").read_text() == "<p>Standard</p>"
    erg = vp.importieren(e, {"default": {"aktion": "umbenennen", "ziel": "usermail_x"}}, "t")["ergebnis"]
    assert erg[0]["ok"] is False


def test_ueberschreiben_sichert_auch_baukasten_und_text(verz):
    (verz / "K.html").write_text("ALT", encoding="utf-8")
    (verz / "K.meta.json").write_bytes(META)
    (verz / "K.txt").write_text("ALT-TEXT", encoding="utf-8")
    vp.importieren(_eingaben({"templates/K.html": b"NEU"}), {"K": {"aktion": "ueberschreiben"}}, "t")
    assert (verz / "K.meta.json.bak").read_bytes() == META
    assert (verz / "K.txt.bak").read_text() == "ALT-TEXT"
