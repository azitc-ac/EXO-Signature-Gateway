"""HTML zurück in Blöcke lesen — und der Beweis, dass dabei nichts kippt.

DIE ZENTRALE PRÜFUNG IST DER RUNDLAUF
`render(parse(render(blocks)))` muss `render(blocks)` ergeben. Er prüft nicht,
ob die Blöcke gleich heissen, sondern ob das ERGEBNIS gleich aussieht — und
genau darauf kommt es an: Der Nutzer soll nach der Umwandlung dieselbe Signatur
sehen wie vorher.

Ein Vergleich der Blocklisten wäre schwächer und zugleich zerbrechlicher: Er
schlüge bei jedem zusätzlichen Vorgabewert an, ohne dass etwas kaputt ist.

FÜR FREMDES HTML GILT ETWAS ANDERES
Dort wird geraten, und die Regel lautet „im Zweifel Freitext". Geprüft wird
deshalb nicht die erkannte Struktur, sondern dass INHALT ERHALTEN bleibt.
"""
import re

import pytest

import template_builder as tb
import template_parser as tp


def _norm(html: str) -> str:
    """Leerraum vereinheitlichen — Einrückung ist keine Bedeutung."""
    return re.sub(r"\s+", " ", html).strip()


def _rundlauf(blocks: list[dict]) -> tuple[str, str]:
    meta = {"version": 1, "blocks": blocks}
    einmal = tb.render_html(meta)
    zurueck = tp.parse_html(einmal)
    zurueck.pop("_hinweise", None)
    return _norm(einmal), _norm(tb.render_html(zurueck))


FAELLE = [
    pytest.param([{"type": "greeting", "text": "Freundliche Grüße"}], id="grussformel"),
    pytest.param([{"type": "name_field", "field": "displayName", "bold": True}], id="name"),
    pytest.param([{"type": "field", "field": "jobTitle", "color": "muted"}], id="feld-gedaempft"),
    pytest.param([{"type": "field", "field": "companyName", "size": "20pt"}], id="feld-groesse"),
    pytest.param([{"type": "divider"}], id="trenner"),
    pytest.param([{"type": "spacer", "height": 10}], id="abstand"),
    pytest.param([{"type": "email_link"}], id="mail"),
    pytest.param([{"type": "phone", "prefix": "Tel:"}], id="telefon"),
    pytest.param([{"type": "mobile", "prefix": "Mobil:"}], id="mobil"),
    pytest.param([{"type": "logo", "url": "https://x/y.png", "width": 120}], id="logo"),
    pytest.param([{"type": "badge", "label": "RSS", "badge_color": "#f26522",
                   "html": "Neues im Blog"}], id="etikett"),
    pytest.param([{"type": "freetext", "html": "<em>Hallo</em> Welt"}], id="freitext"),
    pytest.param([{"type": "address"}], id="anschrift"),
    pytest.param([{"type": "address", "one_line": True, "show_country": True}],
                 id="anschrift-einzeilig"),
    pytest.param([{"type": "two_col", "divider": True, "gap": 12,
                   "left": [{"type": "logo", "url": "https://x/y.png", "width": 90}],
                   "right": [{"type": "name_field", "field": "displayName",
                              "bold": True}]}], id="zweispalter"),
    pytest.param([{"type": "box", "border_width": 1, "border_color": "#e2e8f0",
                   "padding": 12, "radius": 0, "width": 0,
                   "children": [{"type": "field", "field": "jobTitle"}]}], id="kasten"),
    pytest.param([{"type": "box", "border_side": "left", "border_width": 3,
                   "border_color": "#2563eb", "filled": True, "fill_color": "#eff6ff",
                   "padding": 6, "padding_x": 14, "radius": 0, "width": 0,
                   "children": [{"type": "freetext", "html": "Hinweis"}]}], id="hinweisband"),
    pytest.param([
        {"type": "greeting", "text": "Freundliche Grüße"},
        {"type": "name_field", "field": "displayName", "bold": True},
        {"type": "field", "field": "jobTitle", "color": "muted"},
        {"type": "divider"},
        {"type": "email_link"},
        {"type": "phone", "prefix": "Tel:"},
        {"type": "spacer", "height": 8},
        {"type": "logo", "url": "https://x/y.png", "width": 100},
    ], id="volle-signatur"),
]


@pytest.mark.parametrize("blocks", FAELLE)
def test_rundlauf_erhaelt_das_ergebnis(blocks):
    vorher, nachher = _rundlauf(blocks)
    assert vorher == nachher, (
        "Die Umwandlung verändert die Signatur.\n\n"
        f"vorher : {vorher}\n\nnachher: {nachher}")


def test_rundlauf_erkennt_die_typen_und_raet_nicht():
    """Beim eigenen HTML soll kaum etwas als Freitext landen — sonst wäre die
    Umwandlung zwar verlustfrei, aber nutzlos: alles bliebe HTML."""
    blocks = FAELLE[-1].values[0]
    meta = tp.parse_html(tb.render_html({"version": 1, "blocks": blocks}))
    typen = [b["type"] for b in meta["blocks"]]
    assert typen.count("freetext") == 0, f"unnötig als Freitext übernommen: {typen}"
    assert typen == ["greeting", "name_field", "field", "divider", "email_link",
                     "phone", "spacer", "logo"], typen


# ── Fremdes HTML: Inhalt erhalten, Struktur nicht erfinden ───────────────────

