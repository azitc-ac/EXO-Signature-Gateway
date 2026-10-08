"""Auto-Typisierung des Vorlagen-Bestands beim Start.

Vor der Einführung der Vorlagen-Arten galt jede Vorlage als Signatur. Die
Migration setzt `kind` für die Vorlagen, die sich EINDEUTIG als Banner oder
Disclaimer zuordnen lassen — und lässt mehrdeutige (auch als Signatur genutzte)
in Ruhe, damit keine echte Signatur aus ihrem Dropdown fällt.

Der Test schlägt fehl, wenn man die Migration zurückbaut (die eindeutige
Zuordnung greift nicht mehr) ODER wenn man den Übermigrations-Schutz entfernt
(eine auch als Signatur genutzte Vorlage würde fälschlich umtypisiert).
"""
from __future__ import annotations

import json

import pytest

import config
import signature_engine
import vorlagen_typ


@pytest.fixture
def verz(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    return tmp_path


def _vorlage(verz, name, kind=None):
    """HTML + optional Meta anlegen. kind=None → keine Meta (gilt als signatur)."""
    (verz / f"{name}.html").write_text("<p>x</p>", encoding="utf-8")
    if kind is not None:
        meta = {"version": 1, "kind": kind, "blocks": [{"type": "text", "text": "x"}]}
        (verz / f"{name}.meta.json").write_text(json.dumps(meta), encoding="utf-8")


def _settings(monkeypatch, **werte):
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: werte.get(k, d))


def test_eindeutiges_banner_wird_migriert(verz, monkeypatch):
    """Nur als Banner zugewiesen (Richtlinie) → kind=banner."""
    _vorlage(verz, "Aktion", kind="signatur")
    _settings(monkeypatch, TEMPLATE_POLICIES={"sig": "default", "banner": "Aktion"})

    geaendert = vorlagen_typ.migriere_bestand()

    assert ("Aktion", "banner") in geaendert
    assert signature_engine.vorlagen_art("Aktion") == "banner"


def test_disclaimer_ueber_postfach_uebersteuerung(verz, monkeypatch):
    """Per-Postfach als disclaimer_template zugewiesen → kind=disclaimer."""
    _vorlage(verz, "Haftung", kind="signatur")
    _settings(monkeypatch, MAILBOX_CONFIG={"a@x.de": {"disclaimer_template": "Haftung"}})

    vorlagen_typ.migriere_bestand()

    assert signature_engine.vorlagen_art("Haftung") == "disclaimer"


def test_banner_aus_kampagne_wird_migriert(verz, monkeypatch):
    _vorlage(verz, "Sommer", kind="signatur")
    _settings(monkeypatch, BANNER_CAMPAIGNS=[{"banner": "Sommer"}])

    vorlagen_typ.migriere_bestand()

    assert signature_engine.vorlagen_art("Sommer") == "banner"


def test_auch_als_signatur_genutzt_bleibt_signatur(verz, monkeypatch):
    """DER Übermigrations-Schutz: dient eine Vorlage als Signatur UND als Banner,
    darf sie NICHT zum Banner werden — sonst fiele sie aus dem Signatur-Dropdown."""
    _vorlage(verz, "Doppel", kind="signatur")
    _settings(monkeypatch, TEMPLATE_POLICIES={"sig": "Doppel", "banner": "Doppel"})

    geaendert = vorlagen_typ.migriere_bestand()

    assert geaendert == []
    assert signature_engine.vorlagen_art("Doppel") == "signatur"


