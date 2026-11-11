"""Bausteine: Unterstreichung, Logo mit Link, alphabetische Postfachliste.

1. Jeder Baustein mit Schriftschnitt-Feldern kann unterstrichen werden. Bei
   Link-Bausteinen sitzt die Unterstreichung am <a>, nicht an der Zelle — sonst
   wäre der Präfix („Tel:") mit unterstrichen.
2. Das Logo kann verlinkt werden; nur erlaubte Ziele (wie im Textbaustein).
   Der Import lieferte `link` schon — der Renderer warf ihn weg.
3. `/api/mailboxes/namen` liefert alphabetisch, Umlaute wie ihr Grundbuchstabe.
"""
import re

import jinja2
import pytest

import template_builder as tb
import template_parser as tp


class _User:
    displayName = "Erika Mustermann"
    mail = "erika@example.org"
    phone = "+49 30 123"
    mobilePhone = "+49 170 123"
    jobTitle = "Leitung"
    companyName = "Beispiel GmbH"
    website = "https://example.org"
    bookingsUrl = "https://example.org/termin"
    streetAddress = "Hauptstr. 1"
    postalCode = "10115"
    city = "Berlin"
    country = "DE"


def _html(*blocks):
    meta = {"version": 1, "global": dict(tb.DEFAULT_GLOBAL), "blocks": list(blocks)}
    return jinja2.Environment().from_string(tb.render_html(meta)).render(user=_User(), custom={})


TEXT_BAUSTEINE = [
    {"type": "greeting", "text": "Grüße"},
    {"type": "name_field"},
    {"type": "field", "field": "jobTitle"},
    {"type": "address"},
]
LINK_BAUSTEINE = [
    {"type": "phone", "prefix": "Tel:"},
    {"type": "mobile", "prefix": "Mobil:"},
    {"type": "email_link", "prefix": "Mail:"},
    {"type": "web_link", "prefix": "Web:"},
    {"type": "booking_link", "prefix": "Termin:"},
    {"type": "social", "platform": "LinkedIn", "url": "https://linkedin.com/x", "prefix": "In:"},
]


@pytest.mark.parametrize("b", TEXT_BAUSTEINE + LINK_BAUSTEINE, ids=lambda b: b["type"])
def test_unterstreichung_wirkt_und_fehlt_ohne_haken(b):
    assert "text-decoration:underline" not in _html(dict(b))
    assert "text-decoration:underline" in _html(dict(b, underline=True))


@pytest.mark.parametrize("b", LINK_BAUSTEINE, ids=lambda b: b["type"])
def test_link_unterstrichen_am_anker_nicht_am_praefix(b):
    out = _html(dict(b, underline=True))
    td = re.search(r'<td style="([^"]*)">', out).group(1)
    assert "underline" not in td, "der Präfix gehört nicht unterstrichen"
    assert re.search(r'<a [^>]*text-decoration:underline', out)


def _logo(**kw):
    return _html({"type": "logo", "url": "https://example.org/logo.png", "width": 120, **kw})


@pytest.mark.parametrize("ziel", ["https://example.org", "mailto:info@example.org",
                                  "{{ user.website }}"])
def test_logo_mit_link(ziel):
    out = _logo(link=ziel)
    erwartet = ziel if "{{" not in ziel else _User.website
    assert re.search(rf'<a href="{re.escape(erwartet)}"[^>]*><img ', out), out


@pytest.mark.parametrize("ziel", ["", "javascript:alert(1)", "data:text/html,x",
                                  "  ", 'https://x" onmouseover="y'])
def test_logo_ohne_oder_mit_unbrauchbarem_link(ziel):
    out = _logo(link=ziel)
    assert "<img " in out, "das Bild bleibt"
    assert "onmouseover=\"" not in out
    if not ziel.startswith("https"):
        assert "<a " not in out


def test_verlinktes_logo_ueberlebt_import_und_rendern():
    quelle = ('<table><tr><td><a href="https://firma.example/"><img src="https://firma.example/l.png" '
              'width="90" alt="Firma"></a></td></tr></table>')
    meta = tp.parse_html(quelle)
    logos = [b for b in meta["blocks"] if b.get("type") == "logo"]
    assert logos and logos[0].get("link") == "https://firma.example/"
    assert '<a href="https://firma.example/"' in tb.render_html(meta)


def test_import_uebernimmt_unterstreichung():
    meta = tp.parse_html('<table><tr><td style="text-decoration: underline">Nur Text</td></tr></table>')
    assert any(b.get("underline") for b in meta["blocks"]), meta["blocks"]


# ── Postfachliste der Vorschau ───────────────────────────────────────────────

def test_postfachnamen_alphabetisch(monkeypatch):
    from fastapi.testclient import TestClient
    import exo_mailboxes
    import webui.app as wa
    roh = [{"primary": "z@x.de", "display_name": "Zander"},
           {"primary": "o@x.de", "display_name": "Özdemir"},
           {"primary": "a@x.de", "display_name": "anna"},
           {"primary": "b@x.de", "display_name": ""},
           {"primary": "", "display_name": "ohne Adresse"}]
    monkeypatch.setattr(exo_mailboxes, "list_mailboxes", lambda *a, **k: roh)
    wa.app.dependency_overrides[wa._check_auth] = lambda: "test"
    try:
        namen = [m["name"] for m in TestClient(wa.app).get("/api/mailboxes/namen").json()["mailboxes"]]
    finally:
        wa.app.dependency_overrides.pop(wa._check_auth, None)
    assert namen == ["anna", "b@x.de", "Özdemir", "Zander"]