def test_fremdes_hinweisband_wird_erkannt():
    """Der konkrete Anlass: die von Hand geschriebenen Blog-Banner."""
    roh = ('<table cellpadding="0" cellspacing="0" border="0">'
           '<tr><td style="background:#EFF6FF;border-left:3px solid #2563EB;'
           'padding:6px 14px;color:#1E3A5F;">'
           '💡 <strong>Schon gewusst?</strong>&nbsp; Tipps &amp; Tools — '
           '<a href="https://blog.zarenko.net">blog.zarenko.net</a>'
           '</td></tr></table>')
    meta = tp.parse_html(roh)
    b = meta["blocks"][0]
    assert b["type"] == "box", f"Hinweisband nicht als Kasten erkannt: {b['type']}"
    assert b["border_side"] == "left" and b["border_width"] == 3
    assert b["fill_color"].lower() == "#eff6ff"
    assert b["padding"] == 6 and b["padding_x"] == 14
    # Der Inhalt darin bleibt vollständig.
    inhalt = tb.render_html({"version": 1, "blocks": [b]})
    assert "Schon gewusst?" in inhalt and "blog.zarenko.net" in inhalt


def test_unbekanntes_html_geht_nicht_verloren():
    """Was nicht erkannt wird, muss unverändert wieder herauskommen."""
    roh = ('<div class="wild"><p>Ein Absatz</p>'
           '<ul><li>eins</li><li>zwei</li></ul></div>')
    meta = tp.parse_html(roh)
    erneut = tb.render_html(meta)
    for stueck in ("Ein Absatz", "eins", "zwei"):
        assert stueck in erneut, f"'{stueck}' ging verloren:\n{erneut}"


def test_leeres_html():
    assert tp.parse_html("")["blocks"] == []
    assert tp.parse_html("   \n ")["blocks"] == []


def test_kaputtes_html_wirft_nicht():
    """Nicht geschlossene Tags dürfen den Editor nicht lahmlegen."""
    for roh in ("<table><tr><td>offen", "<div><span>x</div>",
                "<<>>", "<td>ohne tabelle</td>", "&amp;", "<br>"):
        meta = tp.parse_html(roh)
        assert isinstance(meta.get("blocks"), list), roh


def test_hinweise_melden_geratenes():
    """Der Nutzer muss erfahren, wo er nachsehen muss."""
    meta = tp.parse_html('<table><tr><td><p>irgendwas</p></td></tr></table>')
    assert meta["_hinweise"], "kein Hinweis trotz Freitext-Rückfall"
    assert "Freitext" in meta["_hinweise"][0]

    sauber = tp.parse_html(tb.render_html(
        {"version": 1, "blocks": [{"type": "field", "field": "jobTitle"}]}))
    assert not sauber["_hinweise"], f"Hinweis obwohl alles erkannt: {sauber['_hinweise']}"


def test_jinja_huellen_verschwinden_und_verdoppeln_sich_nicht():
    """`{% if %}` erzeugt der Renderer selbst wieder. Bliebe es stehen, stünde
    es nach zwei Runden doppelt im Quelltext."""
    blocks = [{"type": "field", "field": "jobTitle"}]
    einmal = tb.render_html({"version": 1, "blocks": blocks})
    assert "{% if" in einmal
    zweimal = tb.render_html(tp.parse_html(einmal))
    assert zweimal.count("{% if") == einmal.count("{% if"), \
        f"Jinja-Hüllen verdoppelt:\n{zweimal}"


def test_unbekannte_variable_wird_kein_feldblock():
    """Ein Ausdruck auf etwas, das der Editor nicht anbietet, würde als
    Feldblock beim nächsten Speichern stillschweigend verschwinden."""
    roh = '<table><tr><td style="padding:0">{{ user.erfundenesFeld }}</td></tr></table>'
    b = tp.parse_html(roh)["blocks"][0]
    assert b["type"] == "freetext", f"unbekanntes Feld wurde zu {b['type']}"
    assert "erfundenesFeld" in b["html"]


# ── Stellen, an denen frühere Fassungen still danebenlagen ───────────────────

def test_jinja_innerhalb_einer_zelle_verdoppelt_sich_nicht():
    """Die Anschrift trägt ihre `{% if %}` INNERHALB der Zelle.

    Blieben sie beim Zurücklesen stehen, landeten sie im Freitext — und der
    Renderer setzte beim nächsten Mal seine eigenen davor. Nach zwei Runden
    stünde die Bedingung doppelt im Quelltext.

    Der Rundlauf-Fall `anschrift` deckt das mit ab; diese Prüfung benennt es
    ausdrücklich, weil eine frühere Fassung genau hier durchrutschte: die
    Hüllen ausserhalb der Zeile werden ohnehin ignoriert, die innerhalb nicht.
    """
    blocks = [{"type": "address"}]
    einmal = tb.render_html({"version": 1, "blocks": blocks})
    assert "{% if" in einmal
    zweimal = tb.render_html(tp.parse_html(einmal))
    assert zweimal.count("{% if") == einmal.count("{% if"), (
        f"Jinja-Bedingungen verdoppelt:\n{zweimal}")
    assert "{% if" not in _jeder_freitext(tp.parse_html(einmal))


def _jeder_freitext(meta) -> str:
    raus = []

    def lauf(bs):
        for b in bs:
            if b.get("type") == "freetext":
                raus.append(b.get("html", ""))
            for k in ("children", "left", "right"):
                lauf(b.get(k) or [])
    lauf(meta["blocks"])
    return " ".join(raus)


def test_grussformel_nur_auf_oberster_ebene():
    """Im Kasten ist die erste Zeile fast nie eine Anrede.

    Der Unterschied ist nicht kosmetisch: `greeting` maskiert seinen Text,
    `freetext` gibt HTML durch. Ein Hinweistext, der fälschlich zur Grußformel
    wird, verliert beim nächsten Rendern seine Auszeichnung.
    """
    meta = tp.parse_html(tb.render_html({"version": 1, "blocks": [{
        "type": "box", "border_width": 1, "padding": 8, "radius": 0, "width": 0,
        "children": [{"type": "freetext", "html": "Hinweis"}]}]}))
    kind = meta["blocks"][0]["children"][0]
    assert kind["type"] != "greeting", (
        "Erste Zeile im Kasten wurde zur Grußformel — damit verlöre "
        "ausgezeichneter Text seine Auszeichnung.")
    assert (kind.get("text") or kind.get("html", "")).strip() == "Hinweis"


