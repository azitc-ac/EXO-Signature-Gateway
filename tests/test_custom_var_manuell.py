"""Manuelle Custom-Variablen (ohne Entra-Feld).

Eine eigene Variable darf auch OHNE AD-Mapping angelegt werden — ihr Wert kommt
dann aus einem Benutzer-Override oder einer Gruppen-Zuweisung (z.B. Vertreter).
Der Backend-Vertrag dafür: ein leeres `entra_field` darf die Graph-$select-Liste
NICHT verunreinigen (sonst schlägt der Abruf mit einem ungültigen Feld fehl).
"""
from __future__ import annotations

import graph_client
import settings_store


def test_manueller_var_nicht_in_select(monkeypatch):
    monkeypatch.setattr(settings_store, "get",
                        lambda k, d=None: {"CUSTOM_TEMPLATE_VARS": [
                            {"name": "vertreter_mail", "entra_field": ""},        # manuell
                            {"name": "abteilungscode", "entra_field": "extensionAttribute5"},
                        ]}.get(k, d))
    sel = graph_client._build_select_fields()
    felder = sel.split(",")
    assert "" not in felder, "leeres Entra-Feld verunreinigt die $select-Liste"
    # Das echte Mapping wird weiterhin berücksichtigt.
    assert "onPremisesExtensionAttributes" in felder


def test_leere_var_liste_ist_unschaedlich(monkeypatch):
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: {}.get(k, d))
    sel = graph_client._build_select_fields()
    assert sel and "" not in sel.split(",")
