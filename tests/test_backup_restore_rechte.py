"""Wiederherstellung darf Rechte nie lockern.

ANLASS (07.08.2026)
Beim Wiederherstellungspfad war die Schleife über `data/` bereits auf
`secure_io.write_secret_bytes()` umgestellt worden — mit ausdrücklichem
Kommentar, dass sonst Privatschlüssel mit 644 zurückkämen. `settings.json`
wird aber NACHGELAGERT geschrieben (sie ist der Konsistenz-Anker und soll
zuletzt entstehen), und dieser eine Aufruf blieb bei `write_bytes()`.

Warum es nie auffiel: `write_bytes()` übernimmt die Rechte einer BESTEHENDEN
Datei. Auf einem eingerichteten Gateway lag settings.json bereits mit 600 da,
also blieb es dabei. Nur wenn die Datei fehlt — der Wiederherstellungsfall auf
einem frischen System, also der einzige, auf den es ankommt — entsteht sie mit
umask-Rechten.

settings.json enthält CLIENT_SECRET.
"""
import io
import json
import zipfile

import pytest


@pytest.fixture
def bm(tmp_path, monkeypatch, data_dir):
    """backup_manager gegen ein Wegwerf-Verzeichnis, nie gegen /app/data."""
    import backup_manager
    import settings_store
    vorlagen = tmp_path / "templates"
    vorlagen.mkdir()
    monkeypatch.setattr(backup_manager, "DATA_DIR", data_dir)
    monkeypatch.setattr(backup_manager, "TEMPLATE_DIR", vorlagen)
    # Der Wiederherstellungspfad lädt am Ende die Einstellungen neu. Das würde
    # gegen das ECHTE Datenverzeichnis laufen.
    monkeypatch.setattr(settings_store, "init", lambda *a, **k: None)
    return backup_manager, data_dir, vorlagen


def _zip(eintraege: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, inhalt in eintraege.items():
            zf.writestr(name, inhalt)
    return buf.getvalue()


def test_settings_json_entsteht_mit_600(bm):
    """Frisches System: die Datei gibt es noch nicht."""
    from conftest import mode_of
    backup_manager, daten, _ = bm
    assert not (daten / "settings.json").exists(), "Vorbedingung: Datei fehlt"

    r = backup_manager.restore_backup(_zip({
        "data/settings.json": json.dumps({"CLIENT_SECRET": "geheim"}),
    }))

    assert r["ok"], r.get("error")
    ziel = daten / "settings.json"
    assert ziel.exists(), "nicht wiederhergestellt"
    assert mode_of(ziel) == "600", \
        f"CLIENT_SECRET liegt mit {mode_of(ziel)} statt 600"
    assert json.loads(ziel.read_text())["CLIENT_SECRET"] == "geheim"


def test_privatschluessel_entstehen_mit_600(bm):
    """Die Schleife über data/ — war schon richtig, bleibt geprüft."""
    from conftest import mode_of
    backup_manager, daten, _ = bm

    r = backup_manager.restore_backup(_zip({
        "data/settings.json": "{}",
        "data/acme/account_key.pem": "-----BEGIN PRIVATE KEY-----",
        "data/auth.pfx": "PFX",
    }))

    assert r["ok"], r.get("error")
    assert mode_of(daten / "acme" / "account_key.pem") == "600"
    assert mode_of(daten / "auth.pfx") == "600"


def test_vorlagen_bleiben_lesbar(bm):
    """Gegenprobe: Vorlagen sind KEINE Geheimnisse.

    Würde man sie auch über `write_secret_bytes()` schreiben, zöge das das
    Vorlagenverzeichnis auf 700 — der Betreiber bearbeitet die Dateien aber
    vom Host aus. Die Unterscheidung ist beabsichtigt und soll so bleiben.
    """
    from conftest import mode_of
    backup_manager, _daten, vorlagen = bm

    r = backup_manager.restore_backup(_zip({
        "data/settings.json": "{}",
        "templates/Probe.html": "<p>x</p>",
    }))

    assert r["ok"], r.get("error")
    assert (vorlagen / "Probe.html").read_text() == "<p>x</p>"
    assert mode_of(vorlagen) != "700", \
        "das Vorlagenverzeichnis wurde wie ein Geheimnisordner behandelt"


def test_baukasten_daten_liegen_im_backup(tmp_path, monkeypatch, data_dir):
    """Ein Backup ohne .meta.json ist nur die halbe Vorlage.

    Wiederhergestellt kaeme das erzeugte HTML zurueck, aber nicht die
    Bausteine. Der Baukasten muesste sie aus dem HTML zurueckuebersetzen —
    und das ist ausdruecklich verlustbehaftet.
    """
    import zipfile
    import backup_manager
    vorlagen = tmp_path / "templates"
    vorlagen.mkdir()
    (vorlagen / "Blog-Banner.html").write_text("<p>x</p>")
    (vorlagen / "Blog-Banner.txt").write_text("x")
    (vorlagen / "Blog-Banner.meta.json").write_text('{"version": 1, "blocks": []}')
    (vorlagen / "Blog-Banner.html.bak").write_text("<p>alt</p>")
    (data_dir / "settings.json").write_text("{}")
    monkeypatch.setattr(backup_manager, "DATA_DIR", data_dir)
    monkeypatch.setattr(backup_manager, "TEMPLATE_DIR", vorlagen)

    roh, _name = backup_manager.create_backup()
    with zipfile.ZipFile(io.BytesIO(roh)) as zf:
        namen = set(zf.namelist())

    assert "templates/Blog-Banner.meta.json" in namen, "Baukasten-Daten fehlen"
    assert "templates/Blog-Banner.html" in namen
    assert "templates/Blog-Banner.txt" in namen
    assert "templates/Blog-Banner.html.bak" not in namen, "Zwischenstand mitgesichert"
