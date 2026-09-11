"""Sende-Identitäten — Anlage, Anmeldung und die Absender-Grenze.

Diese Tests MÜSSEN fehlschlagen, wenn:
  - ein Passwort im Klartext (statt als Hash) gespeichert würde,
  - `pruefe_login` ein falsches Passwort / inaktives Login durchließe,
  - die Absender-Grenze (`pruefe`) ein Login als fremden Absender einliefern ließe,
  - die Speicherdatei nicht 600 wäre (sie enthält Passwort-Hashes).
"""
import sys
import stat
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import sende_identitaeten as si


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(si, "PFAD", tmp_path / "sende_identitaeten.json")
    monkeypatch.setattr(si, "_cache", None)
    yield


# ── Anlage ────────────────────────────────────────────────────────────────────

def test_anlegen_und_liste_ohne_hash(store):
    rec = si.anlegen("Kundendienst", "KD@Firma.de", "geheim123", absender="KD@Firma.de")
    assert rec["login"] == "kd@firma.de"          # normalisiert (lowercase)
    assert rec["absender"] == "kd@firma.de"
    assert "passwort_hash" not in rec             # öffentliche Sicht ohne Hash
    liste = si.liste()
    assert len(liste) == 1
    assert all("passwort_hash" not in r for r in liste)


def test_passwort_wird_gehasht_nicht_im_klartext(store):
    si.anlegen("X", "x@f.de", "supersecret1")
    roh = (si.PFAD).read_text("utf-8")
    assert "supersecret1" not in roh              # niemals Klartext auf Platte
    assert "pbkdf2:sha256:" in roh                # als Hash gespeichert


def test_doppeltes_login_abgelehnt(store):
    si.anlegen("A", "gleich@f.de", "passwort1")
    with pytest.raises(ValueError):
        si.anlegen("B", "GLEICH@f.de", "passwort2")   # case-insensitiv derselbe


def test_zu_kurzes_passwort_abgelehnt(store):
    with pytest.raises(ValueError):
        si.anlegen("A", "a@f.de", "kurz")


def test_speicherdatei_ist_600(store):
    si.anlegen("A", "a@f.de", "passwort1")
    modus = stat.S_IMODE(si.PFAD.stat().st_mode)
    assert modus == 0o600, f"Speicherdatei hat {oct(modus)}, erwartet 0o600"


# ── Anmeldung ─────────────────────────────────────────────────────────────────

def test_pruefe_login_erfolg_und_fehler(store):
    si.anlegen("A", "a@f.de", "passwort1")
    assert si.pruefe_login("a@f.de", "passwort1") is not None
    assert si.pruefe_login("A@F.DE", "passwort1") is not None   # login case-insensitiv
    assert si.pruefe_login("a@f.de", "falsch") is None
    assert si.pruefe_login("unbekannt@f.de", "passwort1") is None


def test_inaktives_login_meldet_sich_nicht_an(store):
    rec = si.anlegen("A", "a@f.de", "passwort1")
    si.aktualisieren(rec["id"], aktiv=False)
    assert si.pruefe_login("a@f.de", "passwort1") is None


def test_pruefe_login_akzeptiert_bytes(store):
    si.anlegen("A", "a@f.de", "passwort1")
    assert si.pruefe_login(b"a@f.de", b"passwort1") is not None


# ── Absender-Grenze ───────────────────────────────────────────────────────────

def test_pruefe_grenze_bei_gepinntem_absender(store):
    ident = si.anlegen("KD", "kd@f.de", "passwort1", absender="kd@f.de")
    erlaubt, _, _ = si.pruefe(ident, "kd@f.de", ["x@extern.de"])
    assert erlaubt is True
    erlaubt, grund, antwort = si.pruefe(ident, "fremd@f.de", ["x@extern.de"])
    assert erlaubt is False
    assert antwort.startswith("553")
    assert "fremd@f.de" in grund


def test_pruefe_ohne_pin_erlaubt_jeden_absender(store):
    ident = si.anlegen("Frei", "frei@f.de", "passwort1")   # kein absender
    erlaubt, _, _ = si.pruefe(ident, "beliebig@f.de", ["x@extern.de"])
    assert erlaubt is True


# ── Änderung / Entfernen ──────────────────────────────────────────────────────

def test_aktualisieren_leeres_passwort_bleibt(store):
    rec = si.anlegen("A", "a@f.de", "passwort1")
    si.aktualisieren(rec["id"], name="Neu", passwort=None)     # Passwort unverändert
    assert si.pruefe_login("a@f.de", "passwort1") is not None
    assert si.liste()[0]["name"] == "Neu"


def test_aktualisieren_neues_passwort_wirkt(store):
    rec = si.anlegen("A", "a@f.de", "passwort1")
    si.aktualisieren(rec["id"], passwort="ganzneu9")
    assert si.pruefe_login("a@f.de", "passwort1") is None
    assert si.pruefe_login("a@f.de", "ganzneu9") is not None


def test_entfernen(store):
    rec = si.anlegen("A", "a@f.de", "passwort1")
    assert si.entfernen(rec["id"]) is True
    assert si.liste() == []
    assert si.entfernen("gibtsnicht") is False
