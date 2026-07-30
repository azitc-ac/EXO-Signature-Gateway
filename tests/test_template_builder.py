"""Baukasten → Jinja2-Vorlage: Feldnamen dürfen nur geprüft eingesetzt werden.

Hintergrund: `template_builder` baut aus den Vorlagen-Metadaten einen
Jinja2-Quelltext zusammen, indem es Feldnamen als Text einsetzt. Der
Telefon-Block tat das ohne Prüfung — ein Feldname wie `phone or 7*191` wurde
so zu einem ausgewerteten Ausdruck. Die Metadaten darf auch die Editor-Rolle
speichern (`POST /api/templates/{name}/meta` hängt an `_check_auth`, nicht an
`_require_admin`).

Zwei Ebenen sichern das ab, und beide werden hier geprüft:
  * `_resolve_var()` lässt nur bekannte Feldnamen und `custom.NAME` durch —
    baut man es zurück, schlagen `test_*_injektion_*` fehl;
  * die Render-Umgebung ist eine Sandbox — nötig, weil Freitext, Größen-,
    Farb- und URL-Angaben weiterhin roh in den Quelltext gelangen und dort
    ausgewertet werden. Dafür `test_render_umgebung_ist_sandbox`.
"""
import re

import jinja2
import pytest

import template_builder as tb


def _meta(*blocks):
    return {"version": 1, "global": dict(tb.DEFAULT_GLOBAL), "blocks": list(blocks)}


class _User:
    """Minimaler Render-Kontext — nur was die geprüften Blöcke brauchen."""
    displayName = "Erika Mustermann"
    mail = "erika@example.org"
    phone = ""
    mobilePhone = ""
    jobTitle = ""
    companyName = "Beispiel GmbH"


def _render(meta, custom=None):
    """Vorlage bauen UND rendern — erst beim Rendern zeigt sich eine Injektion."""
    src = tb.render_html(meta)
    out = jinja2.Environment().from_string(src).render(user=_User(), custom=custom or {})
    return re.sub(r"<[^>]+>", "", out)


# ── Einsetzung ohne Prüfung ───────────────────────────────────────────────────

@pytest.mark.parametrize("payload", [
    "phone or 7*191",                                    # Ausdruck statt Feldname
    "mail.__class__.__mro__[1].__subclasses__()|length",  # Einstieg Gadget-Kette
    "phone %}{{ 7*191 }}{% if true",                     # Ausbruch aus dem Block
])
def test_telefon_injektion_wird_nicht_ausgewertet(payload):
    """Der Telefon-Block war die Lücke: Feldname ungeprüft in die Vorlage."""
    out = _render(_meta({"id": "1", "type": "phone", "field": payload, "label": "Tel:"}))
    assert "1337" not in out          # 7*191
    assert not re.search(r"\b\d{3,}\b", out)   # auch keine Subclass-Anzahl
    assert out.strip() == ""          # Block entfällt vollständig

@pytest.mark.parametrize("payload", [
    "jobTitle or 7*191",
    "custom.a or 7*191",   # als eigene Variable getarnt (Punkt/Leerzeichen)
])
def test_feld_injektion_wird_nicht_ausgewertet(payload):
    out = _render(_meta({"id": "1", "type": "field", "field": payload}))
    assert "1337" not in out
    assert out.strip() == ""


def test_injektion_auch_im_textteil_nicht(payload="phone or 7*191"):
    """Der TXT-Pfad setzt Feldnamen genauso ein wie der HTML-Pfad."""
    src = tb.render_txt(_meta({"id": "1", "type": "phone", "field": payload}))
    out = jinja2.Environment().from_string(src).render(user=_User(), custom={})
    assert "1337" not in out


# ── Eigene Variablen (custom.NAME) ────────────────────────────────────────────

def test_eigene_variable_wird_gerendert():
    out = _render(
        _meta({"id": "1", "type": "field", "field": "custom.abteilung"}),
        custom={"abteilung": "Buchhaltung"},
    )
    assert "Buchhaltung" in out


def test_eigene_variable_leer_entfaellt():
    """Wie bei user.*-Feldern: leer → die Zeile erscheint nicht."""
    out = _render(
        _meta({"id": "1", "type": "field", "field": "custom.abteilung"}),
        custom={"abteilung": ""},
    )
    assert out.strip() == ""


def test_eigene_variable_im_textteil():
    src = tb.render_txt(_meta({"id": "1", "type": "field", "field": "custom.abteilung"}))
    out = jinja2.Environment().from_string(src).render(user=_User(), custom={"abteilung": "Buchhaltung"})
    assert "Buchhaltung" in out


@pytest.mark.parametrize("name,erwartet", [
    ("jobTitle",          "user.jobTitle"),
    ("custom.abteilung",  "custom.abteilung"),
    ("custom.a_1",        "custom.a_1"),
    ("erfunden",          None),      # nicht in der Whitelist
    ("custom.mit punkt",  None),      # Leerzeichen
    ("custom.a-b",        None),      # Bindestrich
    ("custom.",           None),      # leerer Name
    ("",                  None),
])
def test_resolve_var(name, erwartet):
    assert tb._resolve_var(name) == erwartet


# ── Regression: die Standardfelder funktionieren weiter ───────────────────────

def test_pflichtfelder_ohne_bedingung():
    """displayName/mail sind immer vorhanden — kein {% if %} darum."""
    src = tb.render_html(_meta({"id": "1", "type": "name_field", "field": "displayName"}))
    assert "{% if" not in src
    assert "{{ user.displayName }}" in src


def test_zweispalter_rendert_unterbloecke():
    out = _render(_meta({
        "id": "1", "type": "two_col", "left": [], "right": [
            {"id": "2", "type": "field", "field": "companyName"},
            {"id": "3", "type": "field", "field": "custom.abteilung"},
        ],
    }), custom={"abteilung": "Buchhaltung"})
    assert "Beispiel GmbH" in out and "Buchhaltung" in out


# ── Render-Umgebung ───────────────────────────────────────────────────────────

def test_render_umgebung_ist_sandbox():
    """Signaturvorlagen dürfen nicht an Python-Interna kommen.

    Der Feldname ist seit _resolve_var() geprüft, aber Freitext, Größen-,
    Farb- und URL-Angaben landen weiterhin roh im erzeugten Jinja2-Quelltext
    und werden beim Rendern ausgewertet. Die Schranke ist deshalb die
    Umgebung, nicht die einzelne Einsetzung. Baut man sie auf eine
    gewöhnliche `Environment` zurück, schlägt dieser Test fehl.
    """
    import signature_engine
    from jinja2.sandbox import SecurityError

    env = signature_engine._reload_env()
    tpl = env.from_string("{{ user.mail.__class__.__mro__[1].__subclasses__()|length }}")
    with pytest.raises(SecurityError):
        tpl.render(user=_User(), custom={})


def test_sandbox_laesst_normale_vorlagen_unveraendert():
    import signature_engine

    env = signature_engine._reload_env()
    out = env.from_string("{{ user.displayName }} / {{ custom.abteilung }}").render(
        user=_User(), custom={"abteilung": "Buchhaltung"}
    )
    assert out == "Erika Mustermann / Buchhaltung"
