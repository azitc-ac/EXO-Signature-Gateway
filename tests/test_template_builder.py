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


# ── Kasten (box) ──────────────────────────────────────────────────────────────

def _box(**kw):
    b = {"id": "1", "type": "box", "children": [
        {"id": "2", "type": "field", "field": "companyName"}]}
    b.update(kw)
    return _meta(b)


def test_kasten_rahmt_den_inhalt():
    src = tb.render_html(_box(border_width=2, border_color="#ff0000", padding=20))
    assert "border:2px solid #ff0000" in src
    assert "padding:20px" in src
    assert "{{ user.companyName }}" in src


def test_kasten_ohne_inhalt_entfaellt():
    """Ein leerer Kasten wäre ein Rahmen um nichts."""
    assert tb.render_html(_meta({"id": "1", "type": "box", "children": []})).count("<tr>") == 0


def test_kasten_fuellung_nur_wenn_gewuenscht():
    assert "background-color:#eeeeee" in tb.render_html(_box(filled=True, fill_color="#eeeeee"))
    assert "background-color" not in tb.render_html(_box(filled=False, fill_color="#eeeeee"))


def test_runde_ecken_mit_breite_erzeugen_vml():
    """Outlook braucht die VML-Form — sie setzt eine feste Breite voraus."""
    src = tb.render_html(_box(radius=8, width=520))
    assert "v:roundrect" in src and 'arcsize="' in src
    assert "border-radius:8px" in src          # für alle übrigen Programme
    assert "[if mso]" in src and "[if !mso]" in src


def test_runde_ecken_ohne_breite_ohne_vml():
    """Ohne feste Breite kann VML nicht zeichnen — dann lieber eckig als falsch."""
    src = tb.render_html(_box(radius=8, width=0))
    assert "v:roundrect" not in src
    assert "border-radius:8px" in src


def test_eckiger_kasten_ohne_vml():
    assert "v:roundrect" not in tb.render_html(_box(radius=0, width=520))


def test_inhalt_steht_nur_einmal_im_quelltext():
    """Beide Varianten vollständig auszugeben würde Base64-Logos verdoppeln."""
    src = tb.render_html(_box(radius=8, width=520))
    assert src.count("{{ user.companyName }}") == 1


def test_kasten_traegt_zweispalter():
    out = _render(_box(radius=8, width=520, children=[{
        "id": "2", "type": "two_col", "left": [], "right": [
            {"id": "3", "type": "field", "field": "companyName"}]}]))
    assert "Beispiel GmbH" in out


def test_kasten_farbe_wird_geprueft():
    """Farbwerte landen roh im Quelltext — Unsinn fällt auf die Vorgabe zurück."""
    src = tb.render_html(_box(border_width=1, border_color="rot; evil"))
    assert "evil" not in src
    assert "border:1px solid #e2e8f0" in src


def test_kasten_im_textteil_zeigt_die_kinder():
    txt = tb.render_txt(_box(radius=8, width=520))
    assert "{{ user.companyName }}" in txt
    assert "mso" not in txt


@pytest.mark.parametrize("block,unsinn", [
    ({"type": "divider"}, "color"),
    ({"type": "greeting", "text": "Hi"}, "color"),
    ({"type": "field", "field": "companyName"}, "color"),
    ({"type": "two_col", "divider": True, "left": [], "right": [
        {"id": "9", "type": "field", "field": "companyName"}]}, "divider_color"),
])
def test_farben_werden_ueberall_geprueft(block, unsinn):
    """Kein Farbweg darf einen ungeprüften Wert in die Vorlage schreiben."""
    b = {"id": "1", **block, unsinn: "javascript:evil"}
    assert "evil" not in tb.render_html(_meta(b))


# ── Anschrift ─────────────────────────────────────────────────────────────────

class _Anschrift:
    displayName = "X"; mail = "x@y.z"
    def __init__(self, strasse="Musterstr. 1", plz="12345", ort="Musterstadt", land="Deutschland"):
        self.streetAddress, self.postalCode, self.city, self.country = strasse, plz, ort, land