def test_feld_lehnt_zellen_mit_unterelementen_selbst_ab():
    """Die Absicherung, auf die sich die Erkenner-Reihenfolge verlässt.

    Steht `_als_feld` versehentlich vor `_als_link_block`, darf trotzdem keine
    Telefonzeile zum blossen Feld werden — sonst ginge der Link verloren, ohne
    dass es auffiele.
    """
    h = tb.render_html({"version": 1, "blocks": [{"type": "phone", "prefix": "Tel:"}]})
    wurzel = tp._baum(tp._JINJA_IF.sub("", h))
    zellen = [k for k in tp._alle(wurzel) if k.tag == "td"]
    assert tp._als_feld(zellen[0]) is None, (
        "_als_feld akzeptiert eine Zelle mit <a> darin — dann hinge die "
        "Richtigkeit allein an der Reihenfolge der Erkenner.")


def test_farbige_zelle_mit_feld_bleibt_ein_feld():
    """`_als_kasten_direkt` greift breit und steht deshalb zuletzt."""
    roh = ('<table><tr><td style="background:#eff6ff;padding:0">'
           '{{ user.jobTitle }}</td></tr></table>')
    b = tp.parse_html(roh)["blocks"][0]
    assert b["type"] == "field" and b["field"] == "jobTitle", (
        f"farbige Zelle mit Feld wurde zu {b['type']} — der breite "
        f"Kasten-Erkenner steht zu weit vorn.")


def test_bedingung_um_eine_nicht_erkannte_zeile_bleibt_erhalten():
    """Der Fall, für den die Bedingungs-Anheftung gebaut ist.

    Eine Zeile mit einem Filter (`| upper`) ist kein blosser Feldblock und
    fällt auf Freitext zurück. Ginge die Bedingung dabei verloren, erschiene
    die Zeile IMMER — bei leerem Feld also als leere Zeile in jeder Signatur.
    Das sieht niemand in der Vorschau, weil dort Beispielwerte gefüllt sind.
    """
    roh = ('<table><tr><td style="padding:0">Fest</td></tr>'
           '{% if user.jobTitle %}<tr><td style="padding:0">'
           '{{ user.jobTitle | upper }}</td></tr>{% endif %}</table>')
    meta = tp.parse_html(roh)
    freitexte = [b for b in meta["blocks"] if b["type"] == "freetext"]
    assert freitexte, f"erwartet Freitext, bekam {[b['type'] for b in meta['blocks']]}"
    mit_bedingung = [b for b in freitexte if "{% if user.jobTitle %}" in b["html"]]
    assert mit_bedingung, (
        "Die Bedingung ging verloren — die Zeile erschiene künftig immer, "
        f"auch ohne Wert:\n{freitexte}")
    assert "{% endif %}" in mit_bedingung[0]["html"]

    # Und sie darf sich beim Rendern nicht verdoppeln.
    erneut = tb.render_html(meta)
    assert erneut.count("{% if user.jobTitle %}") == 1, erneut


def test_bedingung_wird_nicht_an_erkannte_felder_geheftet():
    """Beim Feldblock erzeugt der Renderer die Bedingung selbst."""
    einmal = tb.render_html({"version": 1, "blocks": [
        {"type": "field", "field": "jobTitle"}]})
    meta = tp.parse_html(einmal)
    feld = meta["blocks"][0]
    assert feld["type"] == "field"
    assert "html" not in feld, (
        "Bedingung an einen Feldblock geheftet — sie stünde beim Rendern "
        "zusätzlich zur selbst erzeugten.")
    assert tb.render_html(meta).count("{% if") == einmal.count("{% if")


# ── Das Sicherheitsnetz ──────────────────────────────────────────────────────
#
# Alle Erkenner sind Heuristik. Ein Muster, an das niemand gedacht hat, kann
# eine Zeile verschlucken — und der Nutzer merkt es nicht: Er sieht eine
# plausible Blockliste und eine Vorschau, in der genau das fehlt, wonach er
# gerade nicht sucht. Dann speichert er.

def test_bestandsvorlagen_gehen_nie_verloren():
    """Über ALLE Vorlagen im Verzeichnis: der sichtbare Text bleibt gleich.

    Das ist die Prüfung, die zählt — nicht wie gut zerlegt wurde, sondern dass
    nichts verschwindet. Sie läuft gegen den echten Bestand, weil genau dort
    die Muster stehen, die sich niemand ausgedacht hat.
    """
    from pathlib import Path
    verz = Path(__file__).resolve().parents[1] / "templates"
    dateien = sorted(verz.glob("*.html"))
    assert dateien, "keine Vorlagen zum Prüfen gefunden"
    for f in dateien:
        roh = f.read_text(encoding="utf-8")
        meta = tp.parse_html(roh)
        meta.pop("_hinweise", None)
        assert not tp._verlust(roh, tb.render_html(meta)), (
            f"{f.name}: sichtbarer Inhalt ging bei der Umwandlung verloren")


def test_netz_faengt_einen_erfundenen_erkennerfehler():
    """Direkt geprüft: Wenn ein Erkenner Inhalt schluckt, muss das Netz greifen."""
    roh = ('<table><tr><td>Erste Zeile</td></tr>'
           '<tr><td>Zweite Zeile</td></tr></table>')
    echt = tp._block_aus_zelle
    try:
        # Erkenner, der die zweite Zeile verschluckt.
        tp._block_aus_zelle = lambda td, erster, oberste_ebene=True: (
            {"type": "freetext", "html": ""} if "Zweite" in td.nur_text()
            else echt(td, erster, oberste_ebene))
        meta = tp.parse_html(roh)
    finally:
        tp._block_aus_zelle = echt
    assert meta["_hinweise"], "kein Hinweis trotz Verlust"
    assert len(meta["blocks"]) == 1 and meta["blocks"][0]["type"] == "freetext"
    assert "Zweite Zeile" in meta["blocks"][0]["html"], "Inhalt trotz Netz verloren"