def test_ohne_meta_bekommt_nur_die_art(verz, monkeypatch):
    """Handgeschriebene Vorlage ohne Meta, nur als Banner zugewiesen.

    Bis v1.9.129 wurde sie ausgelassen — genau so blieb „Blog-Banner-Orange"
    auf der Produktions-VM in der Signaturliste stehen. Jetzt entsteht eine
    Meta, die NUR die Art trägt: keine Bausteine, damit der Editor sie weiter
    als Quelltext öffnet (`hat_bausteine` False)."""
    _vorlage(verz, "Handbanner", kind=None)
    _settings(monkeypatch, TEMPLATE_POLICIES={"banner": "Handbanner"})

    vorlagen_typ.migriere_bestand()

    meta = json.loads((verz / "Handbanner.meta.json").read_text())
    assert meta.get("kind") == "banner"
    assert "blocks" not in meta, "eine leere Bausteinliste täuschte einen Baukasten vor"
    assert signature_engine.vorlagen_art("Handbanner") == "banner"
    assert signature_engine.hat_bausteine("Handbanner") is False


# ── Mitgelieferte Vorlagen ───────────────────────────────────────────────────

@pytest.fixture
def seed(tmp_path, monkeypatch):
    """Eigenes Seed-Verzeichnis mit einem Banner, damit der Test nicht vom
    echten Seed abhängt (den prüft test_seed_traegt_die_art)."""
    import template_seed
    sd = tmp_path / "seed"
    sd.mkdir()
    (sd / "Banner.html").write_text("<p>b</p>", encoding="utf-8")
    (sd / "Banner.meta.json").write_text(json.dumps(
        {"version": 1, "kind": "banner", "blocks": [{"type": "text", "text": "b"}]}),
        encoding="utf-8")
    monkeypatch.setattr(template_seed, "SEED_DIR", str(sd))
    return sd


def _ohne_art(verz, name):
    (verz / f"{name}.html").write_text("<p>b</p>", encoding="utf-8")
    (verz / f"{name}.meta.json").write_text(json.dumps(
        {"version": 1, "blocks": [{"type": "text", "text": "b"}]}), encoding="utf-8")


def test_seed_banner_ohne_art_wird_banner(verz, seed, monkeypatch):
    """Der Befund der Produktions-VM: „Banner" und „Disclaimer" aus dem Seed
    standen als Signatur in der Liste, weil der Seed keine Art trug."""
    _ohne_art(verz, "Banner")
    _settings(monkeypatch)

    assert ("Banner", "banner") in vorlagen_typ.migriere_bestand()
    assert signature_engine.vorlagen_art("Banner") == "banner"


def test_seed_banner_mit_ausdruecklicher_art_bleibt(verz, seed, monkeypatch):
    """Läuft bei jedem Start: eine bewusste Wahl darf er nicht zurückdrehen."""
    _vorlage(verz, "Banner", kind="signatur")
    _settings(monkeypatch)

    assert vorlagen_typ.migriere_bestand() == []
    assert signature_engine.vorlagen_art("Banner") == "signatur"


def test_seed_banner_als_signatur_zugewiesen_bleibt(verz, seed, monkeypatch):
    """Zum eigenen Signaturentwurf umgebaut und zugewiesen → nicht anfassen."""
    _ohne_art(verz, "Banner")
    _settings(monkeypatch, TEMPLATE_POLICIES={"sig": "Banner"})

    assert vorlagen_typ.migriere_bestand() == []
    assert signature_engine.vorlagen_art("Banner") == "signatur"


def test_seed_traegt_die_art():
    """Die Wurzel: Jede mitgelieferte Vorlage mit Meta nennt ihre Art
    AUSDRÜCKLICH, und die Banner/Disclaimer-Vorlagen sind keine Signaturen."""
    import template_seed
    from pathlib import Path
    sd = Path(template_seed.SEED_DIR)
    arten = {f.name[:-len(".meta.json")]: json.loads(f.read_text()).get("kind")
             for f in sd.glob("*.meta.json")}
    assert all(arten.values()), f"Seed-Vorlagen ohne Art: {[n for n, a in arten.items() if not a]}"
    assert arten["Banner"] == "banner"
    assert arten["Disclaimer"] == "disclaimer"


