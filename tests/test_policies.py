"""Gemeinsame Richtlinien-Auflösung (aus handler.py herausgelöst).

Der Signatur-Hot-Path und die zentrale Abwesenheitsnotiz teilen diese Auflösung.
Der Test hält ihr Verhalten fest: globale Richtlinie, Gruppen-Übersteuerung
(first-match-wins) und die Grenze use_policy=false.
"""
from __future__ import annotations

import pytest

import policies
import settings_store


def _store(monkeypatch, **d):
    monkeypatch.setattr(settings_store, "get", lambda k, dd=None: d.get(k, dd))


def test_ohne_gruppen_gilt_die_globale_richtlinie(monkeypatch):
    _store(monkeypatch, TEMPLATE_POLICIES={"sig": "S", "oof": "O"},
           CUSTOM_POLICIES=[], INTERNAL_GROUPS={})
    pol, use = policies.resolve_policies(
        "a@x.de", {"a@x.de": {"use_policy": True}}, {"use_policy": True})
    assert use is True
    assert pol["sig"] == "S" and pol["oof"] == "O"


def test_gruppe_uebersteuert_den_slot(monkeypatch):
    mc = {"a@x.de": {"use_policy": True}}
    _store(monkeypatch, TEMPLATE_POLICIES={"oof": "Global"},
           CUSTOM_POLICIES=[{"condition_type": "group", "group_name": "G",
                             "applies_to": "oof", "template": "Gruppe"}],
           INTERNAL_GROUPS={"G": ["a@x.de"]})
    pol, _ = policies.resolve_policies("a@x.de", mc, {"use_policy": True})
    assert pol["oof"] == "Gruppe"


def test_use_policy_false_ohne_uebersteuerung(monkeypatch):
    """Folgt das Postfach den Richtlinien nicht, greift keine Gruppe — und der
    Aufrufer weiß es an use_pol=False."""
    mc = {"a@x.de": {"use_policy": False}}
    _store(monkeypatch, TEMPLATE_POLICIES={"oof": "Global"},
           CUSTOM_POLICIES=[{"condition_type": "group", "group_name": "G",
                             "applies_to": "oof", "template": "Gruppe"}],
           INTERNAL_GROUPS={"G": ["a@x.de"]})
    pol, use = policies.resolve_policies("a@x.de", mc, {"use_policy": False})
    assert use is False
    assert pol["oof"] == "Global"


def test_first_match_wins(monkeypatch):
    mc = {"a@x.de": {"use_policy": True}}
    _store(monkeypatch, TEMPLATE_POLICIES={},
           CUSTOM_POLICIES=[
               {"condition_type": "group", "group_name": "G", "applies_to": "oof", "template": "Erste"},
               {"condition_type": "group", "group_name": "G", "applies_to": "oof", "template": "Zweite"},
           ],
           INTERNAL_GROUPS={"G": ["a@x.de"]})
    pol, _ = policies.resolve_policies("a@x.de", mc, {"use_policy": True})
    assert pol["oof"] == "Erste"


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
