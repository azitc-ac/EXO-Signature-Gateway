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


# ── Längenangaben ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("eingabe,erwartet", [
    ("20",    "20pt"),     # nackte Zahl: DAS war der Fehler — CSS verwarf sie
    ("20pt",  "20pt"),
    ("20px",  "20px"),
    ("1.5em", "1.5em"),
    ("120%",  "120%"),
    ("20 pt", "20pt"),
    ("gross", None),       # keine Länge → gar nichts, statt unwirksames CSS
    ("",      None),
    (None,    None),
])
def test_laenge(eingabe, erwartet):
    assert tb._laenge(eingabe) == erwartet


def test_groesse_ohne_einheit_wirkt():
    """`font-size:20` ist ungültiges CSS und wurde stillschweigend verworfen —
    der Text blieb auf der Grundgröße, obwohl im Quelltext etwas stand."""
    src = tb.render_html(_meta({"id": "1", "type": "field",
                                "field": "companyName", "size": "20"}))
    assert "font-size:20pt" in src
    assert "font-size:20;" not in src and 'font-size:20"' not in src


def test_groesse_unsinn_erzeugt_kein_totes_css():
    src = tb.render_html(_meta({"id": "1", "type": "field",
                                "field": "companyName", "size": "riesig"}))
    # Die aeussere Tabelle traegt immer die globale Groesse — geprueft wird
    # die Zelle des Blocks selbst.
    zelle = [z for z in src.splitlines() if "companyName" in z][0]
    assert "font-size" not in zelle


@pytest.mark.parametrize("typ", ["greeting", "field", "phone", "email_link",
                                 "web_link", "booking_link", "address"])
def test_groesse_ueberall_mit_einheit(typ):
    """Alle Bausteine mit Größenangabe gehen durch dieselbe Prüfung."""
    blk = {"id": "1", "type": typ, "size": "14"}
    if typ in ("field", "phone"):
        blk["field"] = "companyName" if typ == "field" else "phone"
    src = tb.render_html(_meta(blk))
    assert "font-size:14pt" in src, typ


def test_spaltenbreite_ohne_einheit():
    """Dieselbe Falle in der Zweispalter-Breite, dort in px."""
    src = tb.render_html(_meta({"id": "1", "type": "two_col", "left_width": "120",
        "left": [{"id": "2", "type": "field", "field": "companyName"}], "right": []}))
    assert "width:120px" in src


def test_globale_schriftgroesse_ohne_einheit():
    meta = _meta({"id": "1", "type": "name_field", "field": "displayName"})
    meta["global"]["font_size"] = "13"
    assert "font-size:13pt" in tb.render_html(meta)


def test_schriftart_kann_das_attribut_nicht_verlassen():
    """Ein Anfuehrungszeichen beendete das style-Attribut, ein Semikolon haenge
    weitere Eigenschaften an. Der Text darf als Schriftname stehenbleiben —
    nur eben nicht als eigene CSS-Eigenschaft."""
    meta = _meta({"id": "1", "type": "name_field", "field": "displayName"})
    meta["global"]["font_family"] = 'Arial";color:red;x="'
    src = tb.render_html(meta)
    wert = src.split("font-family:")[1].split(";")[0]
    assert '"' not in wert
    assert ";color:red" not in src


# ── Präfix und Anzeigetext ────────────────────────────────────────────────────

def _beide(block, **user_kw):
    class U:
        displayName = "X"; mail = "e@x.de"; website = "https://beispiel.de"
        phone = "+49 30 1"; mobilePhone = "+49 170 2"; bookingsUrl = "https://buchen.de"
        companyName = "C"
    for k, v in user_kw.items():
        setattr(U, k, v)
    meta = _meta(dict(block, id="1"))
    html = jinja2.Environment().from_string(tb.render_html(meta)).render(user=U(), custom={})
    txt = jinja2.Environment().from_string(tb.render_txt(meta)).render(user=U(), custom={})
    return re.sub(r"<[^>]+>", "", html).strip(), txt.strip()


def test_praefix_steht_davor_anzeigetext_ersetzt():
    """Die zwei Begriffe müssen sich unterscheidbar verhalten."""
    h, _ = _beide({"type": "web_link", "prefix": "Web:"})
    assert h == "Web: https://beispiel.de"
    h, _ = _beide({"type": "web_link", "label": "beispiel.de"})
    assert h == "beispiel.de"
    h, _ = _beide({"type": "web_link", "prefix": "Web:", "label": "beispiel.de"})
    assert h == "Web: beispiel.de"


