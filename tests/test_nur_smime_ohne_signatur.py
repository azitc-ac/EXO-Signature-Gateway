"""Postfach nur für S/MIME (Haken „Signatur“ aus): NIRGENDS eine Signatur.

Entscheidung 19.11.2026: keine Signatur, kein Banner, kein Disclaimer — im
Mailweg (policies.entscheiden, tests/test_richtlinienergebnis.py), im
Verfassen-Add-in, im Knopf-Add-in und unter der Abwesenheitsnotiz. Bis
v1.12.103 bekam ein solches Postfach überall die volle Signatur.

Diese Tests MÜSSEN fehlschlagen, wenn einer der Wege policies.ohne_signatur
nicht mehr fragt.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import graph_client
import policies
import settings_store
import signature_engine


NUR_SMIME = {"smime": True}
MIT_SIG = {"sig": True}


def test_regel():
    assert policies.ohne_signatur(NUR_SMIME)
    assert not policies.ohne_signatur(MIT_SIG)
    assert not policies.ohne_signatur({})          # nicht betreut ist ein anderer Fall
    assert not policies.ohne_signatur(None)


@pytest.fixture
def daten(monkeypatch):
    d = {"MAILBOX_CONFIG": {"a@firma.at": dict(NUR_SMIME), "b@firma.at": dict(MIT_SIG)},
         "TEMPLATE_POLICIES": {"sig": "default", "banner": "Ban", "disclaimer": "Dis"},
         "CUSTOM_POLICIES": [], "INTERNAL_GROUPS": {},
         "OOO_APPEND_SIGNATURE": True, "OOO_APPEND_BANNER": True, "OOO_APPEND_DISCLAIMER": True}
    monkeypatch.setattr(settings_store, "get", lambda k, dflt=None: d.get(k, dflt))
    monkeypatch.setattr(signature_engine, "render",
                        lambda u, template_name=None, extra=None, **k: (f"<p>{template_name}</p>", template_name or ""))
    monkeypatch.setattr(signature_engine, "list_templates", lambda: ["default", "Ban", "Dis"])
    return d


def test_abwesenheit_haengt_nichts_an(daten):
    import abwesenheit
    u = graph_client.UserData(mail="a@firma.at")
    assert abwesenheit.oof_anhang_teile(u, "a@firma.at", daten["MAILBOX_CONFIG"], NUR_SMIME) == {}
    assert abwesenheit.oof_anhang_teile(u, "a@firma.at", daten["MAILBOX_CONFIG"], None) == {}
    mit = abwesenheit.oof_anhang_teile(graph_client.UserData(mail="b@firma.at"), "b@firma.at",
                                       daten["MAILBOX_CONFIG"], MIT_SIG)
    assert set(mit) == {"sig", "banner", "disclaimer"}


def _knopf(monkeypatch, route, email):
    from webui.routen import addin
    monkeypatch.setattr(addin, "_addin_postfach", lambda req, e, user: e)
    monkeypatch.setattr(addin, "_lokal_gesehen", lambda *a, **k: None)

    async def _user(e):
        return graph_client.UserData(mail=e)
    monkeypatch.setattr(graph_client, "get_user", _user)
    r = asyncio.run(route(None, email=email, user="x"))
    return json.loads(r.body)


def test_knopf_add_in_fuegt_nichts_ein(daten, monkeypatch):
    from webui.routen import addin
    assert _knopf(monkeypatch, addin.api_addin_signature, "a@firma.at")["marked_html"] == ""
    assert _knopf(monkeypatch, addin.api_addin_templates, "a@firma.at")["templates"] == []
    assert _knopf(monkeypatch, addin.api_addin_signature, "b@firma.at")["marked_html"]
