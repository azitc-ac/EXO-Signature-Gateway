"""Gemeinsame Vorlagen-Auflösung (policies.aufloesen) — EINE Quelle für Hot-Path,
Abwesenheit, Self-Service, Add-in, Vorschau und Health-Check.

Seit v1.9.113 zwei Arten von Slots:
  * Wahl-Slots sig/min/oof: Postfach-Feld (auch "") > Gruppen-Default > global —
    UNABHÄNGIG von use_policy.
  * Richtlinien-Slots banner/disclaimer: use_policy → Richtlinie (+ Gruppe),
    sonst das Postfach-eigene Feld.
Dazu die einmalige Bestandsmigration (settings_store._migrate_v3_to_v4).
"""
from __future__ import annotations

import pytest

import policies
import settings_store


def _store(monkeypatch, **d):
    monkeypatch.setattr(settings_store, "get", lambda k, dd=None: d.get(k, dd))


_GRUPPE = {"CUSTOM_POLICIES": [
    {"condition_type": "group", "group_name": "G", "applies_to": "oof", "template": "GruppeOof"},
    {"condition_type": "group", "group_name": "G", "applies_to": "sig", "template": "GruppeSig"},
    {"condition_type": "group", "group_name": "G", "applies_to": "banner", "template": "GruppeBanner"},
], "INTERNAL_GROUPS": {"G": ["a@x.de"]}}


def test_ohne_gruppen_gilt_der_globale_default(monkeypatch):
    _store(monkeypatch, TEMPLATE_POLICIES={"sig": "S", "oof": "O", "banner": "B"},
           CUSTOM_POLICIES=[], INTERNAL_GROUPS={})
    v = policies.aufloesen("a@x.de", {"a@x.de": {}}, {})
    assert v == {"sig": "S", "min": "", "oof": "O", "banner": "B", "disclaimer": ""}


def test_gruppe_uebersteuert_den_default(monkeypatch):
    mc = {"a@x.de": {"use_policy": True}}
    _store(monkeypatch, TEMPLATE_POLICIES={"oof": "Global", "sig": "GlobalSig"}, **_GRUPPE)
    v = policies.aufloesen("a@x.de", mc, mc["a@x.de"])
    assert v["oof"] == "GruppeOof" and v["sig"] == "GruppeSig"
    assert v["banner"] == "GruppeBanner"


def test_wahl_slots_folgen_dem_default_auch_bei_use_policy_false(monkeypatch):
    """Der Kern der Entkopplung: use_policy=false betrifft nur Banner/Disclaimer.
    Ohne eigenes Feld gilt für sig/min/oof weiter der (Gruppen-)Default."""
    mc = {"a@x.de": {"use_policy": False, "banner_template": "EigenBanner"}}
    _store(monkeypatch, TEMPLATE_POLICIES={"oof": "Global", "min": "Kurz", "banner": "Firma"},
           **_GRUPPE)
    v = policies.aufloesen("a@x.de", mc, mc["a@x.de"])
    assert v["oof"] == "GruppeOof"
    assert v["sig"] == "GruppeSig"
    assert v["min"] == "Kurz"
    assert v["banner"] == "EigenBanner", "Postfach-Ausnahme für Banner muss gelten"
    assert v["disclaimer"] == ""


def test_eigene_wahl_bei_use_policy_true_behaelt_banner_der_richtlinie(monkeypatch):
    """Vor v1.9.113 kostete eine eigene Signaturwahl (use_policy=false) still den
    Banner der Richtlinie. Jetzt: Wahl gilt, Banner bleibt."""
    cfg = {"use_policy": True, "template": "Meine", "banner_template": "Eingefroren"}
    _store(monkeypatch, TEMPLATE_POLICIES={"sig": "Firma", "banner": "FirmenBanner"},
           CUSTOM_POLICIES=[], INTERNAL_GROUPS={})
    v = policies.aufloesen("a@x.de", {"a@x.de": cfg}, cfg)
    assert v["sig"] == "Meine"
    assert v["banner"] == "FirmenBanner"


def test_leeres_feld_heisst_ausdruecklich_keine(monkeypatch):
    cfg = {"min_template": "", "oof_template": ""}
    _store(monkeypatch, TEMPLATE_POLICIES={"min": "Kurz", "oof": "Global"}, **_GRUPPE)
    v = policies.aufloesen("a@x.de", {"a@x.de": cfg}, cfg)
    assert v["min"] == "" and v["oof"] == ""


def test_eigene_wahl_schlaegt_die_gruppe(monkeypatch):
    cfg = {"oof_template": "Eigene"}
    _store(monkeypatch, TEMPLATE_POLICIES={}, **_GRUPPE)
    assert policies.vorlage_fuer("a@x.de", "oof", {"a@x.de": cfg}, cfg) == "Eigene"


def test_signatur_ist_nie_leer(monkeypatch):
    _store(monkeypatch, TEMPLATE_POLICIES={}, CUSTOM_POLICIES=[], INTERNAL_GROUPS={})
    assert policies.vorlage_fuer("a@x.de", "sig", {"a@x.de": {}}, {}) == "default"
    cfg = {"template": ""}
    assert policies.vorlage_fuer("a@x.de", "sig", {"a@x.de": cfg}, cfg) == "default"


def test_standards_ignorieren_die_eigene_wahl(monkeypatch):
    mc = {"a@x.de": {"oof_template": "Eigene", "use_policy": False}}
    _store(monkeypatch, TEMPLATE_POLICIES={"oof": "Global"}, **_GRUPPE)
    assert policies.standards("a@x.de", mc)["oof"] == "GruppeOof"


