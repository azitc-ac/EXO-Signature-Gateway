"""Eigene Variablen für Inhaber UND schreibende Person: custom_u / custom_b.

`user.*` und `bearbeiter.*` gab es schon; eigene Variablen (`custom.NAME`)
galten nur für den Postfachinhaber. Seit v1.12.106 trägt der Kontext
`custom_u` (Inhaber, = altes `custom`) und `custom_b` (wer schreibt — bei
„im Auftrag von“ die Person, sonst der Inhaber). Geprüft werden Kontext,
Baukasten, Import und der Weg, auf dem der Bearbeiter seine Werte bekommt.
"""
import asyncio
import re
from pathlib import Path

import pytest

import graph_client
import signature_engine
import template_builder as tb
import template_parser as tp

APP = Path(__file__).resolve().parents[1] / "app"

VORLAGE = "U={{ custom_u.abt }}|B={{ custom_b.abt }}|A={{ custom.abt }}|N={{ bearbeiter.displayName }}"


@pytest.fixture
def verz(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    signature_engine._reload_env()
    (tmp_path / "signature.html").write_text("<p>x</p>", encoding="utf-8")
    (tmp_path / "Zwei.html").write_text(VORLAGE, encoding="utf-8")
    yield tmp_path
    signature_engine._reload_env()


INHABER = graph_client.UserData(mail="info@firma.at", displayName="Info", custom={"abt": "Zentrale"})
PERSON = graph_client.UserData(mail="max@firma.at", displayName="Max Mustermann", custom={"abt": "Vertrieb"})


def test_inhaber_und_bearbeiter_getrennt(verz):
    html, _ = signature_engine.render(INHABER, "Zwei", {"bearbeiter": PERSON})
    assert html == "U=Zentrale|B=Vertrieb|A=Zentrale|N=Max Mustermann", html


def test_ohne_bearbeiter_ist_custom_b_der_inhaber(verz):
    html, _ = signature_engine.render(INHABER, "Zwei")
    assert html == "U=Zentrale|B=Zentrale|A=Zentrale|N=Info", html
    html, _ = signature_engine.render(INHABER, "Zwei", {"bearbeiter": None})
    assert html.startswith("U=Zentrale|B=Zentrale|"), html


def test_pruefung_kennt_die_neuen_namen(verz):
    """Postfach-Prüfung und Versand teilen den Kontext (`_kontext`) — eine
    Vorlage mit custom_b darf dort nicht als „undefined“ gelten."""
    assert signature_engine.render_fehler(INHABER, "Zwei") is None
    assert signature_engine.render_fehler(INHABER, "Zwei", {"bearbeiter": PERSON}) is None


# ── Baukasten und Import ─────────────────────────────────────────────────────

@pytest.mark.parametrize("feld", ["custom_u.abt", "custom_b.abt", "custom.abt"])
def test_baukasten_nimmt_alle_drei_schreibweisen(feld):
    assert tb._resolve_var(feld) == feld
    meta = {"version": 1, "global": dict(tb.DEFAULT_GLOBAL),
            "blocks": [{"id": "1", "type": "field", "field": feld}]}
    assert "{{ " + feld + " }}" in tb.render_html(meta)


@pytest.mark.parametrize("feld", ["custom_b.a b", "custom_b.a-b", "custom_b.", "custom_x.abt"])
def test_baukasten_prueft_den_namen(feld):
    assert tb._resolve_var(feld) is None


def _zellen(html):
    zellen = []

    def lauf(k):
        if k.tag == "td":
            zellen.append(k)
        for c in k.kinder:
            lauf(c)
    lauf(tp._baum(html))
    return zellen


@pytest.mark.parametrize("feld", ["custom.abt", "custom_u.abt", "custom_b.abt"])
def test_import_behaelt_den_vollen_pfad(feld):
    """Bis v1.12.105 las der Import `{{ custom.abt }}` als Feld „abt“ — das
    der Baukasten für ein unbekanntes Benutzerfeld hielt und verwarf."""
    b = tp._als_feld(_zellen(f"<table><tr><td>Abt: {{{{ {feld} }}}}</td></tr></table>")[0])
    assert b and b["field"] == feld, b
    assert tb._resolve_var(b["field"]) == feld


# ── Woher der Bearbeiter seine Werte hat ─────────────────────────────────────

def test_get_user_fuellt_custom_fuer_jede_adresse(monkeypatch):
    """Der Bearbeiter läuft durch dasselbe get_user wie der Inhaber: Entra-Feld,
    Gruppenwert und SEIN Override (nach seiner Adresse)."""
    graph_client._user_data_cache.clear()

    async def daten(email):
        return {"displayName": email, "mail": email, "department": "Abt " + email[:3],
                "businessPhones": []}
    monkeypatch.setattr(graph_client, "_graph_user_data", daten)
    store = {"CUSTOM_TEMPLATE_VARS": [{"name": "abt", "entra_field": "department"},
                                      {"name": "vertreter", "entra_field": ""}],
             "USER_OVERRIDES": {"max@firma.at": {"custom.vertreter": "Erika"}}}
    monkeypatch.setattr(graph_client.settings_store, "get", lambda k, d=None: store.get(k, d))
    import policies
    monkeypatch.setattr(policies, "group_vars_for",
                        lambda m: {"team": "Nord"} if m == "max@firma.at" else {})
    inh = asyncio.run(graph_client.get_user("info@firma.at"))
    per = asyncio.run(graph_client.get_user("max@firma.at"))
    assert inh.custom == {"abt": "Abt inf"}
    assert per.custom == {"abt": "Abt max", "team": "Nord", "vertreter": "Erika"}


_ERLAUBT = re.compile(r"(await\s+[\w.]*get_user\(|_bearbeiter_daten\b|nutzer\b)")


def test_jeder_bearbeiter_kommt_aus_get_user():
    """Jede Stelle, die `bearbeiter` in den Vorlagenkontext legt, holt die
    Person über get_user — sonst wäre custom_b dort leer, obwohl die Person
    Werte hat. `UserData(mail=…)` nur als Rückfall in einem except-Zweig."""
    funde = []
    for datei in APP.rglob("*.py"):
        zeilen = datei.read_text(encoding="utf-8").splitlines()
        for i, z in enumerate(zeilen):
            m = re.search(r"""\{\s*["']bearbeiter["']\s*:\s*(.+)""", z)
            if not m:
                continue
            wert = m.group(1)
            if _ERLAUBT.match(wert):
                funde.append((datei.name, i + 1, "get_user"))
            elif "UserData(mail=" in wert and any("except" in v for v in zeilen[max(0, i - 2):i]):
                funde.append((datei.name, i + 1, "rueckfall"))
            else:
                pytest.fail(f"{datei.name}:{i + 1}: bearbeiter ohne get_user — {z.strip()}")
    assert len([f for f in funde if f[2] == "get_user"]) >= 5, funde


def test_handler_holt_den_bearbeiter_ueber_get_user():
    quelle = (APP / "handler.py").read_text(encoding="utf-8")
    assert "_bearbeiter_daten = (await graph_client.get_user(_bearbeiter)) if _bearbeiter else user_data" in quelle
