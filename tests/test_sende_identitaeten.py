"""Sende-Identitäten — Anlage, Anmeldung und die Absender-Grenze.

Verwaltetes-Identitäten-Modell (Plan 11.09.): Der `login` IST der EXO-Alias;
zusammen mit `domaene` ergibt er die Shared-Mailbox-Adresse `login@domäne`, die
zugleich der Absender-Pin ist. Es gibt kein separates Absender-Feld mehr.

Diese Tests MÜSSEN fehlschlagen, wenn:
  - ein Passwort im Klartext (statt als Hash) gespeichert würde,
  - `pruefe_login` ein falsches Passwort / inaktives Login durchließe,
  - die Absender-Grenze (`pruefe`) ein Login als fremden Absender einliefern ließe,
  - ein Login mit `@`/Leerzeichen (kein gültiger Alias) angenommen würde,
  - die abgeleitete Adresse NICHT als Pin gesetzt würde,
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


# ── Alias-Validierung + Adress-Ableitung ──────────────────────────────────────

def test_alias_gueltig():
    assert si.alias_gueltig("drucker.eg")
    assert si.alias_gueltig("kd_2-neu")
    assert not si.alias_gueltig("a@b.de")        # @ ist kein Alias-Zeichen
    assert not si.alias_gueltig("mit leer")      # kein Leerzeichen
    assert not si.alias_gueltig("")              # leer


def test_abgeleitete_adresse():
    assert si.abgeleitete_adresse("Drucker.EG", " Main.Zarenko.net ") == "drucker.eg@main.zarenko.net"
    assert si.abgeleitete_adresse("x", "") == ""     # ohne Domäne kein Pin
    assert si.abgeleitete_adresse("", "firma.de") == ""


def test_login_mit_at_wird_abgelehnt(store):
    with pytest.raises(ValueError):
        si.anlegen("X", "kd@firma.de", "passwort1", domaene="firma.de")


def test_login_mit_leerzeichen_wird_abgelehnt(store):
    with pytest.raises(ValueError):
        si.anlegen("X", "mit leer", "passwort1", domaene="firma.de")


# ── Anlage ────────────────────────────────────────────────────────────────────

def test_anlegen_leitet_adresse_ab_und_pinnt(store):
    rec = si.anlegen("Drucker EG", "Drucker.EG", "geheim123", domaene="Firma.de")
    assert rec["login"] == "drucker.eg"           # normalisiert (lowercase)
    assert rec["domaene"] == "firma.de"
    assert rec["adresse"] == "drucker.eg@firma.de"   # login@domäne
    assert rec["absender"] == "drucker.eg@firma.de"  # zugleich der Pin
    assert rec["name"] == "Drucker EG"            # wird zum EXO-Anzeigenamen
    assert "passwort_hash" not in rec             # öffentliche Sicht ohne Hash
    liste = si.liste()
    assert len(liste) == 1
    assert all("passwort_hash" not in r for r in liste)


def test_anlegen_ohne_domaene_hat_leeren_pin(store):
    rec = si.anlegen("A", "a", "passwort1")       # keine Domäne
    assert rec["adresse"] == ""
    assert rec["absender"] == ""


def test_passwort_wird_gehasht_nicht_im_klartext(store):
    si.anlegen("X", "x", "supersecret1")
    roh = (si.PFAD).read_text("utf-8")
    assert "supersecret1" not in roh              # niemals Klartext auf Platte
    assert "pbkdf2:sha256:" in roh                # als Hash gespeichert


def test_doppeltes_login_abgelehnt(store):
    si.anlegen("A", "gleich", "passwort1")
    with pytest.raises(ValueError):
        si.anlegen("B", "GLEICH", "passwort2")    # case-insensitiv derselbe


def test_zu_kurzes_passwort_abgelehnt(store):
    with pytest.raises(ValueError):
        si.anlegen("A", "a", "kurz")


def test_speicherdatei_ist_600(store):
    si.anlegen("A", "a", "passwort1")
    modus = stat.S_IMODE(si.PFAD.stat().st_mode)
    assert modus == 0o600, f"Speicherdatei hat {oct(modus)}, erwartet 0o600"


# ── Anmeldung ─────────────────────────────────────────────────────────────────

def test_pruefe_login_erfolg_und_fehler(store):
    si.anlegen("A", "a", "passwort1")
    assert si.pruefe_login("a", "passwort1") is not None
    assert si.pruefe_login("A", "passwort1") is not None        # login case-insensitiv
    assert si.pruefe_login("a", "falsch") is None
    assert si.pruefe_login("unbekannt", "passwort1") is None


def test_inaktives_login_meldet_sich_nicht_an(store):
    rec = si.anlegen("A", "a", "passwort1")
    si.aktualisieren(rec["id"], aktiv=False)
    assert si.pruefe_login("a", "passwort1") is None


def test_pruefe_login_akzeptiert_bytes(store):
    si.anlegen("A", "a", "passwort1")
    assert si.pruefe_login(b"a", b"passwort1") is not None


# ── Absender-Grenze ───────────────────────────────────────────────────────────

def test_pruefe_grenze_bei_gepinntem_absender(store, tenant):
    # extern=True isoliert die Pin-Grenze von der intern/extern-Grenze.
    ident = si.anlegen("KD", "kd", "passwort1", domaene="firma.de", extern=True)
    assert ident["adresse"] == "kd@firma.de"
    erlaubt, _, _ = si.pruefe(ident, "kd@firma.de", ["x@extern.de"])
    assert erlaubt is True
    erlaubt, grund, antwort = si.pruefe(ident, "fremd@firma.de", ["x@extern.de"])
    assert erlaubt is False
    assert antwort.startswith("553")                # Pin schlägt vor den übrigen Grenzen an
    assert "fremd@firma.de" in grund


def test_pruefe_ohne_pin_aber_extern_erlaubt_jeden_absender_der_domaene(store, tenant):
    ident = si.anlegen("Frei", "frei", "passwort1", extern=True)  # kein Pin (keine Domäne)
    erlaubt, _, _ = si.pruefe(ident, "beliebig@firma.de", ["x@extern.de"])
    assert erlaubt is True


# ── Absenderdomäne im Tenant (Schutz vor offenem Relay) ────────────────────────

def test_pruefe_absenderdomaene_muss_zum_tenant_gehoeren(store, tenant):
    ident = si.anlegen("KD", "kd", "passwort1", extern=True)
    erlaubt, grund, antwort = si.pruefe(ident, "kd@fremd.de", ["chef@firma.de"])
    assert erlaubt is False
    assert antwort.startswith("550")
    assert "fremd.de" in grund


def test_pruefe_temporaer_wenn_tenant_domaenen_unbekannt(store, monkeypatch):
    import smtp_relay
    monkeypatch.setattr(smtp_relay, "_eigene_domaenen", lambda: set())
    ident = si.anlegen("KD", "kd", "passwort1")
    erlaubt, _, antwort = si.pruefe(ident, "kd@firma.de", ["chef@firma.de"])
    assert erlaubt is False
    assert antwort.startswith("451")           # im Zweifel NICHT zustellen


# ── Ziel intern (Vorgabe) / extern ─────────────────────────────────────────────

def test_extern_flag_gespeichert_vorgabe_intern(store):
    rec = si.anlegen("A", "a", "passwort1", extern=True)
    assert rec["extern"] is True
    assert si.liste()[0]["extern"] is True
    rec2 = si.anlegen("B", "b", "passwort1")          # ohne Angabe
    assert rec2["extern"] is False                     # Vorgabe: nur intern


def test_pruefe_intern_nur_interne_empfaenger(store, tenant):
    ident = si.anlegen("KD", "kd", "passwort1", domaene="firma.de")   # extern=False
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
    ident = si.anlegen("KD", "kd", "passwort1", domaene="firma.de")     # intern
    erlaubt, _, antwort = si.pruefe(ident, "kd@firma.de", ["chef@firma.de"])
    assert erlaubt is False
    assert antwort.startswith("451")


def test_aktualisieren_absender_pin_setzen_und_leeren(store):
    """`aktualisieren(absender=...)` bleibt als Low-Level-Setter erhalten (nicht im
    UI). Der reguläre Weg ist die Ableitung aus Login+Domäne beim Anlegen."""
    rec = si.anlegen("A", "a", "passwort1")                 # ohne Domäne → kein Pin
    assert si.liste()[0]["absender"] == ""
    si.aktualisieren(rec["id"], absender="KD@Firma.de")     # setzen (normalisiert)
    assert si.liste()[0]["absender"] == "kd@firma.de"
    si.aktualisieren(rec["id"], absender="")                # leeren = Pin entfernen
    assert si.liste()[0]["absender"] == ""
    si.aktualisieren(rec["id"], passwort="ganzneu9")        # None-Felder lassen Pin unberührt
    assert si.liste()[0]["absender"] == ""


def test_kontingent_setzen_und_klemmen(store):
    """Weiches Tageskontingent: setzbar, Vorgabe 0 (unbegrenzt), negativ → 0."""
    rec = si.anlegen("A", "a", "passwort1")
    assert si.liste()[0]["kontingent"] == 0        # Vorgabe: unbegrenzt
    si.aktualisieren(rec["id"], kontingent=5000)
    assert si.liste()[0]["kontingent"] == 5000
    si.aktualisieren(rec["id"], kontingent=-3)      # negativ → geklemmt auf 0
    assert si.liste()[0]["kontingent"] == 0
    si.aktualisieren(rec["id"], name="Neu", kontingent=None)   # None = unverändert
    assert si.liste()[0]["kontingent"] == 0


def test_aktualisieren_extern_umschalten(store):
    rec = si.anlegen("A", "a", "passwort1")
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
    si.anlegen("A", "a", "passwort1")
    # Gedrosselte IP: sogar das RICHTIGE Passwort wird abgewiesen, ohne Prüfung —
    # daher wird weder Erfolg noch Fehlversuch verbucht.
    assert si.pruefe_login("a", "passwort1", "9.9.9.9") is None
    assert ruf["fehl"] == [] and ruf["erfolg"] == []
    # Freie IP kommt durch und meldet Erfolg (löscht die Zählung).
    assert si.pruefe_login("a", "passwort1", "1.1.1.1") is not None
    assert ruf["erfolg"] == ["587:1.1.1.1"]


def test_bremse_zaehlt_fehlversuch_je_ip_mit_praefix(store, monkeypatch):
    import login_drossel
    fehl = []
    monkeypatch.setattr(login_drossel, "gesperrt", lambda k: False)
    monkeypatch.setattr(login_drossel, "fehlversuch", lambda k: fehl.append(k))
    monkeypatch.setattr(login_drossel, "erfolg", lambda k: None)
    si.anlegen("A", "a", "passwort1")
    assert si.pruefe_login("a", "falsch", "9.9.9.9") is None       # falsches PW
    assert si.pruefe_login("unbekannt", "x", "9.9.9.9") is None    # kein Login
    assert fehl == ["587:9.9.9.9", "587:9.9.9.9"]                  # beide gezählt


def test_bremse_ohne_quelle_ip_wird_nicht_befragt(store, monkeypatch):
    import login_drossel

    def _nie(_k):
        raise AssertionError("Drossel darf ohne Quell-IP nicht befragt werden")

    monkeypatch.setattr(login_drossel, "gesperrt", _nie)
    monkeypatch.setattr(login_drossel, "fehlversuch", _nie)
    si.anlegen("A", "a", "passwort1")
    assert si.pruefe_login("a", "passwort1", "") is not None
    assert si.pruefe_login("a", "falsch", "") is None


def test_bremse_echte_drossel_sperrt_und_gibt_wieder_frei(store, monkeypatch):
    """Integrationstest gegen die ECHTE login_drossel (nur die Uhr gestellt)."""
    import login_drossel
    uhr = [1000.0]
    monkeypatch.setattr(login_drossel, "_jetzt", lambda: uhr[0])
    monkeypatch.setattr(login_drossel, "_FEHLER", {})
    si.anlegen("A", "a", "passwort1")
    ip = "9.9.9.9"
    for _ in range(5):                     # > _FREI (3) → Backoff greift
        assert si.pruefe_login("a", "falsch", ip) is None
    assert si.pruefe_login("a", "passwort1", ip) is None        # gedrosselt
    uhr[0] += login_drossel._FENSTER + 1                        # Fenster vorbei
    assert si.pruefe_login("a", "passwort1", ip) is not None    # wieder frei


# ── Änderung / Entfernen ──────────────────────────────────────────────────────

def test_aktualisieren_leeres_passwort_bleibt(store):
    rec = si.anlegen("A", "a", "passwort1")
    si.aktualisieren(rec["id"], name="Neu", passwort=None)     # Passwort unverändert
    assert si.pruefe_login("a", "passwort1") is not None
    assert si.liste()[0]["name"] == "Neu"


def test_aktualisieren_neues_passwort_wirkt(store):
    rec = si.anlegen("A", "a", "passwort1")
    si.aktualisieren(rec["id"], passwort="ganzneu9")
    assert si.pruefe_login("a", "passwort1") is None
    assert si.pruefe_login("a", "ganzneu9") is not None


def test_entfernen(store):
    rec = si.anlegen("A", "a", "passwort1")
    assert si.entfernen(rec["id"]) is True
    assert si.liste() == []
    assert si.entfernen("gibtsnicht") is False