def test_netz_meldet_sich_nicht_ohne_not():
    """Bei sauber erkanntem HTML darf das Netz nicht zuschlagen — sonst wäre
    jede Vorlage ein Freitext und die Umwandlung wertlos."""
    einmal = tb.render_html({"version": 1, "blocks": [
        {"type": "greeting", "text": "Freundliche Grüße"},
        {"type": "name_field", "field": "displayName", "bold": True},
        {"type": "email_link"}]})
    meta = tp.parse_html(einmal)
    assert not meta["_hinweise"], f"Netz schlug grundlos zu: {meta['_hinweise']}"
    assert [b["type"] for b in meta["blocks"]] == ["greeting", "name_field", "email_link"]


def test_zeile_ohne_passendes_muster_wird_nicht_uebersprungen():
    """Drei Spalten sind kein Zweispalter — die Zeile muss trotzdem bleiben."""
    roh = ('<table><tr><td>A</td><td>B</td><td>C</td></tr></table>')
    meta = tp.parse_html(roh)
    erneut = tb.render_html(meta)
    for stueck in ("A", "B", "C"):
        assert stueck in erneut, f"Spalte '{stueck}' verschwand:\n{erneut}"


def test_bedingung_frisst_keinen_inhalt_zwischen_zwei_bedingungen():
    """Der Ausdruck für „Bedingung um eine Zeile" darf nicht überspannen.

    Mit `.+?` und re.S griff er vom ersten `{% if %}` mitten in einer Zelle bis
    zum letzten `{% endif %}` am Dateiende — die Ersetzung behielt nur die
    gefundene Zeile, alles dazwischen fiel weg. Bei signature.html verschwand so
    der komplette Kontaktblock.

    Auffällig wurde das erst am echten Bestand: Alle selbstgebauten Testfälle
    hatten je nur EINE Bedingung, und mit einer allein kann der Ausdruck nicht
    überspannen.
    """
    roh = ('<table>'
           '<tr><td>{% if a %}A{% else %}B{% endif %} Mitteldrin</td></tr>'
           '<tr><td>WICHTIGER INHALT</td></tr>'
           '{% if c %}<tr><td>Optional</td></tr>{% endif %}'
           '</table>')
    meta = tp.parse_html(roh)
    meta.pop("_hinweise", None)
    erneut = tb.render_html(meta)
    assert "WICHTIGER INHALT" in erneut, (
        f"Inhalt zwischen zwei Bedingungen verschwunden:\n{erneut}")
    assert "Mitteldrin" in erneut and "Optional" in erneut


def test_bestandsvorlagen_werden_zerlegt_nicht_nur_gerettet():
    """Das Netz ist die Rückfallebene, nicht das Ergebnis.

    Griffe es bei jeder Vorlage, wäre die Umwandlung formal verlustfrei und
    praktisch wertlos: alles bliebe ein Block HTML.
    """
    from pathlib import Path
    verz = Path(__file__).resolve().parents[1] / "templates"
    im_netz = []
    for f in sorted(verz.glob("*.html")):
        meta = tp.parse_html(f.read_text(encoding="utf-8"))
        if meta["_hinweise"] and len(meta["blocks"]) == 1:
            im_netz.append(f.name)
    assert not im_netz, (
        f"Diese Vorlagen liessen sich nur als Ganzes retten: {im_netz} — "
        f"die Zerlegung greift dort nicht.")


# ── Feste Angaben dürfen nicht zu Platzhaltern werden ────────────────────────
#
# Der gefährlichste Fehler dieses Moduls: Ein Kontaktbaustein rendert
# `{{ user.mail }}` — die Daten des jeweiligen Postfachs. Wird eine FESTE
# Adresse dazu gemacht, sieht die Signatur unverändert aus, zeigt aber etwas
# anderes. Aufgefallen beim Durchlauf durch echte empfangene Fremdmails: dort
# steht naturgemäß nie ein Platzhalter.

def _zelle(html: str):
    wurzel = tp._baum(html)
    return [k for k in tp._alle(wurzel) if k.tag == "td"][0]


def test_feste_mailadresse_wird_kein_kontaktbaustein():
    b = tp._als_link_block(_zelle(
        '<td>E-Mail: <a href="mailto:info@fremdefirma.de">info@fremdefirma.de</a></td>'))
    assert b is None, (
        f"feste Adresse wurde zu {b} — beim Rendern stünde dort die Adresse "
        f"des Postfachinhabers statt info@fremdefirma.de")


def test_feste_rufnummer_wird_kein_kontaktbaustein():
    b = tp._als_link_block(_zelle(
        '<td>Tel: <a href="tel:+4924112345">+49 241 12345</a></td>'))
    assert b is None, f"feste Rufnummer wurde zu {b}"


def test_platzhalter_werden_weiterhin_erkannt():
    """Die Gegenprobe — sonst wäre die Absicherung zu scharf."""
    assert tp._als_link_block(_zelle(
        '<td><a href="mailto:{{ user.mail }}">{{ user.mail }}</a></td>'
    ))["type"] == "email_link"
    b = tp._als_link_block(_zelle(
        '<td>Tel: <a href="tel:{{ user.phone }}">{{ user.phone }}</a></td>'))
    assert b["type"] == "phone" and b["prefix"] == "Tel:"
    b = tp._als_link_block(_zelle(
        '<td><a href="tel:{{ user.mobilePhone }}">x</a></td>'))
    assert b["type"] == "mobile"


def test_feste_angaben_bleiben_im_ergebnis_wortgetreu():
    """Der Beweis über den ganzen Weg: was drinstand, steht danach noch drin."""
    roh = ('<div><p>Musterfirma GmbH</p>'
           '<p>Tel: <a href="tel:+4924112345">+49 241 12345</a></p>'
           '<p><a href="mailto:info@fremdefirma.de">info@fremdefirma.de</a></p></div>')
    meta = tp.parse_html(roh)
    meta.pop("_hinweise", None)
    erneut = tb.render_html(meta)
    for fest in ("info@fremdefirma.de", "+49 241 12345", "Musterfirma GmbH"):
        assert fest in erneut, f"'{fest}' ging verloren oder wurde ersetzt:\n{erneut}"
    assert "{{ user." not in erneut, (
        f"feste Angaben wurden durch Platzhalter ersetzt:\n{erneut}")


