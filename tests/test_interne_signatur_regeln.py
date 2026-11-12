"""Interne Signatur: Wahlregel, Definition „intern“, Add-in-Schnittstelle,
Baukasten-Variable `bearbeiter` (v1.11.56)."""
import asyncio
import json

import pytest

import intern
import mail_processor
import policies

V = {"sig": "Sig", "min": "Min", "intern": "Intern"}


# ── Wahlregel (EINE für Gateway und Add-in) ────────────────────────────────────

@pytest.mark.parametrize("vorlagen, kette, alle_intern, vorrang, erwartet", [
    (V, None, False, False, ("Sig", "standard")),
    (V, None, True, False, ("Intern", "intern")),
    (V, False, True, False, ("Intern", "intern")),
    (V, True, True, False, ("Min", "antwort")),          # Antwort hat Vorrang (Standard)
    (V, True, True, True, ("Intern", "intern")),         # Schalter kehrt um
    (V, True, False, True, ("Min", "antwort")),
    ({**V, "intern": ""}, None, True, False, ("Sig", "standard")),
    ({**V, "min": ""}, True, False, False, ("", "antwort")),
])
def test_vorlage_waehlen(vorlagen, kette, alle_intern, vorrang, erwartet):
    assert policies.vorlage_waehlen(vorlagen, kette, alle_intern, vorrang) == erwartet


def test_slot_ist_registriert():
    import signature_engine
    assert "intern" in policies.WAHL_SLOTS
    assert policies.SLOT_FELD["intern"] == "intern_template"
    assert signature_engine.SLOT_ART["intern"] == "signatur"


# ── Definition „intern“ ────────────────────────────────────────────────────────

@pytest.fixture
def tenant(monkeypatch):
    monkeypatch.setattr(intern, "_postfaecher", lambda: {"max@firma.de", "info@andere.de"})
    monkeypatch.setattr(intern, "_domaenen", lambda: {"firma.de", "firma.at"})


@pytest.mark.parametrize("adressen, erwartet", [
    (["max@firma.de"], True),
    (["Verteiler@FIRMA.at"], True),               # Domäne, Groß-/Kleinschreibung egal
    (["info@andere.de"], True),                   # Postfach unter fremder Domäne
    (["max@firma.de", "kunde@extern.de"], False),
    (["max@firma.de.extern.com"], False),         # kein Suffix-Trick
    ([], False),                                  # keine Empfänger: nicht intern
    (["kaputt"], False),
])
def test_alle_intern(tenant, adressen, erwartet):
    assert intern.alle_intern(adressen) is erwartet


def test_ohne_wissen_nie_intern(monkeypatch):
    monkeypatch.setattr(intern, "_postfaecher", lambda: set())
    monkeypatch.setattr(intern, "_domaenen", lambda: set())
    assert intern.alle_intern(["max@firma.de"]) is False


# ── Erkennung der Add-in-Signatur am Text ──────────────────────────────────────

def test_ist_vorlage():
    i = "<p>Max Muster</p><p>Intern: Raum 3.14</p><p>Durchwahl 123</p>"
    s = "<p>Max Muster</p><p>Musterfirma GmbH</p><p>+49 30 123</p>"
    assert mail_processor.ist_vorlage(i, i, s)
    assert not mail_processor.ist_vorlage(s, i, s)
    assert not mail_processor.ist_vorlage("", i, s)
    assert not mail_processor.ist_vorlage(i, i, i)          # nicht unterscheidbar → nein


# ── Baukasten: Variable der schreibenden Person ────────────────────────────────

def test_bearbeiter_variable_im_baukasten():
    import template_builder
    assert template_builder._resolve_var("bearbeiter.displayName") == "bearbeiter.displayName"
    assert template_builder._resolve_var("bearbeiter.__class__") is None


def test_bearbeiter_ist_ohne_auftrag_der_inhaber(monkeypatch):
    import graph_client
    import jinja2
    import signature_engine
    env = jinja2.Environment(loader=jinja2.DictLoader({"x.html": "{{ bearbeiter.mail }}",
                                                      "x.txt": "{{ bearbeiter.mail }}"}))
    monkeypatch.setattr(signature_engine, "_get_env", lambda: env)
    monkeypatch.setattr(signature_engine, "_resolve_template_names", lambda n: ("x.html", "x.txt"))
    inhaber = graph_client.UserData(mail="team@firma.de")
    person = graph_client.UserData(mail="max@firma.de")
    assert signature_engine.render(inhaber, "x")[0] == "team@firma.de"
    assert signature_engine.render(inhaber, "x", {"bearbeiter": person})[0] == "max@firma.de"


# ── Add-in-Schnittstelle ───────────────────────────────────────────────────────

from test_addin_verfassen import _Req, _token, umgebung  # noqa: E402,F401


class _ReqQ(_Req):
    def __init__(self, auth, **q):
        super().__init__(auth)
        self.query_params = q


@pytest.fixture
def api(umgebung, monkeypatch, tenant):
    import graph_client
    import signature_engine

    async def _user(e):
        return graph_client.UserData(mail=e)
    monkeypatch.setattr(graph_client, "get_user", _user)
    monkeypatch.setattr(signature_engine, "render",
                        lambda u, t, extra=None: (f"<p>{t} {u.mail} "
                                                  f"{(extra or {}).get('bearbeiter', u).mail}</p>", ""))
    umgebung["MAILBOX_CONFIG"] = {
        "erika@zarenko.net": {"sig": True, "template": "Sig", "intern_template": "Intern",
                              "use_policy": False},
        "team@firma.de": {"sig": True, "template": "Team", "use_policy": False},
    }
    return umgebung


def _abruf(**q):
    from webui.routen import addin_verfassen as av
    r = asyncio.run(av.verfassen_signatur(_ReqQ("Bearer " + _token(), **q)))
    return json.loads(r.body)


def test_api_intern_bei_internen_empfaengern(api):
    d = _abruf(empfaenger="max@firma.de")
    assert d["art"] == "intern" and d["vorlage"] == "Intern"


def test_api_standard_sobald_ein_externer(api):
    d = _abruf(empfaenger="max@firma.de,kunde@extern.de")
    assert d["art"] == "standard" and d["vorlage"] == "Sig"


def test_api_geteiltes_postfach(api):
    d = _abruf(von="team@firma.de", empfaenger="kunde@extern.de")
    assert d["vorlage"] == "Team"
    assert "team@firma.de erika@zarenko.net" in d["html"]      # Inhaber + Bearbeiter


def test_api_nicht_betreutes_von_bleibt_bei_der_person(api):
    d = _abruf(von="fremd@firma.de", empfaenger="kunde@extern.de")
    assert d["vorlage"] == "Sig"
