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


@pytest.fixture
def tenant(monkeypatch):
    """Tut so, als kenne das Gateway die Domäne firma.de und zwei Postfächer."""
    import smtp_relay
    import exo_mailboxes
    monkeypatch.setattr(smtp_relay, "_eigene_domaenen", lambda: {"firma.de"})
    monkeypatch.setattr(exo_mailboxes, "known_addresses",
                        lambda: {"chef@firma.de", "kd@firma.de"})
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

def test_pruefe_grenze_bei_gepinntem_absender(store, tenant):
    # extern=True isoliert die Pin-Grenze von der intern/extern-Grenze.
    ident = si.anlegen("KD", "kd@firma.de", "passwort1",
                       absender="kd@firma.de", extern=True)
    erlaubt, _, _ = si.pruefe(ident, "kd@firma.de", ["x@extern.de"])
    assert erlaubt is True
    erlaubt, grund, antwort = si.pruefe(ident, "fremd@firma.de", ["x@extern.de"])
    assert erlaubt is False
    assert antwort.startswith("553")                # Pin schlägt vor den übrigen Grenzen an
    assert "fremd@firma.de" in grund


def test_pruefe_ohne_pin_aber_extern_erlaubt_jeden_absender_der_domaene(store, tenant):
    ident = si.anlegen("Frei", "frei@firma.de", "passwort1", extern=True)  # kein pin
    erlaubt, _, _ = si.pruefe(ident, "beliebig@firma.de", ["x@extern.de"])
    assert erlaubt is True


# ── Absenderdomäne im Tenant (Schutz vor offenem Relay) ────────────────────────

def test_pruefe_absenderdomaene_muss_zum_tenant_gehoeren(store, tenant):
    ident = si.anlegen("KD", "kd@firma.de", "passwort1", extern=True)
    erlaubt, grund, antwort = si.pruefe(ident, "kd@fremd.de", ["chef@firma.de"])
    assert erlaubt is False
    assert antwort.startswith("550")
    assert "fremd.de" in grund


def test_pruefe_temporaer_wenn_tenant_domaenen_unbekannt(store, monkeypatch):
    import smtp_relay
    monkeypatch.setattr(smtp_relay, "_eigene_domaenen", lambda: set())
    ident = si.anlegen("KD", "kd@firma.de", "passwort1")
    erlaubt, _, antwort = si.pruefe(ident, "kd@firma.de", ["chef@firma.de"])
    assert erlaubt is False
    assert antwort.startswith("451")           # im Zweifel NICHT zustellen


# ── Ziel intern (Vorgabe) / extern ─────────────────────────────────────────────

def test_extern_flag_gespeichert_vorgabe_intern(store):
    rec = si.anlegen("A", "a@f.de", "passwort1", extern=True)
    assert rec["extern"] is True
    assert si.liste()[0]["extern"] is True
    rec2 = si.anlegen("B", "b@f.de", "passwort1")          # ohne Angabe
    assert rec2["extern"] is False                          # Vorgabe: nur intern


def test_pruefe_intern_nur_interne_empfaenger(store, tenant):
    ident = si.anlegen("KD", "kd@firma.de", "passwort1")    # extern=False (Vorgabe)
    erlaubt, _, _ = si.pruefe(ident, "kd@firma.de", ["chef@firma.de"])
    assert erlaubt is True                                   # rein intern: ok
    erlaubt, grund, antwort = si.pruefe(
        ident, "kd@firma.de", ["chef@firma.de", "aussen@extern.de"])
    assert erlaubt is False                                  # ein externes Ziel dabei
    assert antwort.startswith("550")
    assert "aussen@extern.de" in grund


def test_pruefe_intern_temporaer_wenn_postfaecher_unbekannt(store, monkeypatch):
    import smtp_relay
    import exo_mailboxes
    monkeypatch.setattr(smtp_relay, "_eigene_domaenen", lambda: {"firma.de"})
    monkeypatch.setattr(exo_mailboxes, "known_addresses", lambda: set())
    ident = si.anlegen("KD", "kd@firma.de", "passwort1")     # intern
    erlaubt, _, antwort = si.pruefe(ident, "kd@firma.de", ["chef@firma.de"])
    assert erlaubt is False
    assert antwort.startswith("451")


