"""Texte im Statusfenster der Postfachliste (28.11.2026, Telefon-Screenshot):
rohe PowerShell-Meldungen („Get-App: ||The operation couldn't be performed …“)
und rohe Speicherausgabe mit Farbcodes."""
from __future__ import annotations

from pathlib import Path

import pytest

import health_check

WURZEL = Path(__file__).resolve().parent.parent


@pytest.fixture
def soll(monkeypatch):
    arten = [art for _s, art in health_check._ADDINS]
    monkeypatch.setattr(health_check, "_addin_soll",
                        lambda: [{"art": a, "id": f"id-{a}", "version": "1.0.0"} for a in arten])
    import addin_verteilung
    monkeypatch.setattr(addin_verteilung, "gesehen", lambda *a, **k: None)
    from webui.routen import addin
    monkeypatch.setattr(addin, "addin_namen", lambda: {a: a for a in arten})
    return arten


def test_nicht_gefunden_heisst_nicht_installiert(soll):
    roh = "!||The operation couldn't be performed because object 'ac34fa25' couldn't be found."
    erg = health_check._check_addins({f"id-{a}": roh for a in soll})
    for a in soll:
        assert erg[a]["status"] == "skip"
        assert "Get-App" not in erg[a]["detail"] and "couldn't" not in erg[a]["detail"]


def test_anderer_fehler_lesbar(soll):
    erg = health_check._check_addins({f"id-{a}": "!||Sitzung abgelaufen" for a in soll})
    for a in soll:
        assert erg[a]["detail"] == "Nicht abgefragt — Exchange-Abfrage fehlgeschlagen: Sitzung abgelaufen"


def test_statusfenster_und_speichermeldung():
    html = (WURZEL / "app/webui/templates/mailboxes.html").read_text(encoding="utf-8")
    assert "tr.gesundheit-zeile { display: grid; grid-template-columns: 1fr auto }" in html
    assert '<tr class="gesundheit-zeile"' in html
    # Unter „Gespeichert“ keine PowerShell-Ausgabe mehr
    assert "'Gespeichert (lokal + Verteilerliste aktualisiert).' + _zurueckgestelltText(gespeichert);" in html


def test_geteiltes_postfach_ohne_addin_ist_kein_befund(soll, monkeypatch):
    """Microsoft: Add-ins nicht an geteilte Postfächer verteilen, sondern an das
    Hauptpostfach der Nutzer. Ein geteiltes Postfach ohne Add-in ist also kein
    Zuweisungsfehler und zählt in der Abnahme nicht mit."""
    erg = health_check._addins_geteilt(health_check._check_addins({f"id-{a}": "" for a in soll}))
    for a in soll:
        assert erg[a]["status"] == "skip" and "geteilten" in erg[a]["detail"]
        assert "soll" not in erg[a] and "lage" not in erg[a]
        assert erg[a]["name"] == a
    import abnahme
    import settings_store
    gesundheit = {"geteilt@firma.at": {"checks": {"addin_verfassen": erg["verfassen"]}}}
    monkeypatch.setattr(settings_store, "get",
                        lambda k, d=None: gesundheit if k == "MAILBOX_HEALTH" else d)
    assert abnahme._addin_fassung()["zustand"] == abnahme.UNBEKANNT   # nicht als Zuweisung gezählt


def test_pruefung_nimmt_geteilte_heraus():
    import inspect
    quelle = inspect.getsource(health_check.run_checks_for_mailbox)
    assert "if exo_mailboxes.ist_geteilt(email):\n        addins = _addins_geteilt(addins)" in quelle
