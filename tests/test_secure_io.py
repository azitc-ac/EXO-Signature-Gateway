"""secure_io — Schreiben und Härten von Geheimnissen.

Diese Tests bilden die Prüfungen ab, mit denen der Audit vom 2026-07-26
abgesichert wurde. Sie existieren, weil dort mehrere Fehler erst beim
adversarialen Testen auffielen — insbesondere, dass ein pauschales
`chmod(0o600)` certbots 400er-Datei ERWEITERT hätte.
"""
import stat

import pytest

import secure_io
from conftest import mode_of


# ── Schreiben ────────────────────────────────────────────────────────────────

def test_write_secret_setzt_600_und_ordner_700(data_dir):
    p = data_dir / "unterordner" / "key.pem"
    secure_io.write_secret_bytes(p, b"geheim")
    assert p.read_bytes() == b"geheim"
    assert mode_of(p) == "600"
    assert mode_of(p.parent) == "700"


def test_write_secret_ist_atomar_keine_temp_datei_bleibt(data_dir):
    p = data_dir / "key.pem"
    secure_io.write_secret_bytes(p, b"x")
    assert list(data_dir.iterdir()) == [p], "Temp-Datei wurde nicht aufgeräumt"


def test_write_secret_ueberschreibt_und_haelt_rechte(data_dir):
    p = data_dir / "settings.json"
    secure_io.write_secret_text(p, "alt")
    p.chmod(0o644)                       # jemand hat von Hand gelockert
    secure_io.write_secret_text(p, "neu")
    assert p.read_text() == "neu"
    # Der eigentliche Fallstrick: rename() erbt die Rechte der QUELLdatei.
    # Ohne chmod auf der Temp-Datei stünde hier wieder 644.
    assert mode_of(p) == "600"


def test_write_secret_json(data_dir):
    p = data_dir / "orders.json"
    secure_io.write_secret_json(p, {"a": 1, "ü": "ö"})
    assert '"ü": "ö"' in p.read_text()    # ensure_ascii=False
    assert mode_of(p) == "600"


# ── Härten ───────────────────────────────────────────────────────────────────

def test_harden_tree_schliesst_offene_dateien_und_ordner(data_dir):
    key = data_dir / "smime" / "u" / "certs" / "a" / "key.pem"
    key.parent.mkdir(parents=True)
    key.write_text("x")
    key.chmod(0o644)
    for d in (data_dir / "smime", data_dir / "smime/u",
              data_dir / "smime/u/certs", data_dir / "smime/u/certs/a"):
        d.chmod(0o755)

    res = secure_io.harden_tree(data_dir)

    assert res["files"] == 1
    assert mode_of(key) == "600"
    assert mode_of(key.parent) == "700"
    assert mode_of(data_dir / "smime") == "700"


def test_harden_tree_erweitert_niemals_rechte(data_dir):
    """Der wichtigste Test dieser Datei.

    certbot legt `private_key.json` mit 400 ab. Ein pauschales chmod(0o600)
    würde daraus 600 machen — die Härtung hätte die Rechte GELOCKERT. Genau
    dieser Fehler steckte im ersten Entwurf.
    """
    p = data_dir / "le-config" / "accounts" / "x" / "private_key.json"
    p.parent.mkdir(parents=True)
    p.write_text("x")
    p.chmod(0o400)

    secure_io.harden_tree(data_dir)

    assert mode_of(p) == "400", "Härten darf Rechte nur einschränken, nie erweitern"


@pytest.mark.parametrize("vorher,nachher", [
    (0o644, "600"), (0o640, "600"), (0o666, "600"),
    (0o400, "400"), (0o600, "600"), (0o700, "700"),
])
def test_harden_file_entfernt_nur_group_und_other(data_dir, vorher, nachher):
    p = data_dir / "key.pem"
    p.write_text("x")
    p.chmod(vorher)
    secure_io.harden_file(p)
    assert mode_of(p) == nachher


def test_harden_tree_laesst_nicht_geheime_dateien_in_ruhe(data_dir):
    log = data_dir / "logs" / "app.log"
    log.parent.mkdir()
    log.write_text("x")
    log.chmod(0o644)
    (data_dir / "logs").chmod(0o755)

    secure_io.harden_tree(data_dir)

    assert mode_of(log) == "644"
    assert mode_of(data_dir / "logs") == "755"


def test_harden_tree_haertet_ordner_auch_ohne_geaenderte_datei(data_dir):
    """Invariante erzwingen, nicht nur Änderungen nachziehen: ein Ordner mit
    einem bereits korrekten 600er-Schlüssel muss trotzdem auf 700."""
    key = data_dir / "smime" / "key.pem"
    key.parent.mkdir()
    key.write_text("x")
    key.chmod(0o600)
    (data_dir / "smime").chmod(0o755)

    res = secure_io.harden_tree(data_dir)

    assert res["files"] == 0
    assert mode_of(data_dir / "smime") == "700"


def test_harden_tree_ist_idempotent(data_dir):
    key = data_dir / "key.pem"
    key.write_text("x")
    key.chmod(0o644)
    secure_io.harden_tree(data_dir)
    assert secure_io.harden_tree(data_dir) == {"files": 0, "dirs": 0}


def test_harden_tree_laesst_die_wurzel_unangetastet(data_dir):
    """`data/` ist der Einhängepunkt des Bind-Mounts — seine Rechte gehören
    dem Betreiber."""
    (data_dir / "key.pem").write_text("x")
    data_dir.chmod(0o755)
    secure_io.harden_tree(data_dir)
    assert mode_of(data_dir) == "755"


def test_audit_tree_meldet_ohne_zu_aendern(data_dir):
    p = data_dir / "key.pem"
    p.write_text("x")
    p.chmod(0o644)
    offen = secure_io.audit_tree(data_dir)
    assert [x[0] for x in offen] == [str(p)]
    assert mode_of(p) == "644", "audit_tree darf nichts verändern"
    secure_io.harden_tree(data_dir)
    assert secure_io.audit_tree(data_dir) == []


# ── Pfadsicherheit ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("rel", [
    "../evil.txt",
    "../data-evil/x",          # Präfixvergleich auf Zeichenketten liess das durch
    "../../etc/cron.d/x",
    "a/../../ausserhalb",
])
def test_safe_join_blockiert_ausbrueche(tmp_path, rel):
    base = tmp_path / "data"
    base.mkdir()
    assert secure_io.safe_join(base, rel) is None


@pytest.mark.parametrize("rel", ["smime/u/key.pem", "a.json", "a/b/c/d.pem"])
def test_safe_join_laesst_legitime_pfade_durch(tmp_path, rel):
    base = tmp_path / "data"
    base.mkdir()
    ziel = secure_io.safe_join(base, rel)
    assert ziel is not None
    assert ziel.is_relative_to(base.resolve())


def test_safe_join_gegen_geschwister_mit_gleichem_praefix(tmp_path):
    """Der konkrete Defekt: `startswith('/…/data')` trifft auch '/…/data-evil'."""
    base = tmp_path / "data"
    base.mkdir()
    (tmp_path / "data-evil").mkdir()
    assert secure_io.safe_join(base, "../data-evil/x") is None
