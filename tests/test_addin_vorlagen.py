"""Add-in: Beide Endpunkte (/api/addin/templates und /api/addin/signature) teilen
EINE wirksame Sicht (_addin_cfg).

Bis v1.9.112 wendete nur /templates die Richtlinie an. /signature las das rohe
Postfach-Feld: Bei use_policy=true bot das Add-in „alle Vorlagen" an, der Abruf
wies die gewählte aber ab und fiel auf das (oft leere) Feld zurück.
"""
from __future__ import annotations

import settings_store
import signature_engine
from webui.routen import addin


def _store(monkeypatch, mb, tp, custom=None, groups=None):
    data = {"MAILBOX_CONFIG": mb, "TEMPLATE_POLICIES": tp,
            "CUSTOM_POLICIES": custom or [], "INTERNAL_GROUPS": groups or {}}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: data.get(k, d))
    monkeypatch.setattr(signature_engine, "list_templates", lambda: ["default", "Kurz", "Lang"])


def test_richtlinie_gibt_die_freigabeliste(monkeypatch):
    _store(monkeypatch, {"a@x.de": {"use_policy": True}}, {"sig": "Kurz", "addin": "*"})
    cfg = addin._addin_cfg("a@x.de")
    assert addin._addin_allowed_templates("a@x.de", cfg) == ["default", "Kurz", "Lang"]
    assert cfg["template"] == "Kurz"


def test_standard_folgt_der_eigenen_wahl_und_der_gruppe(monkeypatch):
    custom = [{"condition_type": "group", "group_name": "G", "applies_to": "sig", "template": "Lang"}]
    _store(monkeypatch, {"a@x.de": {"use_policy": True}}, {"sig": "Kurz"}, custom, {"G": ["a@x.de"]})
    assert addin._addin_cfg("a@x.de")["template"] == "Lang"
    _store(monkeypatch, {"a@x.de": {"use_policy": True, "template": "default"}}, {"sig": "Kurz"},
           custom, {"G": ["a@x.de"]})
    assert addin._addin_cfg("a@x.de")["template"] == "default"


def test_ausnahme_nutzt_die_eigene_liste(monkeypatch):
    _store(monkeypatch, {"a@x.de": {"use_policy": False, "addin_templates": ["Lang"]}},
           {"sig": "Kurz", "addin": "*"})
    cfg = addin._addin_cfg("a@x.de")
    assert addin._addin_allowed_templates("a@x.de", cfg) == ["Kurz", "Lang"]
