"""Backup mit optionalem Passwort (v1.11.2).

Das ZIP enthält alle Geheimnisse des Gateways im Klartext. Am 08.11.2026 war
ein Pflicht-Passwort bewusst verworfen worden (vergessenes Passwort = wertlose
Sicherung); jetzt ist es ein Angebot. Geprüft:
1. mit Passwort ist die Datei kein ZIP und ohne Passwort nicht lesbar,
2. falsches Passwort und veränderter Kopf scheitern laut, nicht still,
3. ohne Passwort bleibt alles wie bisher (auch alte Backups),
4. das Passwort steht in keiner URL; die Datei ist nur einmal abholbar.
"""
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

import backup_manager

GEHEIM = b"-----BEGIN PRIVATE KEY-----geheim"


@pytest.fixture
def bm(tmp_path, monkeypatch, data_dir):
    import settings_store
    vorlagen = tmp_path / "templates"
    vorlagen.mkdir()
    (data_dir / "settings.json").write_text(json.dumps({"REINJECT_MODE": "graph"}))
    (data_dir / "smime").mkdir()
    (data_dir / "smime" / "key.pem").write_bytes(GEHEIM)
    monkeypatch.setattr(backup_manager, "DATA_DIR", data_dir)
    monkeypatch.setattr(backup_manager, "TEMPLATE_DIR", vorlagen)
    monkeypatch.setattr(backup_manager, "_SCRYPT", {"n": 2 ** 10, "r": 8, "p": 1})  # schnell
    monkeypatch.setattr(settings_store, "init", lambda *a, **k: None)
    return backup_manager


def test_mit_passwort_verschluesselt_und_zurueck(bm):
    daten, name = bm.create_backup("richtig-langes-pw")
    assert name.endswith(".gwbak") and bm.ist_verschluesselt(daten)
    assert GEHEIM not in daten
    with pytest.raises(zipfile.BadZipFile):
        zipfile.ZipFile(io.BytesIO(daten))
    zip_bytes = bm.oeffnen(daten, "richtig-langes-pw")
    assert zipfile.ZipFile(io.BytesIO(zip_bytes)).read("data/smime/key.pem") == GEHEIM


def test_ohne_passwort_wie_bisher(bm):
    daten, name = bm.create_backup()
    assert name.endswith(".zip") and not bm.ist_verschluesselt(daten)
    assert bm.oeffnen(daten, "") == daten
    assert bm.oeffnen(daten, "irgendwas") == daten, "Passwort bei ZIP schadet nicht"


@pytest.mark.parametrize("pw", ["", "falsches-passwort"])
def test_falsches_oder_fehlendes_passwort(bm, pw):
    daten, _ = bm.create_backup("richtig-langes-pw")
    with pytest.raises(bm.PasswortFehler):
        bm.oeffnen(daten, pw)


def test_veraenderter_kopf_oder_inhalt_scheitert(bm):
    daten, _ = bm.create_backup("richtig-langes-pw")
    a = len(bm.MAGIE)
    salz_kaputt = daten[:a] + bytes([daten[a] ^ 1]) + daten[a + 1:]
    inhalt_kaputt = daten[:-1] + bytes([daten[-1] ^ 1])
    for kaputt in (salz_kaputt, inhalt_kaputt):
        with pytest.raises(bm.PasswortFehler):
            bm.oeffnen(kaputt, "richtig-langes-pw")


def test_zu_kurzes_passwort_wird_abgelehnt(bm):
    with pytest.raises(bm.PasswortFehler):
        bm.create_backup("kurz")


# ── Über die Routen ──────────────────────────────────────────────────────────

@pytest.fixture
def client(bm):
    from webui.app import app
    from webui import deps
    app.dependency_overrides[deps._require_admin] = lambda: "admin"
    yield TestClient(app)
    app.dependency_overrides.pop(deps._require_admin, None)


def test_erstellen_abholen_genau_einmal(client):
    r = client.post("/api/backup/erstellen", json={"passwort": "richtig-langes-pw"})
    assert r.status_code == 200, r.text
    abruf = r.json()["abruf"]
    assert "richtig" not in abruf
    d = client.get(f"/api/backup/download?abruf={abruf}")
    assert d.status_code == 200 and backup_manager.ist_verschluesselt(d.content)
    assert ".gwbak" in d.headers["content-disposition"]
    assert client.get(f"/api/backup/download?abruf={abruf}").status_code == 404


def test_erstellen_ohne_passwort_abgelehnt(client):
    assert client.post("/api/backup/erstellen", json={"passwort": ""}).status_code == 400


def test_ansehen_meldet_passwort_noetig_und_oeffnet_mit_passwort(client, bm):
    daten, _ = bm.create_backup("richtig-langes-pw")
    ohne = client.post("/api/backup/inspect", files={"file": ("b.gwbak", daten)}).json()
    assert not ohne["ok"] and ohne["passwort_noetig"]
    mit = client.post("/api/backup/inspect", files={"file": ("b.gwbak", daten)},
                      data={"passwort": "richtig-langes-pw"}).json()
    assert mit["ok"], mit


def test_wiederherstellen_mit_falschem_passwort_schreibt_nichts(client, bm, data_dir):
    daten, _ = bm.create_backup("richtig-langes-pw")
    (data_dir / "smime" / "key.pem").write_bytes(b"aktuell")
    r = client.post("/api/backup/restore", files={"file": ("b.gwbak", daten)},
                    data={"passwort": "falsches-passwort"}).json()
    assert not r["ok"] and r["passwort_noetig"]
    assert (data_dir / "smime" / "key.pem").read_bytes() == b"aktuell"
