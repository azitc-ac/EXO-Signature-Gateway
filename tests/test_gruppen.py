"""Vordefinierte Gruppe „Alle Postfächer" (gruppen.py).

Ihre Mitglieder werden bei jedem Zugriff aus MAILBOX_CONFIG berechnet — ein neu
aktiviertes Postfach ist sofort Mitglied. Damit das überall gilt, lesen alle
Stellen Gruppen über `gruppen.interne_gruppen()`; der Strukturtest unten
verhindert, dass ein neuer Leser an ihr vorbei `INTERNAL_GROUPS` direkt liest.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import gruppen
import policies
import settings_store


def _store(monkeypatch, **d):
    monkeypatch.setattr(settings_store, "get", lambda k, dd=None: d.get(k, dd))


def test_alle_postfaecher_enthaelt_auch_neue(monkeypatch):
    mc = {"g1": {"sig": True}}
    _store(monkeypatch, MAILBOX_CONFIG=mc, INTERNAL_GROUPS={"Team": ["g1"]})
    assert gruppen.interne_gruppen()[gruppen.ALLE] == ["g1"]
    mc["g2"] = {"sig": True}                      # neu aktiviert — niemand pflegt die Gruppe
    assert gruppen.interne_gruppen()[gruppen.ALLE] == ["g1", "g2"]


def test_alle_postfaecher_steht_zuletzt_und_verdraengt_gleichnamige(monkeypatch):
    _store(monkeypatch, MAILBOX_CONFIG={"g1": {}},
           INTERNAL_GROUPS={gruppen.ALLE: ["fremd"], "Team": ["g1"]})
    g = gruppen.interne_gruppen()
    assert list(g) == ["Team", gruppen.ALLE]
    assert g[gruppen.ALLE] == ["g1"], "gespeicherte Kopie darf die berechnete nicht ersetzen"


def test_gruppenregel_auf_alle_gilt_fuer_neues_postfach(monkeypatch):
    mc = {"a@x.de": {}, "neu@x.de": {}}
    _store(monkeypatch, MAILBOX_CONFIG=mc, INTERNAL_GROUPS={}, TEMPLATE_POLICIES={},
           CUSTOM_POLICIES=[{"condition_type": "group", "group_name": gruppen.ALLE,
                             "applies_to": "banner", "template": "Alle"}])
    assert policies.vorlage_fuer("neu@x.de", "banner", mc, mc["neu@x.de"]) == "Alle"


def test_gezielte_gruppe_schlaegt_alle_bei_variablen(monkeypatch):
    mc = {"a@x.de": {}}
    _store(monkeypatch, MAILBOX_CONFIG=mc, INTERNAL_GROUPS={"Vertrieb": ["a@x.de"]},
           GROUP_VARS={gruppen.ALLE: {"v": "allgemein"}, "Vertrieb": {"v": "gezielt"}})
    assert policies.group_vars_for("a@x.de", mc) == {"v": "gezielt"}


def test_speichern_persistiert_die_berechnete_gruppe_nicht(monkeypatch):
    from webui.routen import settings as sroute
    geschrieben: dict = {}
    monkeypatch.setattr(settings_store, "update", lambda d: geschrieben.update(d))

    class _Req:
        async def json(self):
            return {"groups": {gruppen.ALLE: ["g1"], "Team": ["g1"]},
                    "group_vars": {gruppen.ALLE: {"v": "1"}, "Weg": {"v": "2"}}}
    asyncio.run(sroute.api_save_internal_groups(_Req()))
    assert geschrieben["INTERNAL_GROUPS"] == {"Team": ["g1"]}
    assert geschrieben["GROUP_VARS"] == {gruppen.ALLE: {"v": "1"}}, \
        "Variablen der berechneten Gruppe müssen speicherbar sein, verwaiste nicht"


def test_niemand_liest_internal_groups_an_gruppen_py_vorbei():
    """Executable statt Prosa: ein direkter Leser übersähe „Alle Postfächer"."""
    app = Path(__file__).resolve().parent.parent / "app"
    muster = re.compile(r"""get\(\s*["']INTERNAL_GROUPS["']""")
    treffer = [f"{p.relative_to(app)}:{i}"
               for p in app.rglob("*.py") if p.name != "gruppen.py"
               for i, zeile in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
               if muster.search(zeile)]
    assert not treffer, f"INTERNAL_GROUPS direkt gelesen — gruppen.interne_gruppen() nutzen: {treffer}"