# ── Layout ohne Tabellen ─────────────────────────────────────────────────────

def test_div_layout_wird_zerlegt():
    """Von 276 empfangenen Fremdmails hatten 156 gar keine Tabelle."""
    roh = ('<div><p>Freundliche Grüße</p><p><b>Max Mustermann</b></p>'
           '<p>Musterfirma GmbH</p><p>Musterweg 1, 12345 Musterstadt</p></div>')
    meta = tp.parse_html(roh)
    assert len(meta["blocks"]) >= 4, (
        f"div/p-Layout nicht zerlegt: {[b['type'] for b in meta['blocks']]}")
    erneut = tb.render_html(meta)
    for stueck in ("Max Mustermann", "Musterfirma GmbH", "Musterweg 1"):
        assert stueck in erneut, f"'{stueck}' fehlt:\n{erneut}"


def test_verschachtelte_huellen_werden_durchstiegen():
    """Echte Mails bringen `<html><body><div><div>…` mit — je ein Kind.
    Ohne Absteigen läge alles in einem einzigen Block."""
    roh = ('<html><body><div><div>'
           '<p>Zeile eins</p><p>Zeile zwei</p><p>Zeile drei</p>'
           '</div></div></body></html>')
    meta = tp.parse_html(roh)
    assert len(meta["blocks"]) >= 3, (
        f"Hüllen nicht durchstiegen: {len(meta['blocks'])} Block/Blöcke")


def test_alternativtext_wird_nicht_erfunden():
    """Ein Logo ohne oder mit eigenem Alternativtext darf nicht den
    Firmennamen des Postfachs bekommen — das erfände Inhalt.

    An 276 empfangenen Fremdmails aufgefallen: dort trugen die Bilder eigene
    Alternativtexte oder gar keine.
    """
    # eigener Text bleibt
    meta = tp.parse_html('<table><tr><td><img src="x.png" width="90" '
                         'alt="Logo Musterfirma"></td></tr></table>')
    meta.pop("_hinweise", None)
    erneut = tb.render_html(meta)
    assert 'alt="Logo Musterfirma"' in erneut, erneut
    assert "user.companyName" not in erneut

    # kein Text → leer, nicht erfunden
    meta = tp.parse_html('<table><tr><td><img src="x.png" width="90"></td></tr></table>')
    meta.pop("_hinweise", None)
    erneut = tb.render_html(meta)
    assert 'alt=""' in erneut, erneut
    assert "user.companyName" not in erneut


def test_eigene_vorlagen_behalten_den_platzhalter():
    """Gegenprobe: Im Baukasten angelegte Logos sollen weiter den Firmennamen
    tragen — sonst wäre der Rundlauf kaputt."""
    einmal = tb.render_html({"version": 1, "blocks": [
        {"type": "logo", "url": "x.png", "width": 90}]})
    assert "{{ user.companyName }}" in einmal
    zweimal = tb.render_html(tp.parse_html(einmal))
    assert "{{ user.companyName }}" in zweimal, (
        "Der Platzhalter ging beim Rundlauf verloren:\n" + zweimal)


def test_fliesstext_neben_tabelle_geht_nicht_verloren():
    """Der schwerste Fund aus dem Lauf gegen echte Geschäftsmails.

    Steht in einem Behälter sowohl Text als AUCH eine Tabelle, sammelte die
    Zeilensuche nur die Tabellenzeilen ein — alles daneben fiel weg. An einer
    echten Nachricht gemessen: 164 von 174 Wörtern. Das trifft auch Vorlagen,
    sobald jemand eine Zeile über oder unter seine Tabelle schreibt.
    """
    roh = ('<div>'
           '<p>Text VOR der Tabelle</p>'
           '<table><tr><td>In der Tabelle</td></tr></table>'
           '<p>Text NACH der Tabelle</p>'
           '</div>')
    meta = tp.parse_html(roh)
    hinweise = meta.pop("_hinweise", [])
    erneut = tb.render_html(meta)
    for stueck in ("Text VOR der Tabelle", "In der Tabelle", "Text NACH der Tabelle"):
        assert stueck in erneut, f"'{stueck}' ging verloren:\n{erneut}"
    # Und zwar ZERLEGT, nicht durch das Netz gerettet: Sonst bestünde diese
    # Prüfung auch dann, wenn die Zerlegung alles daneben verwirft.
    assert len(meta["blocks"]) >= 3, (
        f"nur {len(meta['blocks'])} Block/Blöcke — der Inhalt wurde vom "
        f"Sicherheitsnetz gerettet statt zerlegt: {hinweise}")


def test_reine_tabelle_bleibt_zeilenweise():
    """Gegenprobe: eine Vorlage, die NUR aus einer Tabelle besteht, muss
    weiterhin Zeile für Zeile zerlegt werden — sonst wäre der ganze
    Baukasten-Rundlauf hinfällig."""
    einmal = tb.render_html({"version": 1, "blocks": [
        {"type": "greeting", "text": "Freundliche Grüße"},
        {"type": "name_field", "field": "displayName", "bold": True},
        {"type": "divider"}]})
    meta = tp.parse_html(einmal)
    assert [b["type"] for b in meta["blocks"]] == ["greeting", "name_field", "divider"], \
        [b["type"] for b in meta["blocks"]]