def _adr(user, **kw):
    src = tb.render_html(_meta({"id": "1", "type": "address", **kw}))
    out = jinja2.Environment().from_string(src).render(user=user, custom={})
    return " / ".join(t.strip() for t in re.findall(r"<td[^>]*>(.*?)</td>", out) if t.strip())


@pytest.mark.parametrize("user,erwartet", [
    (_Anschrift(),                              "Musterstr. 1 / 12345 Musterstadt"),
    (_Anschrift(strasse=""),                    "12345 Musterstadt"),
    (_Anschrift(plz=""),                        "Musterstr. 1 / Musterstadt"),
    (_Anschrift(ort=""),                        "Musterstr. 1 / 12345"),
    (_Anschrift(strasse="", plz=""),            "Musterstadt"),
    (_Anschrift(strasse="", plz="", ort=""),    ""),
])
def test_anschrift_zweizeilig_ohne_lose_trennzeichen(user, erwartet):
    """Fehlende Teile dürfen kein Komma und keine Lücke hinterlassen."""
    assert _adr(user) == erwartet


@pytest.mark.parametrize("user,erwartet", [
    (_Anschrift(),                           "Musterstr. 1, 12345 Musterstadt"),
    (_Anschrift(strasse=""),                 "12345 Musterstadt"),
    (_Anschrift(plz=""),                     "Musterstr. 1, Musterstadt"),
    (_Anschrift(strasse="", plz="", ort=""), ""),
])
def test_anschrift_einzeilig_ohne_lose_trennzeichen(user, erwartet):
    assert _adr(user, one_line=True) == erwartet


def test_anschrift_land_nur_auf_wunsch():
    assert "Deutschland" not in _adr(_Anschrift())
    assert "Deutschland" in _adr(_Anschrift(), show_country=True)


def test_anschrift_html_und_text_bleiben_gleich():
    """Beide Wege nutzen dieselbe Zusammensetzung — sonst driften sie."""
    meta = _meta({"id": "1", "type": "address", "one_line": True})
    u = _Anschrift(plz="")
    html = jinja2.Environment().from_string(tb.render_html(meta)).render(user=u, custom={})
    txt = jinja2.Environment().from_string(tb.render_txt(meta)).render(user=u, custom={})
    assert re.sub(r"<[^>]+>", "", html).strip() == txt.strip()


def test_adressfelder_sind_echte_felder():
    for f in ("streetAddress", "postalCode", "city", "state", "country"):
        assert tb._resolve_var(f) == f"user.{f}"


# ── Link-Bausteine: Formatierung, Feldwahl, Anzeigetext ──────────────────────

def test_links_lassen_sich_formatieren():
    """Vorher konnten nur Feld-Blöcke fett/kursiv/gefärbt werden."""
    for blk in ({"type": "phone", "field": "phone"},
                {"type": "email_link"},
                {"type": "web_link"},
                {"type": "booking_link"}):
        src = tb.render_html(_meta({"id": "1", **blk, "bold": True, "italic": True,
                                    "size": "9pt", "color": "#ff0000"}))
        assert "font-weight:bold" in src, blk
        assert "font-style:italic" in src, blk
        assert "font-size:9pt" in src, blk
        assert "color:#ff0000" in src, blk      # am <a>, nicht die Link-Farbe


def test_email_und_web_link_mit_feldwahl():
    """Beide waren fest auf user.mail bzw. user.website verdrahtet."""
    assert "mailto:{{ user.custom_ok }}" not in tb.render_html(
        _meta({"id": "1", "type": "email_link", "field": "erfunden"}))   # unbekannt → entfällt
    src = tb.render_html(_meta({"id": "1", "type": "web_link", "field": "bookingsUrl"}))
    assert "{{ user.bookingsUrl }}" in src


def test_web_link_mit_anzeigetext():
    """Als einziger Link-Baustein konnte er den Text nicht ersetzen."""
    src = tb.render_html(_meta({"id": "1", "type": "web_link", "label": "Unsere Seite"}))
    assert ">Unsere Seite<" in src
    assert 'href="{{ user.website }}"' in src


def test_telefon_ohne_beschriftung_kein_leerzeichen():
    src = tb.render_html(_meta({"id": "1", "type": "phone", "field": "phone", "label": ""}))
    assert '">Tel:' not in src
