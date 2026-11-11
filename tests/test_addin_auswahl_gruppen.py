"""Add-in-Auswahl je Gruppe (v1.11.29).

Wunsch aus einem Kundentermin (11.11.2026): drei Firmen mit eigenem
Markenauftritt; je Gruppe sollen im Knopf-Add-in nur die passenden Vorlagen
wählbar sein. Bisher gab es die Freigabe nur je Postfach oder global.

Regeln: Gruppen-Regel `applies_to: "addin"` mit Namensliste oder "*". Mehrere
passende Gruppen → Vereinigung. Keine passende → globale Freigabe. Die
Standardvorlage des Postfachs bleibt immer wählbar.
"""
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import policies  # noqa: E402

VORLAGEN = ["default", "FirmaA", "FirmaA-kurz", "FirmaB", "FirmaC"]


@pytest.fixture
def stand(monkeypatch):
    werte = {
        "MAILBOX_CONFIG": {"anna@a.at": {"sig": True}, "bert@b.at": {"sig": True},
                           "chris@x.at": {"sig": True}, "dora@x.at": {"sig": True}},
        "TEMPLATE_POLICIES": {"sig": "default", "addin": "*"},
        "CUSTOM_POLICIES": [
            {"condition_type": "group", "group_name": "Firma A", "applies_to": "addin",
             "template": ["FirmaA", "FirmaA-kurz"]},
            {"condition_type": "group", "group_name": "Firma B", "applies_to": "addin",
             "template": ["FirmaB"]},
            {"condition_type": "group", "group_name": "Firma B", "applies_to": "sig",
             "template": "FirmaB"},
            {"condition_type": "group", "group_name": "Admins", "applies_to": "addin",
             "template": "*"},
        ],
    }
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: werte.get(k, d))
    import gruppen
    monkeypatch.setattr(gruppen, "interne_gruppen", lambda: {
        "Firma A": ["anna@a.at", "chris@x.at"], "Firma B": ["bert@b.at", "chris@x.at"],
        "Admins": ["dora@x.at"]})
    import mailbox_match
    monkeypatch.setattr(mailbox_match, "match_sender_key", lambda cfg, s: s if s in cfg else None)
    monkeypatch.setattr(mailbox_match, "match_sender",
                        lambda cfg, s: cfg.get(s) or {})
    import signature_engine
    monkeypatch.setattr(signature_engine, "list_templates", lambda: list(VORLAGEN))
    return werte


def test_eine_gruppe(stand):
    assert policies.addin_freigabe("anna@a.at") == ["FirmaA", "FirmaA-kurz"]


def test_mehrere_gruppen_ergeben_die_vereinigung(stand):
    assert policies.addin_freigabe("chris@x.at") == ["FirmaA", "FirmaA-kurz", "FirmaB"]


def test_alle(stand):
    assert policies.addin_freigabe("dora@x.at") == "*"


def test_ohne_gruppenregel_keine_aussage(stand):
    stand["MAILBOX_CONFIG"]["erik@x.at"] = {"sig": True}
    assert policies.addin_freigabe("erik@x.at") is None
    stand["CUSTOM_POLICIES"] = []
    assert policies.addin_freigabe("anna@a.at") is None


def test_einzelwert_regeln_unberuehrt(stand):
    """Die Add-in-Regel darf die Standardsignatur je Gruppe nicht verdrängen."""
    assert policies.standards("bert@b.at")["sig"] == "FirmaB"
    assert policies.standards("anna@a.at")["sig"] == "default"


def _im_addin(email):
    from webui.routen import addin
    cfg = addin._addin_cfg(email)
    return addin._addin_allowed_templates(email, cfg)


def test_addin_zeigt_die_gruppenauswahl_und_den_standard(stand):
    assert _im_addin("anna@a.at") == ["default", "FirmaA", "FirmaA-kurz"]
    assert _im_addin("bert@b.at") == ["FirmaB"], "Standard der Gruppe ist schon dabei"
    # chris ist auch in „Firma B“ → Standardsignatur FirmaB, steht schon in der Liste
    assert _im_addin("chris@x.at") == ["FirmaA", "FirmaA-kurz", "FirmaB"]


def test_ohne_gruppe_gilt_die_globale_freigabe(stand):
    stand["MAILBOX_CONFIG"]["erik@x.at"] = {"sig": True}
    assert _im_addin("erik@x.at") == VORLAGEN


def test_postfach_ohne_richtlinie_behaelt_eigene_liste(stand):
    stand["MAILBOX_CONFIG"]["anna@a.at"] = {"sig": True, "use_policy": False,
                                           "addin_templates": ["FirmaC"]}
    assert _im_addin("anna@a.at") == ["default", "FirmaC"]


def test_typerkennung_mit_liste_und_alle(stand):
    import vorlagen_typ
    arten = vorlagen_typ.zuweisungen_nach_art()
    assert "*" not in arten, "„Alle“ ist kein Vorlagenname"
    assert arten.get("FirmaA-kurz") == {"signatur"}


def test_umbenennen_erreicht_die_liste(stand, monkeypatch):
    from webui.routen import vorlagen
    gespeichert = {}
    import settings_store
    monkeypatch.setattr(settings_store, "update", lambda d: gespeichert.update(d))
    vorlagen._vorlagenverweise_umbenennen("FirmaA", "FirmaA-neu")
    assert gespeichert["CUSTOM_POLICIES"][0]["template"] == ["FirmaA-neu", "FirmaA-kurz"]