def test_idempotent(verz, monkeypatch):
    """Zweiter Lauf ändert nichts mehr."""
    _vorlage(verz, "Aktion", kind="signatur")
    _settings(monkeypatch, TEMPLATE_POLICIES={"banner": "Aktion"})

    assert vorlagen_typ.migriere_bestand()  # erster Lauf ändert
    assert vorlagen_typ.migriere_bestand() == []  # zweiter nicht mehr


def test_bewusste_art_wird_nicht_ueberschrieben(verz, monkeypatch):
    """Eine schon als usermail markierte Vorlage bleibt usermail, selbst wenn sie
    (versehentlich) als Banner zugewiesen wäre."""
    _vorlage(verz, "Nachricht", kind="usermail")
    _settings(monkeypatch, TEMPLATE_POLICIES={"banner": "Nachricht"})

    vorlagen_typ.migriere_bestand()

    assert signature_engine.vorlagen_art("Nachricht") == "usermail"


def test_default_bleibt_unberuehrt(verz, monkeypatch):
    """default ist die Basissignatur — nie umtypisieren."""
    _vorlage(verz, "default", kind="signatur")
    _settings(monkeypatch, TEMPLATE_POLICIES={"banner": "default"})

    geaendert = vorlagen_typ.migriere_bestand()

    assert geaendert == []


# ── Aufräumen eingefrorener Vorlagenfelder (use_policy=true) ──────────────────

def _store(monkeypatch, mailbox_config):
    """settings_store.get liefert MAILBOX_CONFIG; update() fängt den Schreibwert."""
    import settings_store
    geschrieben = {}
    monkeypatch.setattr(settings_store, "get",
                        lambda k, d=None: mailbox_config if k == "MAILBOX_CONFIG" else d)
    monkeypatch.setattr(settings_store, "update", lambda d: geschrieben.update(d))
    return geschrieben


def test_bereinige_entfernt_felder_bei_use_policy_true(monkeypatch):
    cfg = {"guid1": {"sig": True, "use_policy": True,
                     "banner_template": "B", "oof_template": "O",
                     "known_addresses": ["a@x.de"]}}
    geschrieben = _store(monkeypatch, cfg)
    n = vorlagen_typ.bereinige_eingefrorene_vorlagen()
    assert n == 1
    neu = geschrieben["MAILBOX_CONFIG"]["guid1"]
    assert "banner_template" not in neu
    assert neu["sig"] is True and neu["known_addresses"] == ["a@x.de"]  # Rest bleibt
    # Wahl-Slots (sig/min/oof) sind seit v1.9.113 Nutzerwahl und gelten auch bei
    # use_policy=true — der bei jedem Start laufende Aufräumer darf sie NIE löschen.
    assert neu["oof_template"] == "O"


def test_bereinige_laesst_nutzerwahl_bei_use_policy_true_stehen(monkeypatch):
    cfg = {"g": {"sig": True, "use_policy": True, "template": "Eigene",
                 "min_template": "", "oof_template": "Urlaub"}}
    geschrieben = _store(monkeypatch, cfg)
    assert vorlagen_typ.bereinige_eingefrorene_vorlagen() == 0
    assert geschrieben == {}


def test_bereinige_laesst_use_policy_false_in_ruhe(monkeypatch):
    cfg = {"g": {"sig": True, "use_policy": False, "oof_template": "Eigene"}}
    geschrieben = _store(monkeypatch, cfg)
    assert vorlagen_typ.bereinige_eingefrorene_vorlagen() == 0
    assert geschrieben == {}        # kein Schreibvorgang


def test_bereinige_laesst_altbestand_ohne_use_policy_in_ruhe(monkeypatch):
    """Alt-Eintrag ohne use_policy-Schlüssel trug Vorlagen postfach-eigen → behalten."""
    cfg = {"g": {"sig": True, "template": "Firma"}}
    geschrieben = _store(monkeypatch, cfg)
    assert vorlagen_typ.bereinige_eingefrorene_vorlagen() == 0
    assert geschrieben == {}
