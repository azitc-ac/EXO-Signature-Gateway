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


def test_ohne_meta_wird_nicht_angefasst(verz, monkeypatch):
    """Handgeschriebene Vorlage ohne Meta: keine leere Baukasten-Meta erzeugen."""
    _vorlage(verz, "Handbanner", kind=None)
    _settings(monkeypatch, TEMPLATE_POLICIES={"banner": "Handbanner"})

    vorlagen_typ.migriere_bestand()

    assert not (verz / "Handbanner.meta.json").exists()


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
