"""Baukasten-Bedingung „nur wenn jemand für das Postfach schreibt“.

Für geteilte Postfächer: Die Signatur zeigt die schreibende Person
(`{{ bearbeiter.* }}`) und einen Baustein mit dem Postfachnamen nur dann, wenn
die Person bekannt ist. Bei „Senden als“ ohne Add-in ist `bearbeiter` der
Inhaber selbst — der Postfachname stünde sonst doppelt.
"""
import pytest

import graph_client
import signature_engine
import template_builder as tb

POSTFACH = graph_client.UserData(displayName="Team Rechnungen", mail="rechnungen@firma.at")
PERSON = graph_client.UserData(displayName="Max Mustermann", mail="max@firma.at")


def _meta(bedingung):
    return {"blocks": [
        {"id": "1", "type": "name_field", "field": "bearbeiter.displayName"},
        {"id": "2", "type": "field", "field": "displayName", "bedingung": bedingung},
    ]}


def _html(meta, extra):
    src = tb.render_html(meta)
    return signature_engine._get_env().from_string(src).render(
        **signature_engine._kontext(POSTFACH, extra))


def _txt(meta, extra):
    src = tb.render_txt(meta)
    return signature_engine._text_env().from_string(src).render(
        **signature_engine._kontext(POSTFACH, extra))


FAELLE = [
    # (extra, schreibt jemand für das Postfach?)
    (None, False),                                         # Inhaber selbst
    ({"bearbeiter": PERSON}, True),                        # Add-in / im Auftrag von
    ({"bearbeiter": POSTFACH}, False),                     # Senden als ohne Add-in
    ({"bearbeiter": graph_client.UserData(displayName="X", mail="RECHNUNGEN@firma.at")}, False),
]


@pytest.mark.parametrize("extra,fuer", FAELLE)
@pytest.mark.parametrize("render", [_html, _txt])
def test_nur_fuer_postfach(render, extra, fuer):
    out = render(_meta("fuer_postfach"), extra)
    # Block 1 zeigt den Inhabernamen, wenn bearbeiter der Inhaber ist; Block 2
    # steht genau dann drin, wenn jemand für das Postfach schreibt.
    erwartet = (1 if fuer else 0) + (1 if (extra or {}).get("bearbeiter", POSTFACH) is POSTFACH else 0)
    assert out.count("Team Rechnungen") == erwartet


@pytest.mark.parametrize("extra,fuer", FAELLE)
@pytest.mark.parametrize("render", [_html, _txt])
def test_nur_inhaber_selbst(render, extra, fuer):
    out = render(_meta("selbst"), extra)
    erwartet = (0 if fuer else 1) + (1 if (extra or {}).get("bearbeiter", POSTFACH) is POSTFACH else 0)
    assert out.count("Team Rechnungen") == erwartet


@pytest.mark.parametrize("render", [_html, _txt])
def test_ohne_bedingung_immer(render):
    for extra, _ in FAELLE:
        assert "Max Mustermann" in render(_meta(""), {"bearbeiter": PERSON})
        assert "Team Rechnungen" in render(_meta(""), extra)


@pytest.mark.parametrize("wert", ["fuer_postfach or 7*7 %}{{ 7*7 }}{% if 1", "7*7", "  "])
def test_unbekannter_wert_gelangt_nicht_in_die_vorlage(wert):
    src = tb.render_html(_meta(wert)) + tb.render_txt(_meta(wert))
    assert "7*7" not in src
    assert "{% if" not in src.replace("{% if bearbeiter.displayName %}", "").replace(
        "{% if user.displayName %}", "")


def test_bedingung_gilt_auch_im_zweispalter():
    meta = {"blocks": [{"id": "1", "type": "two_col", "left": [],
                        "right": [{"id": "2", "type": "field", "field": "displayName",
                                   "bedingung": "fuer_postfach"}]}]}
    assert "Team Rechnungen" not in _html(meta, None)
    assert "Team Rechnungen" in _html(meta, {"bearbeiter": PERSON})