def test_aktualisieren_absender_pin_setzen_und_leeren(store):
    rec = si.anlegen("A", "a@f.de", "passwort1")           # ohne Pin
    assert si.liste()[0]["absender"] == ""
    si.aktualisieren(rec["id"], absender="KD@Firma.de")     # setzen (normalisiert)
    assert si.liste()[0]["absender"] == "kd@firma.de"
    si.aktualisieren(rec["id"], absender="")                # leeren = Pin entfernen
    assert si.liste()[0]["absender"] == ""
    si.aktualisieren(rec["id"], passwort="ganzneu9")        # None-Felder lassen Pin unberührt
    assert si.liste()[0]["absender"] == ""


def test_aktualisieren_extern_umschalten(store):
    rec = si.anlegen("A", "a@f.de", "passwort1")
    assert si.liste()[0]["extern"] is False
    si.aktualisieren(rec["id"], extern=True)
    assert si.liste()[0]["extern"] is True
    si.aktualisieren(rec["id"], name="Neu", extern=None)     # None = unverändert
    assert si.liste()[0]["extern"] is True


# ── Anmelde-Bremse: Verdrahtung mit login_drossel (kein Eigenbau) ──────────────

def test_bremse_gedrosselte_ip_wird_vor_passwortpruefung_abgewiesen(store, monkeypatch):
    import login_drossel
    ruf = {"erfolg": [], "fehl": []}
    monkeypatch.setattr(login_drossel, "gesperrt", lambda k: k == "587:9.9.9.9")
    monkeypatch.setattr(login_drossel, "sperr_sekunden", lambda k: 42.0)
    monkeypatch.setattr(login_drossel, "erfolg", lambda k: ruf["erfolg"].append(k))
    monkeypatch.setattr(login_drossel, "fehlversuch", lambda k: ruf["fehl"].append(k))
    si.anlegen("A", "a@f.de", "passwort1")
    # Gedrosselte IP: sogar das RICHTIGE Passwort wird abgewiesen, ohne Prüfung —
    # daher wird weder Erfolg noch Fehlversuch verbucht.
    assert si.pruefe_login("a@f.de", "passwort1", "9.9.9.9") is None
    assert ruf["fehl"] == [] and ruf["erfolg"] == []
    # Freie IP kommt durch und meldet Erfolg (löscht die Zählung).
    assert si.pruefe_login("a@f.de", "passwort1", "1.1.1.1") is not None
    assert ruf["erfolg"] == ["587:1.1.1.1"]


def test_bremse_zaehlt_fehlversuch_je_ip_mit_praefix(store, monkeypatch):
    import login_drossel
    fehl = []
    monkeypatch.setattr(login_drossel, "gesperrt", lambda k: False)
    monkeypatch.setattr(login_drossel, "fehlversuch", lambda k: fehl.append(k))
    monkeypatch.setattr(login_drossel, "erfolg", lambda k: None)
    si.anlegen("A", "a@f.de", "passwort1")
    assert si.pruefe_login("a@f.de", "falsch", "9.9.9.9") is None       # falsches PW
    assert si.pruefe_login("unbekannt@f.de", "x", "9.9.9.9") is None    # kein Login
    assert fehl == ["587:9.9.9.9", "587:9.9.9.9"]                       # beide gezählt


def test_bremse_ohne_quelle_ip_wird_nicht_befragt(store, monkeypatch):
    import login_drossel

    def _nie(_k):
        raise AssertionError("Drossel darf ohne Quell-IP nicht befragt werden")

    monkeypatch.setattr(login_drossel, "gesperrt", _nie)
    monkeypatch.setattr(login_drossel, "fehlversuch", _nie)
    si.anlegen("A", "a@f.de", "passwort1")
    assert si.pruefe_login("a@f.de", "passwort1", "") is not None
    assert si.pruefe_login("a@f.de", "falsch", "") is None


def test_bremse_echte_drossel_sperrt_und_gibt_wieder_frei(store, monkeypatch):
    """Integrationstest gegen die ECHTE login_drossel (nur die Uhr gestellt)."""
    import login_drossel
    uhr = [1000.0]
    monkeypatch.setattr(login_drossel, "_jetzt", lambda: uhr[0])
    monkeypatch.setattr(login_drossel, "_FEHLER", {})
    si.anlegen("A", "a@f.de", "passwort1")
    ip = "9.9.9.9"
    for _ in range(5):                     # > _FREI (3) → Backoff greift
        assert si.pruefe_login("a@f.de", "falsch", ip) is None
    assert si.pruefe_login("a@f.de", "passwort1", ip) is None        # gedrosselt
    uhr[0] += login_drossel._FENSTER + 1                             # Fenster vorbei
    assert si.pruefe_login("a@f.de", "passwort1", ip) is not None    # wieder frei


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
