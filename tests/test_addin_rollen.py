"""Fehlen die Benutzerrollen für Add-ins, laufen die Add-ins nur bei Admins.

Gemessen 26.10.2026: Die Standard-Rollenzuweisungsrichtlinie des Tenants war
leer. Beim Admin lief alles (eine Admin-Rolle hebt die Sperre auf), bei
erika@ zeigte Outlook iOS „Der Zugriff auf Add-Ins wurde von Ihrer
Organisation eingeschränkt“, und das Verfassen-Add-in startete nie. Nach dem
Anhaken der drei Rollen sofort „geliefert (iOS)“. Die Abnahme muss das
melden — wer einrichtet, ist Admin und sieht es sonst nie.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import abnahme          # noqa: E402
import addin_rollen     # noqa: E402
import settings_store   # noqa: E402
import waechter_regel   # noqa: E402

ALLE = list(addin_rollen.SOLL)


def test_vollstaendig_ist_ok():
    assert addin_rollen.luecken({"apps_for_office": True, "rollen": ALLE + ["MyBaseOptions"]}) == []


def test_leere_richtlinie_meldet_alle_drei():
    """Der Fall vom 26.10.: Richtlinie ohne eine einzige Rolle."""
    assert addin_rollen.luecken({"apps_for_office": True, "rollen": []}) == ALLE


def test_jede_einzelne_rolle_wird_vermisst():
    for r in ALLE:
        rest = [x for x in ALLE if x != r]
        assert addin_rollen.luecken({"apps_for_office": True, "rollen": rest}) == [r]


def test_gross_kleinschreibung_egal():
    assert addin_rollen.luecken({"apps_for_office": True,
                                 "rollen": [r.upper() for r in ALLE]}) == []


def test_apps_for_office_aus_wird_gemeldet():
    f = addin_rollen.luecken({"apps_for_office": False, "rollen": ALLE})
    assert len(f) == 1 and "AppsForOfficeEnabled" in f[0]


def _ps(monkeypatch, antwort):
    monkeypatch.setattr(waechter_regel, "ps_regelskript", lambda *a, **kw: antwort)


def test_abnahme(monkeypatch):
    assert abnahme._addin_rollen in abnahme.PRUEFUNGEN
    monkeypatch.setattr(settings_store, "get",
                        lambda k, d=None: True if k == "ADDIN_ENABLED" else d)
    _ps(monkeypatch, {"ok": True, "apps_for_office": True,
                      "richtlinie": "Default Role Assignment Policy", "rollen": []})
    p = abnahme._addin_rollen()
    assert p["zustand"] == abnahme.OFFEN
    assert "My Custom Apps" in p["befund"] and "Benutzerrollen" in p["zu_tun"]
    _ps(monkeypatch, {"ok": True, "apps_for_office": True,
                      "richtlinie": "Default Role Assignment Policy", "rollen": ALLE})
    assert abnahme._addin_rollen()["zustand"] == abnahme.OK
    _ps(monkeypatch, None)
    assert abnahme._addin_rollen()["zustand"] == abnahme.UNBEKANNT
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: d)
    assert abnahme._addin_rollen()["zustand"] == abnahme.ENTFAELLT


def test_skript_liest_nur():
    """Nur Get-Cmdlets — die Richtlinie ist eine Entscheidung im Tenant."""
    code = (WURZEL / "app" / "scripts" / "get_addin_rollen.ps1").read_text(encoding="utf-8")
    code = "\n".join(z for z in code.splitlines() if not z.lstrip().startswith("#"))
    verben = set(re.findall(r"\b(Get|Set|New|Remove|Add|Enable|Disable|Update)-[A-Z]\w+", code))
    assert verben <= {"Get"}, verben