def test_huellen_mit_head_werden_durchstiegen():
    """Echte Nachrichten haben <html><head>…</head><body>…</body></html>.

    <html> hat damit ZWEI Kinder, und ein Abstieg, der nur bei genau einem
    weitergeht, bleibt sofort stehen — die ganze Signatur landete in einem
    einzigen Baustein. Der frühere Test hatte keinen <head> und konnte das
    nicht zeigen.
    """
    roh = ('<html><head><style>p{margin:0}</style></head><body>'
           '<p>Zeile eins</p><p>Zeile zwei</p><p>Zeile drei</p>'
           '</body></html>')
    meta = tp.parse_html(roh)
    assert len(meta["blocks"]) >= 3, (
        f"<head> hielt den Abstieg auf: {len(meta['blocks'])} Block/Blöcke")


def test_formatvorlagen_zaehlen_nicht_als_inhalt():
    """CSS im <style> ist kein sichtbarer Text.

    Zählte es mit, sähe das Verwerfen eines <head> wie Inhaltsverlust aus —
    und der Verlustschutz schlüge bei jeder echten Nachricht an.
    """
    mit = tp._sichtbarer_text('<style>.a{color:red}</style><p>Hallo</p>')
    ohne = tp._sichtbarer_text('<p>Hallo</p>')
    assert mit == ohne, "CSS wurde als sichtbarer Text gezählt"


# ── Aus dem Praxislauf an echten Signaturen ──────────────────────────────────

def test_leerzeilen_werden_abstandshalter():
    """Wer in Outlook eine Leerzeile setzt, erzeugt `&nbsp;` — oft in <o:p>
    verpackt. Als Freitext übernommen stünde in der Blockliste ein Baustein
    mit dem Inhalt „&nbsp;", dessen Zweck niemand ansieht."""
    for roh, marke in (('<td>&nbsp;</td>', "blank"),
                       ('<td><o:p>&nbsp;</o:p></td>', "o:p"),
                       ('<td><span>&nbsp;</span></td>', "span"),
                       ('<td><br></td>', "br")):
        wurzel = tp._baum(roh)
        td = [k for k in tp._alle(wurzel) if k.tag == "td"][0]
        b = tp._als_leerzeile(td)
        assert b and b["type"] == "spacer", f"{marke}: {b}"


def test_leerzeile_mit_hoehe_uebernimmt_sie():
    wurzel = tp._baum('<td style="height:14px">&nbsp;</td>')
    td = [k for k in tp._alle(wurzel) if k.tag == "td"][0]
    assert tp._als_leerzeile(td)["height"] == 14


def test_zeile_mit_inhalt_wird_kein_abstand():
    """Gegenprobe — sonst verschwände Text."""
    for roh in ('<td>Text</td>', '<td><o:p>Text</o:p></td>',
                '<td><img src="x.png"></td>', '<td><a href="x">y</a></td>'):
        wurzel = tp._baum(roh)
        td = [k for k in tp._alle(wurzel) if k.tag == "td"][0]
        assert tp._als_leerzeile(td) is None, roh


def test_name_ist_keine_grussformel():
    """In echten Signaturen steht in der ersten Zeile oft der NAME.

    Aufgefallen beim Übernehmen einer echten Signatur: Aus „Mats Barnick"
    wurde eine „Grußformel". Auf die Ausgabe wirkt sich das nicht aus — beide
    geben denselben Text —, auf die Verständlichkeit des Baukastens sehr wohl.
    """
    meta = tp.parse_html('<table><tr><td>Max Mustermann</td></tr>'
                         '<tr><td>Musterfirma GmbH</td></tr></table>')
    assert meta["blocks"][0]["type"] != "greeting", (
        f"Name als Grußformel eingeordnet ({meta['blocks'][0]['type']})")
    assert (meta["blocks"][0].get("text")
            or meta["blocks"][0].get("html", "")).strip() == "Max Mustermann"


def test_echte_grussformeln_werden_weiter_erkannt():
    """Gegenprobe — sonst wäre die Verschärfung zu scharf."""
    for text in ("Mit freundlichen Grüßen", "Freundliche Grüße",
                 "Viele Grüße", "Kind regards", "Best regards"):
        meta = tp.parse_html(f'<table><tr><td>{text}</td></tr>'
                             f'<tr><td>Max Mustermann</td></tr></table>')
        assert meta["blocks"][0]["type"] == "greeting", (
            f"'{text}' nicht als Grußformel erkannt")


def test_leerzeile_kommt_auch_ueber_den_ganzen_weg_als_abstand_an():
    """Nicht nur die Einzelfunktion — auch die Verdrahtung.

    Die erste Fassung dieser Prüfungen rief `_als_leerzeile` direkt auf. Nimmt
    man den Erkenner aus der Liste, bestanden sie trotzdem: Der Test sah die
    Funktion, nicht ihren Einsatz.
    """
    meta = tp.parse_html('<table><tr><td>Max Mustermann</td></tr>'
                         '<tr><td><o:p>&nbsp;</o:p></td></tr>'
                         '<tr><td>Musterfirma GmbH</td></tr></table>')
    typen = [b["type"] for b in meta["blocks"]]
    assert "spacer" in typen, f"Leerzeile kam nicht als Abstand an: {typen}"


# ── Der Aufbau der mitgelieferten Standardvorlage ────────────────────────────

def test_trennzelle_zaehlt_nicht_als_spalte():
    """Der übliche Aufbau ist Logo | schmale Trennzelle | Kontaktdaten.

    Drei Zellen für zwei Spalten. Zählte die Trennzelle mit, wurde daraus kein
    Zweispalter, und der ganze Kontaktblock fiel als ein Freitext an — genau
    das passierte mit der mitgelieferten Standardvorlage.
    """
    roh = ('<table><tr><td>'
           '<table><tr>'
           '<td><img src="logo.png" width="100" alt="Logo"></td>'
           '<td style="border-left:1pt solid #7F7F7F;padding:0">&nbsp;</td>'
           '<td>{{ user.companyName }}</td>'
           '</tr></table>'
           '</td></tr></table>')
    meta = tp.parse_html(roh)
    typen = [b["type"] for b in meta["blocks"]]
    assert "two_col" in typen, f"Zweispalter nicht erkannt: {typen}"
    zwei = [b for b in meta["blocks"] if b["type"] == "two_col"][0]
    assert len(zwei["left"]) == 1 and len(zwei["right"]) == 1, zwei
    assert zwei["divider"] is True, "Trennlinie nicht vermerkt"


