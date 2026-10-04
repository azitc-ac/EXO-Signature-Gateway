"""Funktionsschalter „Zentrale Abwesenheiten verwalten" (ABWESENHEIT_AKTIV).

Aus → Menüpunkt, Spalte und Seite weg, UND kein Hintergrund-Abgleich: eine
ausgeblendete Funktion darf nicht unsichtbar weiterlaufen. Bestandsanlagen, die
Abwesenheiten nutzen, bekommen den Schalter per Migration an.
"""
from __future__ import annotations

import asyncio

import pytest

import abwesenheit
import settings_store


def _store(monkeypatch, **d):
    monkeypatch.setattr(settings_store, "get", lambda k, dd=None: d.get(k, dd))


def test_abgleich_steht_wenn_funktion_aus(monkeypatch):
    _store(monkeypatch, ABWESENHEIT_AKTIV=False, OOO_ENABLED=True)
    assert abwesenheit.zentral_aktiv() is False
    assert asyncio.run(abwesenheit.poll_alle())["aktiv"] is False


def test_abgleich_braucht_beide_schalter(monkeypatch):
    _store(monkeypatch, ABWESENHEIT_AKTIV=True, OOO_ENABLED=False)
    assert abwesenheit.zentral_aktiv() is False
    _store(monkeypatch, ABWESENHEIT_AKTIV=True, OOO_ENABLED=True)
    assert abwesenheit.zentral_aktiv() is True


@pytest.mark.parametrize("daten, erwartet", [
    ({}, False),
    ({"OOO_ENABLED": True}, True),
    ({"OOO_CALENDAR_AUTO": True}, True),
    ({"TEMPLATE_POLICIES": {"oof": "Urlaub"}}, True),
    ({"CUSTOM_POLICIES": [{"applies_to": "oof", "template": "X"}]}, True),
    ({"MAILBOX_CONFIG": {"g": {"oof_template": "X"}}}, True),
    ({"MAILBOX_CONFIG": {"g": {"oof_template": ""}}}, False),
    ({"MAILBOX_CONFIG": {"g": {"ooo_calendar": True}}}, True),
])
def test_migration_schaltet_bei_nutzung_ein(daten, erwartet):
    assert settings_store._migrate_v4_to_v5(dict(daten))["ABWESENHEIT_AKTIV"] is erwartet


def test_migration_nur_einmal():
    """Danach entscheidet der Betreiber — ein Neustart darf ein bewusstes Aus nicht
    wieder einschalten."""
    daten, geaendert = settings_store._run_migrations(
        {"_SCHEMA_VERSION": 5, "OOO_ENABLED": True, "ABWESENHEIT_AKTIV": False})
    assert not geaendert and daten["ABWESENHEIT_AKTIV"] is False


def test_self_service_bietet_abwesenheit_nicht_an_wenn_aus(monkeypatch):
    from webui.routen import selfservice as sv
    mb = {"g1": {"known_addresses": ["a@x.de"], "primary": "a@x.de", "self_templates": True}}
    _store(monkeypatch, MAILBOX_CONFIG=mb, SELF_TEMPLATE_KATEGORIEN=["sig", "oof"],
           INTERNAL_GROUPS={}, ABWESENHEIT_AKTIV=False)
    assert sv._waehlbar("a@x.de") == {"sig": True, "min": False, "oof": False}
