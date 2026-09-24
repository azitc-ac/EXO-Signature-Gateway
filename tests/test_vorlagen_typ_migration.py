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
