"""Veraltetes Knopf-Add-in wird eine Aufgabe, nicht nur eine Warnung je Postfach.

Bis v1.12.84 gab es `addin-fassung` nur für das Verfassen-Add-in. Ein Knopf-Add-in
(„Signatur und Abwesenheit“) auf alter Fassung erschien allein in der
Postfachprüfung mit Warndreieck; im Sammelpunkt stand nichts. Jetzt teilen beide
Add-ins EINE Prüfung (`abnahme._addin_fassung_art`), je Art eine Aufgabe.
"""
from test_addin_verteilung import KNOPF, T0, VERF, _uhr, stand  # noqa: F401

import abnahme
import aufgaben
import health_check

KNOPF_SOLL = "1.9.163"


def _knopf_postfaecher(werte, fassungen: dict):
    health_check._verteilung_vormerken({e: {"addins": {KNOPF: v}} for e, v in fassungen.items()})
    werte["MAILBOX_HEALTH"] = {
        e: {"checks": {"addin_knopf": health_check._check_addins({VERF: "", KNOPF: v})["knopf"]}}
        for e, v in fassungen.items()}


def _offen(kennung):
    aufgaben.vergessen()
    return any(a["kennung"] == kennung for a in aufgaben.offene())


def test_veralteter_knopf_wird_aufgabe(stand, monkeypatch):
    _uhr(monkeypatch, T0)
    _knopf_postfaecher(stand, {"a@x.de": "1.9.100", "b@x.de": "1.9.100"})
    p = abnahme._addin_knopf_fassung()
    assert p["zustand"] == abnahme.OFFEN, p
    assert "Knopf" in p["zu_tun"] and "Aktualisieren" in p["zu_tun"]
    assert _offen("addin-knopf-fassung")


def test_aktueller_knopf_keine_aufgabe(stand, monkeypatch):
    _uhr(monkeypatch, T0)
    _knopf_postfaecher(stand, {"a@x.de": KNOPF_SOLL})
    assert abnahme._addin_knopf_fassung()["zustand"] == abnahme.OK
    assert not _offen("addin-knopf-fassung")


def test_knopf_steht_in_den_pruefungen():
    assert abnahme._addin_knopf_fassung in abnahme.PRUEFUNGEN


def test_jedes_addin_hat_eine_aufgabe():
    """Strukturregel: Jede Add-in-Art aus health_check._ADDINS hat eine Prüfung in
    abnahme.PRUEFUNGEN, und die steckt in einer Aufgabe. Ein drittes Add-in ohne
    Aufgabe fiele hier auf, nicht erst beim Kunden."""
    arten = {art for _s, art in health_check._ADDINS}
    pruef = {"verfassen": abnahme._addin_fassung, "knopf": abnahme._addin_knopf_fassung}
    assert arten == set(pruef), f"Add-in ohne Prüfung: {arten - set(pruef)}"
    in_aufgaben = {a.pruefung for a in aufgaben.AUFGABEN}
    for art, p in pruef.items():
        assert p in abnahme.PRUEFUNGEN and p in in_aufgaben, art