@pytest.mark.parametrize("typ,feld", [
    ("phone", "phone"), ("email_link", "mail"), ("web_link", "website"),
])
def test_praefix_bei_allen_linkbausteinen(typ, feld):
    """Der Website-Link konnte als einziger keinen Vorsatz tragen."""
    h, _ = _beide({"type": typ, "prefix": "Vorsatz:"})
    assert h.startswith("Vorsatz: ")


def test_textteil_zeigt_die_adresse_trotz_anzeigetext():
    """Im Text gibt es keinen Verweis — ein ersetzter Wert wäre verloren."""
    _, t = _beide({"type": "email_link", "label": "Schreib mir"})
    assert "e@x.de" in t and "Schreib mir" in t


def test_textteil_folgt_der_feldwahl():
    """Der Textteil setzte user.mail bzw. user.website fest ein und
    überging die Feldwahl aus dem HTML-Pfad."""
    _, t = _beide({"type": "email_link", "field": "website"})
    assert "https://beispiel.de" in t and "e@x.de" not in t
    _, t = _beide({"type": "web_link", "field": "mail"})
    assert "e@x.de" in t


def test_textteil_beachtet_den_praefix_beim_website_link():
    """Der Website-Link überging im Text Präfix UND Anzeigetext."""
    _, t = _beide({"type": "web_link", "prefix": "Web:"})
    assert t == "Web: https://beispiel.de"


# ── Altbestand: `label` war beim Telefon der Präfix ───────────────────────────

def test_altes_telefon_label_bleibt_praefix():
    """Vorlagen vor v1.7.116 dürfen ihre Beschriftung nicht verlieren."""
    h, t = _beide({"type": "phone", "field": "phone", "label": "Tel:"})
    assert h == "Tel: +49 30 1"
    assert t == "Tel: +49 30 1"


def test_altes_telefon_ohne_label_behaelt_vorgabe():
    h, _ = _beide({"type": "phone", "field": "phone"})
    assert h == "Tel: +49 30 1"
    h, _ = _beide({"type": "mobile", "field": "mobilePhone"})
    assert h == "Mobil: +49 170 2"


def test_neues_telefon_kennt_beide_felder():
    h, t = _beide({"type": "phone", "field": "phone", "prefix": "Tel:", "label": "anrufen"})
    assert h == "Tel: anrufen"
    assert t == "Tel: +49 30 1"          # im Text bleibt die Nummer


def test_telefon_praefix_ausdruecklich_leer():
    """Mit der neuen Form muss sich der Vorsatz auch entfernen lassen."""
    h, _ = _beide({"type": "phone", "field": "phone", "prefix": ""})
    assert h == "+49 30 1"


def test_social_kennt_praefix_und_formatierung():
    """Der Social-Baustein las `label` direkt und übersprang damit Präfix,
    Farbe und Schriftschnitt, die alle anderen Link-Bausteine haben."""
    src = tb.render_html(_meta({"id": "1", "type": "social", "platform": "LinkedIn",
                                "url": "https://li.example", "prefix": "Folgen:",
                                "bold": True, "color": "#ff0000"}))
    assert "Folgen:" in src and "font-weight:bold" in src and "color:#ff0000" in src
    txt = tb.render_txt(_meta({"id": "1", "type": "social", "platform": "LinkedIn",
                               "url": "https://li.example", "prefix": "Folgen:"}))
    assert txt.strip() == "Folgen: https://li.example"


# ── Leerzeilen am Rand eines Freitext-Bausteins ──────────────────────────────
#
# ANLASS (07.08.2026): Vor der ersten Zeile eines Kastens stand ein <br>, das
# niemand getippt hatte — im Eingabefeld ist eine leere erste Zeile praktisch
# unsichtbar. Die Textfassung desselben Bausteins verwarf leere Zeilen laengst;
# die beiden Ausgaben widersprachen sich also.

def test_leerzeile_am_anfang_erzeugt_kein_br():
    src = tb.render_html(_meta({"id": "1", "type": "text",
                                "text": "\nErste Zeile\nZweite Zeile"}))
    assert "<br>Erste Zeile" not in src, "fuehrendes <br> steht wieder da"
    assert "Erste Zeile<br>Zweite Zeile" in src, "der echte Umbruch fehlt"


def test_leerzeile_am_ende_erzeugt_kein_br():
    src = tb.render_html(_meta({"id": "1", "type": "text", "text": "Zeile\n\n"}))
    assert "Zeile<br>" not in src


def test_leerzeile_MITTENDRIN_bleibt_erhalten():
    """Die Gegenprobe: dort ist die Leerzeile gewollt und darf nicht
    wegoptimiert werden."""
    src = tb.render_html(_meta({"id": "1", "type": "text",
                                "text": "Oben\n\nUnten"}))
    assert "Oben<br><br>Unten" in src
