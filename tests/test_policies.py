"""Gemeinsame Vorlagen-Auflösung (policies.aufloesen) — EINE Quelle für Hot-Path,
Abwesenheit, Self-Service, Add-in, Vorschau und Health-Check.

Seit v1.12.99 gilt für jeden Slot dieselbe Regel: Postfach-Feld (auch "") >
Gruppen-Regel > Standardrichtlinie — außer die greifende Ebene schreibt fest
(Schalter RICHTLINIE_GRUPPE_FEST / RICHTLINIE_STANDARD_FEST; die Matrix steht in
tests/test_gruppenregeln_fest.py). Dazu die Bestandsmigrationen v3→v4 und — für
die Kette — v8→v9 (`use_policy` entfällt).
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
    assert v == {"sig": "S", "min": "", "oof": "O", "intern": "", "banner": "B", "disclaimer": ""}


def test_gruppe_uebersteuert_den_default(monkeypatch):
    mc = {"a@x.de": {"use_policy": True}}
    _store(monkeypatch, TEMPLATE_POLICIES={"oof": "Global", "sig": "GlobalSig"}, **_GRUPPE)
    v = policies.aufloesen("a@x.de", mc, mc["a@x.de"])
    assert v["oof"] == "GruppeOof" and v["sig"] == "GruppeSig"
    assert v["banner"] == "GruppeBanner"


def test_banner_ist_wahl_wie_die_signatur(monkeypatch):
    """Seit v1.12.99: ohne Feld der Standard (hier Gruppe bzw. global), mit Feld
    die Wahl — sofern nicht fest. `use_policy` wird nicht mehr gelesen."""
    _store(monkeypatch, TEMPLATE_POLICIES={"oof": "Global", "min": "Kurz", "banner": "Firma",
                                           "disclaimer": "Recht"},
           RICHTLINIE_GRUPPE_FEST=False, **_GRUPPE)
    mc = {"a@x.de": {"banner_template": "EigenBanner", "use_policy": True}}
    v = policies.aufloesen("a@x.de", mc, mc["a@x.de"])
    assert v["banner"] == "EigenBanner", "eigene Banner-Wahl gilt, use_policy ist bedeutungslos"
    assert v["disclaimer"] == "Recht" and v["min"] == "Kurz"
    mc = {"a@x.de": {"banner_template": ""}}
    assert policies.aufloesen("a@x.de", mc, mc["a@x.de"])["banner"] == "", "\"\" = ausdrücklich keiner"


def test_leeres_feld_heisst_ausdruecklich_keine(monkeypatch):
    cfg = {"min_template": "", "oof_template": ""}
    _store(monkeypatch, TEMPLATE_POLICIES={"min": "Kurz", "oof": "Global"},
           RICHTLINIE_GRUPPE_FEST=False, **_GRUPPE)
    v = policies.aufloesen("a@x.de", {"a@x.de": cfg}, cfg)
    assert v["min"] == "" and v["oof"] == ""


def test_eigene_wahl_schlaegt_die_gruppe_nur_ohne_festschreiben(monkeypatch):
    cfg = {"oof_template": "Eigene"}
    _store(monkeypatch, TEMPLATE_POLICIES={}, RICHTLINIE_GRUPPE_FEST=False, **_GRUPPE)
    assert policies.vorlage_fuer("a@x.de", "oof", {"a@x.de": cfg}, cfg) == "Eigene"
    # Vorgabe (Schalter fehlt = an): die Gruppe schreibt fest.
    _store(monkeypatch, TEMPLATE_POLICIES={}, **_GRUPPE)
    assert policies.vorlage_fuer("a@x.de", "oof", {"a@x.de": cfg}, cfg) == "GruppeOof"


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
    """v3→v4 und — weil `use_policy` seither nicht mehr gelesen wird — v8→v9
    zusammen: die heutige Auflösung liefert, was vor v1.9.113 galt."""
    vorher = _alt_aufloesen(_BESTAND[key], _TP)
    daten = settings_store._migrate_v3_to_v4({"MAILBOX_CONFIG": {k: dict(v) for k, v in _BESTAND.items()}})
    daten = settings_store._migrate_v8_to_v9(daten)
    mc = daten["MAILBOX_CONFIG"]
    assert not any("use_policy" in e for e in mc.values())
    _store(monkeypatch, TEMPLATE_POLICIES=_TP, CUSTOM_POLICIES=[], INTERNAL_GROUPS={},
           MAILBOX_CONFIG=mc)
    nachher = policies.aufloesen(key, mc, mc[key])
    assert nachher.pop("intern") == ""          # Slot seit v1.11.56, im Bestand leer
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
    assert daten["MAILBOX_CONFIG"]["g"] == {"template": "default", "min_template": "",
                                            "oof_template": "", "banner_template": "",
                                            "disclaimer_template": "", "addin_templates": []}


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
