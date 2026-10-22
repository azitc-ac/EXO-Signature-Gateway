"""Standardbezugsweg für neue Postfächer (SMIME_AUTO_ENROLL_CA).

Ein neues Postfach stand auf „Assistiert manuell“: „Auto-Enroll“ war
ausgegraut, bis man hinter „…“ den Bezugsweg wählte. Jetzt gilt die Vorgabe —
aus EINER Quelle (ca_backends.bezugsweg) für Anzeige und Bestellung.
"""
from __future__ import annotations

import pytest

import ca_backends
import settings_store
from ca_backends import registry


@pytest.fixture
def werte(monkeypatch):
    w = {"SMIME_AUTO_ENROLL_CA": ""}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: w.get(k, d))
    return w


def test_eigener_weg_vor_vorgabe(werte):
    werte["SMIME_AUTO_ENROLL_CA"] = "castle_acme"
    assert ca_backends.bezugsweg({"backend": "assisted_manual"}) == "assisted_manual"


def test_vorgabe_fuer_postfach_ohne_eigenen_weg(werte):
    werte["SMIME_AUTO_ENROLL_CA"] = "castle_acme"
    assert ca_backends.bezugsweg({}) == "castle_acme"
    assert ca_backends.bezugsweg(None) == "castle_acme"
    assert ca_backends.bezugsweg({"backend": ""}) == "castle_acme"


def test_ohne_vorgabe_assistiert_manuell(werte):
    assert ca_backends.bezugsweg({}) == "assisted_manual"


@pytest.mark.parametrize("vorgabe", ["assisted_manual", "gibtsnicht"])
def test_vorgabe_die_nicht_selbst_bestellt_gilt_nicht(werte, vorgabe):
    # get_backend("gibtsnicht") liefert still assisted_manual — der Name passt
    # dann nicht und die Vorgabe zählt nicht.
    werte["SMIME_AUTO_ENROLL_CA"] = vorgabe
    assert ca_backends.bezugsweg({}) == "assisted_manual"


def test_hub_weg_als_vorgabe(werte, monkeypatch):
    class _Fake:
        def get_name(self): return "hub:certum"
        def can_auto_renew(self): return True
    monkeypatch.setattr(registry, "_hub_backend", lambda pid: _Fake())
    werte["SMIME_AUTO_ENROLL_CA"] = "hub:certum"
    assert ca_backends.bezugsweg({}) == "hub:certum"