def test_first_match_wins(monkeypatch):
    mc = {"a@x.de": {"use_policy": True}}
    _store(monkeypatch, TEMPLATE_POLICIES={},
           CUSTOM_POLICIES=[
               {"condition_type": "group", "group_name": "G", "applies_to": "oof", "template": "Erste"},
               {"condition_type": "group", "group_name": "G", "applies_to": "oof", "template": "Zweite"},
           ],
           INTERNAL_GROUPS={"G": ["a@x.de"]})
    assert policies.vorlage_fuer("a@x.de", "oof", mc, mc["a@x.de"]) == "Erste"


# ── Bestandsmigration v3 → v4 ──────────────────────────────────────────────────

def _alt_aufloesen(cfg: dict, tp: dict) -> dict:
    """Die Auflösung VOR v1.9.113 (ohne Gruppen) — Referenz für „bitgleich"."""
    if cfg.get("use_policy", True):
        return {"sig": tp.get("sig") or "default", "min": tp.get("min") or "",
                "oof": tp.get("oof") or "", "banner": tp.get("banner") or "",
                "disclaimer": tp.get("disclaimer") or ""}
    return {"sig": cfg.get("template") or "default", "min": cfg.get("min_template") or "",
            "oof": cfg.get("oof_template") or "", "banner": cfg.get("banner_template") or "",
            "disclaimer": cfg.get("disclaimer_template") or ""}


_BESTAND = {
    # use_policy=false ohne template (Raspi-Fall): hiess „default"
    "g1": {"sig": True, "use_policy": False, "min_template": "Kurz",
           "oof_template": "Urlaub", "banner_template": "B"},
    # use_policy=false ganz ohne Felder: hiess default/keine/keine
    "g2": {"sig": True, "use_policy": False},
    # use_policy=true mit eingefrorener Kopie: Felder galten NICHT
    "g3": {"sig": True, "use_policy": True, "template": "Alt", "oof_template": "AltOof"},
    # Alt-Eintrag ohne use_policy: wurde als true behandelt
    "g4": {"sig": True, "template": "Firma"},
}
_TP = {"sig": "Firmensig", "min": "Minimal", "oof": "FirmenOof", "banner": "FB"}


@pytest.mark.parametrize("key", sorted(_BESTAND))
def test_migration_haelt_die_wirksame_vorlage_bitgleich(monkeypatch, key):
    vorher = _alt_aufloesen(_BESTAND[key], _TP)
    daten = settings_store._migrate_v3_to_v4({"MAILBOX_CONFIG": {k: dict(v) for k, v in _BESTAND.items()}})
    mc = daten["MAILBOX_CONFIG"]
    _store(monkeypatch, TEMPLATE_POLICIES=_TP, CUSTOM_POLICIES=[], INTERNAL_GROUPS={},
           MAILBOX_CONFIG=mc)
    nachher = policies.aufloesen(key, mc, mc[key])
    assert nachher == vorher, f"{key}: {vorher} → {nachher}"


def test_migration_laeuft_nur_einmal():
    """Nach der Umstellung ist ein fehlendes Feld bei use_policy=false gewollt
    („folgt dem Standard") und darf nicht erneut festgeschrieben werden."""
    daten, geaendert = settings_store._run_migrations(
        {"_SCHEMA_VERSION": settings_store.SETTINGS_SCHEMA_VERSION,
         "MAILBOX_CONFIG": {"g": {"use_policy": False}}})
    assert not geaendert
    assert daten["MAILBOX_CONFIG"]["g"] == {"use_policy": False}


def test_migrationskette_ab_v3():
    daten, geaendert = settings_store._run_migrations(
        {"_SCHEMA_VERSION": 3, "MAILBOX_CONFIG": {"g": {"use_policy": False}}})
    assert geaendert and daten["_SCHEMA_VERSION"] == settings_store.SETTINGS_SCHEMA_VERSION
    assert daten["MAILBOX_CONFIG"]["g"] == {"use_policy": False, "template": "default",
                                            "min_template": "", "oof_template": ""}


# ── Gruppenbasierte Variablen ──────────────────────────────────────────────────

def test_group_vars_fuer_mitglied(monkeypatch):
    mc = {"a@x.de": {}}
    _store(monkeypatch,
           INTERNAL_GROUPS={"Vertrieb": ["a@x.de"]},
           GROUP_VARS={"Vertrieb": {"vertreter_mail": "chef@x.de", "vertreter_tel": "123"}})
    v = policies.group_vars_for("a@x.de", mc)
    assert v == {"vertreter_mail": "chef@x.de", "vertreter_tel": "123"}


def test_group_vars_nicht_mitglied_leer(monkeypatch):
    mc = {"a@x.de": {}}
    _store(monkeypatch,
           INTERNAL_GROUPS={"Vertrieb": ["b@x.de"]},
           GROUP_VARS={"Vertrieb": {"vertreter_mail": "chef@x.de"}})
    assert policies.group_vars_for("a@x.de", mc) == {}


def test_group_vars_first_match_bei_mehreren_gruppen(monkeypatch):
    mc = {"a@x.de": {}}
    _store(monkeypatch,
           INTERNAL_GROUPS={"A": ["a@x.de"], "B": ["a@x.de"]},
           GROUP_VARS={"A": {"vertreter_mail": "erste@x.de"},
                       "B": {"vertreter_mail": "zweite@x.de"}})
    # A steht in INTERNAL_GROUPS zuerst → gewinnt
    assert policies.group_vars_for("a@x.de", mc)["vertreter_mail"] == "erste@x.de"