def test_leere_zelle_ohne_rahmen_bleibt_eine_spalte():
    """Gegenprobe: Nicht jede leere Zelle ist ein Trenner — ohne Rahmen ist es
    eine echte, nur leere Spalte, und drei davon sind kein Zweispalter."""
    roh = ('<table><tr>'
           '<td>A</td><td style="padding:0">&nbsp;</td><td>B</td>'
           '</tr></table>')
    meta = tp.parse_html(roh)
    meta.pop("_hinweise", None)
    erneut = tb.render_html(meta)
    assert "A" in erneut and "B" in erneut


def test_praefix_wird_nicht_doppelt_maskiert():
    """`Phone:&nbsp;` darf beim Zurücklesen nicht zu `Phone:&amp;nbsp;` werden.

    Der Renderer maskiert den Präfix beim Ausgeben. Übernimmt man ihn roh,
    erscheint beim Empfänger sichtbar „Phone:&nbsp;" statt eines Abstands.
    Die mitgelieferte Standardvorlage hat genau solche Präfixe.
    """
    roh = ('<table><tr><td>Phone:&nbsp;&nbsp;'
           '<a href="tel:{{ user.phone }}">{{ user.phone }}</a></td></tr></table>')
    meta = tp.parse_html(roh)
    meta.pop("_hinweise", None)
    b = meta["blocks"][0]
    assert b["type"] == "phone", b
    assert "&nbsp;" not in b.get("prefix", ""), f"Entity im Präfix: {b['prefix']!r}"
    assert "&amp;nbsp;" not in tb.render_html(meta), "doppelt maskiert"


def test_standardvorlage_wird_vollstaendig_zerlegt():
    """Die mitgelieferten Vorlagen müssen ihren Kontaktblock als Zweispalter
    zeigen — sonst steht dort ein unverständlicher Block HTML."""
    from pathlib import Path
    verz = Path(__file__).resolve().parents[1] / "templates"
    for name in ("signature.html", "default-without-greeting.html"):
        f = verz / name
        if not f.exists():
            continue
        meta = tp.parse_html(f.read_text(encoding="utf-8"))
        meta.pop("_hinweise", None)
        typen = [b["type"] for b in meta["blocks"]]
        assert "two_col" in typen, f"{name}: kein Zweispalter, nur {typen}"
        zwei = [b for b in meta["blocks"] if b["type"] == "two_col"][0]
        rechts = [b["type"] for b in zwei["right"]]
        assert "phone" in rechts and "email_link" in rechts, (
            f"{name}: Kontaktdaten nicht zerlegt, rechts steht {rechts}")


def test_bild_in_einer_spalte_wird_ein_logo_baustein():
    """Eine Spaltenzelle mit nur einem Bild ist ein Logo, keine Sammlung.

    Der Weg über die Zeilen-Tags kennt `<img>` nicht und machte daraus sofort
    Freitext — das Firmenlogo der Standardvorlage kam als roher Quelltext an,
    obwohl `_als_logo` es erkannt hätte. Die Erkenner liefen für einen
    Behälter ohne Zeilenstruktur schlicht nicht.
    """
    roh = ('<td width="116"><img src="data:image/png;base64,AAAA" '
           'width="100" alt="Logo"></td>')
    wurzel = tp._baum(roh)
    td = [k for k in tp._alle(wurzel) if k.tag == "td"][0]
    bloecke = tp._bloecke_aus(td)
    assert [b["type"] for b in bloecke] == ["logo"], (
        f"Bild kam als {[b['type'] for b in bloecke]} an statt als Logo")
    assert bloecke[0]["width"] == 100


def test_standardvorlage_zeigt_ihre_logos_als_bausteine():
    """Über den ganzen Weg an der echten Vorlage."""
    from pathlib import Path
    f = Path(__file__).resolve().parents[1] / "templates/signature.html"
    if not f.exists():
        return
    meta = tp.parse_html(f.read_text(encoding="utf-8"))
    meta.pop("_hinweise", None)

    def sammle(bs):
        for b in bs:
            yield b["type"]
            for k in ("left", "right", "children"):
                yield from sammle(b.get(k) or [])

    typen = list(sammle(meta["blocks"]))
    assert typen.count("logo") >= 1, f"kein Logo-Baustein: {typen}"
    for erwartet in ("two_col", "phone", "email_link"):
        assert erwartet in typen, f"{erwartet} fehlt: {typen}"


# ── Das Ergebnis muss ein GÜLTIGES Template sein ─────────────────────────────
#
# Der schwerste Fehler dieser Reihe: Eine erzeugte Vorlage enthielt
# `{% else %}` ohne `{% if %}`. Jinja bricht darauf ab, und die Signatur kam
# beim Empfänger LEER an. Die Verlustprüfung sah nichts davon — sie misst
# sichtbaren Text, und Steueranweisungen zählen dort zu Recht nicht mit.
# Eine Vorlage kann also inhaltlich vollständig und trotzdem unbrauchbar sein.

def _jinja_pruefen(html: str) -> None:
    from jinja2 import Environment
    Environment().parse(html)


def test_ergebnis_ist_gueltiges_jinja_bei_allen_bestandsvorlagen():
    from pathlib import Path
    verz = Path(__file__).resolve().parents[1] / "templates"
    for f in sorted(verz.glob("*.html")):
        roh = f.read_text(encoding="utf-8")
        meta = tp.parse_html(roh)
        meta.pop("_hinweise", None)
        erzeugt = tb.render_html(meta)
        try:
            _jinja_pruefen(erzeugt)
        except Exception as exc:
            raise AssertionError(
                f"{f.name}: die umgewandelte Vorlage ist kein gültiges "
                f"Template ({exc}) — sie würde beim Versand LEER erscheinen."
            ) from exc


