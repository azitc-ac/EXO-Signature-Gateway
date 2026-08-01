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
    assert kind["type"] == "freetext", (
        f"Erste Zeile im Kasten wurde zur Grußformel ({kind['type']}) — "
        f"damit verlöre ausgezeichneter Text seine Auszeichnung.")


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
