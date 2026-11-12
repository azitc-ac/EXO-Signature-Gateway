"""Die Benutzerfelder (`user.…`) stehen an mehreren Stellen — sie müssen übereinstimmen.

ANLASS (11.11.2026): Vor- und Nachname (`givenName`, `surname`) sollten
Standardvariablen werden. Die Feldliste stand an fünf Stellen (Renderer,
Import, Datenklasse, Editor, Postfach-Überschreibungen); ein Feld, das an einer
davon fehlt, ist im Editor wählbar und rendert leer — oder umgekehrt.
"""
import asyncio
import dataclasses
import re
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import graph_client      # noqa: E402
import template_builder  # noqa: E402
import template_parser   # noqa: E402

VORLAGEN = WURZEL / "app" / "webui" / "templates"
FELDER = set(template_builder._USER_FIELDS)


def test_vor_und_nachname_sind_standardfelder():
    assert {"givenName", "surname"} <= FELDER


def test_import_kennt_dieselben_felder():
    assert set(template_parser.BEKANNTE_FELDER) == FELDER


def test_datenklasse_hat_jedes_feld():
    felder = {f.name for f in dataclasses.fields(graph_client.UserData)} - {"custom", "entra_gefunden"}
    assert felder == FELDER


def test_editor_bietet_dieselben_felder_an():
    quelle = (VORLAGEN / "template_editor.html").read_text(encoding="utf-8")
    m = re.search(r"const USER_FIELDS = \[(.*?)\];", quelle, re.S)
    assert m, "USER_FIELDS fehlt im Editor"
    assert set(re.findall(r"'(\w+)'", m.group(1))) == FELDER


def test_ueberschreibungen_nur_bekannte_felder_und_die_neuen():
    quelle = (VORLAGEN / "settings_signature.html").read_text(encoding="utf-8")
    m = re.search(r"const _orVarOptions = \[(.*?)\];", quelle, re.S)
    assert m
    angeboten = set(re.findall(r'key:"user\.(\w+)"', m.group(1)))
    assert angeboten <= FELDER, angeboten - FELDER
    assert {"givenName", "surname"} <= angeboten


def test_get_user_fuellt_vor_und_nachname(monkeypatch):
    graph_client._user_data_cache.clear()

    async def daten(email):
        return {"displayName": "Max B", "givenName": "Max", "surname": "Beispielmann",
                "mail": email, "businessPhones": []}
    monkeypatch.setattr(graph_client, "_graph_user_data", daten)
    store = {"USER_OVERRIDES": {"b@firma.at": {"user.surname": "Anders"}}}
    monkeypatch.setattr(graph_client.settings_store, "get", lambda k, d=None: store.get(k, d))
    a = asyncio.run(graph_client.get_user("a@firma.at"))
    assert (a.givenName, a.surname) == ("Max", "Beispielmann")
    b = asyncio.run(graph_client.get_user("b@firma.at"))
    assert b.surname == "Anders", "Postfach-Überschreibung gilt auch hier"


def test_vorlage_rendert_vor_und_nachname():
    import jinja2
    meta = {"version": 1, "global": dict(template_builder.DEFAULT_GLOBAL),
            "blocks": [{"type": "field", "field": "givenName"},
                       {"type": "field", "field": "surname"}]}
    html = jinja2.Environment().from_string(template_builder.render_html(meta)).render(
        user=graph_client.UserData(givenName="Max", surname="Beispielmann"), custom={})
    assert "Max" in html and "Beispielmann" in html