def test_bedingung_in_einer_zelle_bleibt_vollstaendig():
    """`{% if %}…{% else %}…{% endif %}` INNERHALB einer Zelle darf nicht
    zerpflückt werden. Genau daran zerbrach die Standardvorlage."""
    roh = ('<table><tr><td>'
           '{% if user.companyName %}{{ user.companyName }}'
           '{% else %}{{ user.displayName }}{% endif %}'
           '</td></tr></table>')
    meta = tp.parse_html(roh)
    meta.pop("_hinweise", None)
    erzeugt = tb.render_html(meta)
    _jinja_pruefen(erzeugt)          # wirft, wenn zerpflückt
    assert erzeugt.count("{% if") == 1, erzeugt
    assert erzeugt.count("{% else") == 1, erzeugt
    assert erzeugt.count("{% endif") == 1, erzeugt


def test_umgewandelte_vorlage_rendert_auch_wirklich():
    """Über den ganzen Weg: parsen, erzeugen, mit Werten füllen.

    Die stärkste Prüfung — sie hätte den Leer-Fehler sofort gezeigt.
    """
    from jinja2.sandbox import SandboxedEnvironment
    roh = ('<table>'
           '<tr><td>{% if user.companyName %}{{ user.companyName }}'
           '{% else %}{{ user.displayName }}{% endif %}</td></tr>'
           '<tr><td>{% if user.phone %}Tel: {{ user.phone }}{% endif %}</td></tr>'
           '</table>')
    meta = tp.parse_html(roh)
    meta.pop("_hinweise", None)
    erzeugt = tb.render_html(meta)
    ausgabe = SandboxedEnvironment().from_string(erzeugt).render(
        user={"companyName": "Musterfirma", "phone": "+49 241 1"}, custom={})
    assert "Musterfirma" in ausgabe, f"Vorlage rendert leer:\n{ausgabe!r}"
    assert "+49 241 1" in ausgabe
    assert ausgabe.strip(), "Ergebnis ist vollständig leer"


# ── Fließtext wird gesammelt, nicht zerschnitten ─────────────────────────────

def test_fliesstext_bleibt_ein_baustein():
    """Ein Satz mit Auszeichnung ist EIN Baustein, nicht drei.

    „💡 <strong>Schon gewusst?</strong> Tipps & Tools — <a …>blog</a>" wurde an
    den Element-Grenzen zerschnitten. Jeder Teil wird eine eigene Tabellenzeile:
    Aus einem umbrechenden Fließtext werden drei starre Zeilen, die an den
    früheren Elementgrenzen brechen statt am Rand.
    """
    roh = ('<table><tr><td style="background:#eff6ff;border-left:3px solid #2563eb">'
           '💡 <strong>Schon gewusst?</strong>&nbsp; Tipps &amp; Tools — '
           '<a href="https://blog.example">blog.example</a>'
           '</td></tr></table>')
    meta = tp.parse_html(roh)
    meta.pop("_hinweise", None)
    kasten = meta["blocks"][0]
    assert kasten["type"] == "box"
    assert len(kasten["children"]) == 1, (
        f"Fließtext in {len(kasten['children'])} Teile zerschnitten: "
        + str([(b.get("html") or b.get("text") or "")[:30] for b in kasten["children"]]))
    # Seit der Auszeichnung kann daraus ein Textbaustein werden — beide Formen
    # sind richtig, entscheidend ist die Vollständigkeit in EINEM Baustein.
    inhalt = kasten["children"][0].get("html") or kasten["children"][0].get("text") or ""
    assert "Schon gewusst?" in inhalt
    assert "blog.example" in inhalt


def test_echte_absatzgrenzen_trennen_weiterhin():
    """Gegenprobe: An <p> und <div> wird sehr wohl getrennt — sonst klebte die
    ganze Signatur in einem Baustein."""
    roh = "<div><p>Erster Absatz</p><p>Zweiter Absatz</p><p>Dritter</p></div>"
    meta = tp.parse_html(roh)
    assert len(meta["blocks"]) == 3, [b.get("text") or b.get("html") for b in meta["blocks"]]


def test_reiner_text_wird_ein_freitext_baustein():
    """Ohne Auszeichnung entsteht der Baustein MIT Feldern (Farbe, Größe, fett),
    nicht der HTML-Baustein."""
    roh = "<div><p>Nur schlichter Text</p></div>"
    b = tp.parse_html(roh)["blocks"][0]
    assert b["type"] == "text", b
    assert b["text"] == "Nur schlichter Text"


def test_einzelnes_bild_bleibt_ein_logo():
    """Das Sammeln darf den Erkennern nichts wegfangen."""
    roh = '<td width="116"><img src="x.png" width="100" alt="Logo"></td>'
    wurzel = tp._baum(roh)
    td = [k for k in tp._alle(wurzel) if k.tag == "td"][0]
    assert [b["type"] for b in tp._bloecke_aus(td)] == ["logo"]


def test_keine_endlosschleife_bei_zellen_ohne_zeilen():
    """`_als_kasten_direkt` rief `_bloecke_aus` auf DERSELBEN Zelle auf, und die
    fiel ohne Zeilen wieder auf den Kasten-Erkenner zurück — endlos.

    Ein solcher Fehler zeigt sich nicht als fehlgeschlagene Zusicherung, sondern
    als hängender Lauf; deshalb hier mit knapper Rekursionsgrenze geprüft.
    """
    import sys
    roh = ('<table><tr><td style="background:#eff6ff;border-left:3px solid #000">'
           'Inhalt</td></tr></table>')
    alt = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(120)
        meta = tp.parse_html(roh)      # wirft RecursionError, wenn die Schleife zurück ist
    finally:
        sys.setrecursionlimit(alt)
    assert meta["blocks"], meta
