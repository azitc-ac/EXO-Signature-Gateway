"""Lassen sich die vorhandenen Hinweisbänder mit dem Baukasten nachbauen?

Anlass: Die drei Blog-Banner (templates/Blog-Banner*.html) wurden von Hand
geschrieben. Der Baukasten konnte sie nicht abbilden — ihm fehlten der
einseitige Rahmen, getrennte Innenabstände und das Etikett.

Diese Prüfung vergleicht nicht Zeichen für Zeichen (Reihenfolge und
Leerraum dürfen abweichen), sondern die MERKMALE, an denen das Band hängt.
Ein Vergleich der ganzen Zeichenkette wäre beim ersten Umbau des Renderers
fällig geworden, ohne dass etwas kaputt ist.
"""
import re

import template_builder as tb


def _html(bloecke):
    return tb.render_html({"version": 1, "blocks": bloecke})


def test_hinweisband_mit_linkem_balken():
    """Blog-Banner.html: heller Grund, 3px blauer Balken NUR links, 6/14 Innenabstand."""
    html = _html([{
        "type": "box", "border_side": "left", "border_width": 3,
        "border_color": "#2563EB", "filled": True, "fill_color": "#EFF6FF",
        "padding": 6, "padding_x": 14, "radius": 0, "width": 0,
        "children": [{"type": "freetext",
                      "html": '💡 <strong>Schon gewusst?</strong>&nbsp; Tipps '
                              '&amp; Tools — <a href="https://blog.zarenko.net">'
                              'blog.zarenko.net</a>'}],
    }])
    assert "border-left:3px solid #2563EB" in html, html
    assert "border:3px" not in html, "Rahmen läuft rundum statt nur links"
    assert "border-right" not in html and "border-top" not in html
    assert "padding:6px 14px" in html, "getrennte Innenabstände fehlen"
    assert "background-color:#EFF6FF" in html
    assert "Schon gewusst?" in html and "blog.zarenko.net" in html


def test_etikett_band():
    """Blog-Banner-Text.html: farbiges Etikett, dahinter Fließtext mit Link."""
    html = _html([{
        "type": "badge", "label": "RSS", "badge_color": "#F26522",
        "label_color": "#ffffff", "size": "8pt", "radius": 2,
        "html": 'Regelmäßig neue Beiträge: <a href="https://blog.zarenko.net">'
                'blog.zarenko.net</a>',
    }])
    assert ">RSS</span>" in html
    assert "background:#F26522" in html
    assert "color:#ffffff" in html
    assert "border-radius:2px" in html
    assert "vertical-align:middle" in html, "Etikett hinge in Outlook unter der Zeile"
    assert "blog.zarenko.net" in html


def test_etikett_im_textteil_erkennbar():
    """Die Textfassung darf das Etikett nicht verschlucken — sonst beginnt der
    Satz mitten im Wort."""
    txt = tb.render_txt({"version": 1, "blocks": [{
        "type": "badge", "label": "RSS",
        "html": 'Neue Beiträge: <a href="https://blog.zarenko.net">blog.zarenko.net</a>',
    }]})
    assert "[RSS]" in txt, f"Etikett fehlt in der Textfassung:\n{txt}"
    assert "Neue Beiträge" in txt
    assert "<a" not in txt, "HTML in der Textfassung"


def test_umlaufender_rahmen_bleibt_der_vorgabefall():
    """Bestehende Vorlagen ohne border_side dürfen sich nicht ändern."""
    html = _html([{"type": "box", "border_width": 1, "border_color": "#e2e8f0",
                   "padding": 12, "radius": 0, "width": 0,
                   "children": [{"type": "freetext", "html": "x"}]}])
    assert "border:1px solid #e2e8f0" in html
    assert "border-left:" not in html


def test_gleicher_innenabstand_bleibt_kurz_geschrieben():
    """Ohne padding_x soll `padding:12px` herauskommen, nicht `padding:12px 12px`
    — sonst ändert sich jede bestehende Vorlage beim nächsten Speichern."""
    html = _html([{"type": "box", "padding": 12, "border_width": 0, "radius": 0,
                   "children": [{"type": "freetext", "html": "x"}]}])
    assert "padding:12px;" in html or 'padding:12px"' in html, html
    assert "padding:12px 12px" not in html


def test_einseitiger_rahmen_erzeugt_kein_vml():
    """VML zeichnet immer umlaufend — ein linker Balken würde in Outlook zum
    Rahmen ringsum, also zum Gegenteil der Absicht."""
    html = _html([{"type": "box", "border_side": "left", "border_width": 3,
                   "radius": 8, "width": 520, "padding": 6,
                   "children": [{"type": "freetext", "html": "x"}]}])
    assert "v:roundrect" not in html, "VML trotz einseitigem Rahmen ausgegeben"


def test_runde_ecken_mit_umlaufendem_rahmen_weiter_mit_vml():
    """Die Gegenprobe: der bisherige Fall muss VML behalten."""
    html = _html([{"type": "box", "border_width": 1, "radius": 8, "width": 520,
                   "padding": 12, "children": [{"type": "freetext", "html": "x"}]}])
    assert "v:roundrect" in html


def test_kein_block_ohne_textfassung():
    """Wächter: jeder Blocktyp im HTML-Verteiler muss auch im Textteil
    vorkommen. Sonst verschwindet er still aus der Nur-Text-Signatur — und
    das sieht niemand, der nur die HTML-Vorschau ansieht."""
    import inspect
    quelle = inspect.getsource(tb._render_block_txt) if hasattr(tb, "_render_block_txt") \
        else inspect.getsource(tb)
    # Bewusst ohne Textfassung, mit Grund — sonst meldet der Wächter dauerhaft
    # und wird ignoriert.
    OHNE_TEXTFASSUNG = {
        "logo": "ein Bild hat in der Nur-Text-Fassung keine Entsprechung; der "
                "Block trägt keinen Alternativtext, aus dem sich einer bilden ließe",
    }
    fehlend = [t for t in tb._HTML_RENDERERS
               if f'"{t}"' not in quelle and t not in OHNE_TEXTFASSUNG]
    assert not fehlend, (f"Blocktypen ohne Textfassung: {fehlend} — "
                         f"sie fehlen still in der Nur-Text-Signatur.")


# ── Klartext-Umwandlung: der Eckfall, der beim Nachbau auffiel ────────────────

def test_entities_werden_im_textteil_aufgeloest():
    """„Tipps &amp; Tools" darf in der Nur-Text-Signatur nicht wörtlich stehen.

    Gefunden beim Nachbau von Blog-Banner.html: der Text enthält `&nbsp;` und
    `&amp;`, und beide standen roh in der Textfassung. Beim Empfänger sieht das
    nach kaputter Software aus.
    """
    txt = tb.render_txt({"version": 1, "blocks": [{
        "type": "freetext",
        "html": '💡 <strong>Schon gewusst?</strong>&nbsp; Tipps &amp; Tools — '
                '<a href="https://blog.zarenko.net">blog.zarenko.net</a>'}]})
    assert "&amp;" not in txt and "&nbsp;" not in txt, f"rohe Entities:\n{txt}"
    assert "Tipps & Tools" in txt
    assert "Schon gewusst?" in txt


def test_entities_auch_im_etikett_block():
    """Dieselbe Umwandlung, zweiter Block — sie darf nicht auseinanderlaufen."""
    txt = tb.render_txt({"version": 1, "blocks": [{
        "type": "badge", "label": "RSS",
        "html": "Exchange&nbsp;Online &amp; Intune"}]})
    assert "&amp;" not in txt and "&nbsp;" not in txt, f"rohe Entities:\n{txt}"
    assert "Exchange" in txt and "Intune" in txt


def test_maskiertes_tag_ueberlebt_die_umwandlung():
    """Reihenfolge-Falle: erst Tags weg, DANN Entities auflösen.

    Umgekehrt würde aus `&lt;b&gt;` erst `<b>` und dann von der Tag-Regel
    entfernt — aus einer harmlosen Zeichenfolge verschwände Inhalt.
    """
    txt = tb.render_txt({"version": 1, "blocks": [{
        "type": "freetext", "html": "Nutze &lt;b&gt; für fett"}]})
    assert "<b>" in txt, f"maskiertes Tag verschluckt:\n{txt}"


# ── Nachträglich einrahmen ───────────────────────────────────────────────────

def test_bestehende_bloecke_im_kasten_rendern_unveraendert():
    """„Signatur einrahmen" schiebt die vorhandenen Blöcke in einen Kasten.
    Der Renderer muss sie dort genauso darstellen wie vorher — sonst wäre der
    Rahmen nicht das Einzige, was sich ändert.
    """
    inhalt = [
        {"type": "greeting", "text": "Freundliche Grüße"},
        {"type": "name_field", "field": "displayName", "bold": True},
        {"type": "field", "field": "jobTitle"},
        {"type": "divider"},
        {"type": "badge", "label": "RSS", "html": "Neues im Blog"},
    ]
    ohne = _html(inhalt)
    mit = _html([{"type": "box", "border_width": 1, "border_color": "#e2e8f0",
                  "padding": 12, "radius": 0, "width": 0, "children": inhalt}])

    # Jede inhaltliche Zeile aus der ungerahmten Fassung muss auch gerahmt
    # vorkommen. Verglichen wird zeilenweise ohne Einrückung, weil der Kasten
    # zwei Ebenen tiefer schachtelt.
    for zeile in (z.strip() for z in ohne.splitlines()):
        if not zeile or zeile.startswith("<table") or zeile.startswith("</table"):
            continue
        assert zeile in (z.strip() for z in mit.splitlines()), \
            f"Zeile ging beim Einrahmen verloren:\n{zeile}"
    assert "border:1px solid #e2e8f0" in mit


def test_zweispalter_ueberlebt_das_einrahmen():
    """Der häufigste Fall einer gewachsenen Signatur: Logo links, Daten rechts."""
    zwei = {"type": "two_col", "divider": True, "gap": 12,
            "left": [{"type": "logo", "url": "https://x/y.png", "width": 100}],
            "right": [{"type": "name_field", "field": "displayName"}]}
    mit = _html([{"type": "box", "border_width": 2, "border_color": "#2563EB",
                  "padding": 10, "radius": 0, "width": 0, "children": [zwei]}])
    assert "y.png" in mit and "displayName" in mit
    assert "border:2px solid #2563EB" in mit


def test_verschachtelter_kasten_bricht_nicht():
    """Wer zweimal einrahmt, bekommt Kasten in Kasten. Muss der Renderer
    aushalten — die Nachfrage im Editor ist eine Bequemlichkeit, keine Sperre."""
    innen = {"type": "box", "border_width": 1, "padding": 8, "radius": 0,
             "width": 0, "children": [{"type": "freetext", "html": "Inhalt"}]}
    html = _html([{"type": "box", "border_width": 3, "padding": 12, "radius": 0,
                   "width": 0, "children": [innen]}])
    assert html.count("border:") >= 2
    assert "Inhalt" in html


# ── Zweispalter dürfen das Layout ringsum nicht verbiegen ────────────────────

def _haupttabelle_zellen(html: str) -> list[int]:
    """Zellen je Zeile der äußersten Tabelle."""
    import template_parser as tp
    wurzel = tp._baum(html)
    tab = [k for k in wurzel.kinder if k.tag == "table"][0]
    return [len([z for z in r.kinder if z.tag == "td"])
            for r in tab.kinder if r.tag == "tr"]


def test_zweispalter_liegt_in_eigener_tabelle():
    """In HTML teilen sich alle Zeilen einer Tabelle die Spaltenbreiten.

    Stünden die beiden Spalten direkt in der Haupttabelle, würde eine
    einspaltige Zeile darüber nur die ERSTE Spalte belegen — bei einem breiten
    Logo bräche die Grußformel mitten im Satz um. Und ein zweiter Zweispalter
    erbte die Spaltenbreiten des ersten: ein kleines Symbol mit Text daneben
    bekäme den Einzug des grossen Logos.
    """
    html = _html([
        {"type": "greeting", "text": "Freundliche Grüße / Kind regards"},
        {"type": "two_col", "left": [{"type": "logo", "url": "l.png", "width": 116}],
         "right": [{"type": "field", "field": "jobTitle"}]},
        {"type": "two_col", "left": [{"type": "logo", "url": "k.png", "width": 20}],
         "right": [{"type": "booking_link", "label": "Termin buchen"}]},
    ])
    zellen = _haupttabelle_zellen(html)
    assert zellen == [1, 1, 1], (
        f"Die Haupttabelle hat Zeilen mit mehreren Zellen ({zellen}) — "
        f"dadurch teilen sich alle Zeilen die Spaltenbreiten.")


def test_zweispalter_behaelt_seine_spaltenangaben():
    """Gegenprobe: Die Angaben müssen erhalten bleiben, nur eine Ebene tiefer."""
    html = _html([{"type": "two_col", "gap": 6, "divider": True,
                   "left": [{"type": "logo", "url": "x.png", "width": 20}],
                   "right": [{"type": "freetext", "html": "Text"}]}])
    assert "padding-right:6px" in html and "padding-left:6px" in html
    assert "border-left:1px solid" in html
    assert "Text" in html and "x.png" in html


# ── Freitext (formatiert) vs. HTML-Code ──────────────────────────────────────
#
# Zwei Bausteine für zwei Absichten: `text` für Text, den man tippt und über
# Felder auszeichnet; `freetext` für fertiges HTML. Der Unterschied ist die
# Maskierung — und der ist wesentlich: Wer im Textbaustein `<b>` schreibt,
# meint das Zeichen, nicht den Befehl.

def test_text_wird_maskiert():
    html = _html([{"type": "text", "text": "Preis <b>wichtig</b> & mehr"}])
    assert "&lt;b&gt;" in html, html
    assert "<b>wichtig</b>" not in html
    assert "&amp;" in html


def test_html_code_bleibt_roh():
    """Die Gegenprobe — sonst wären beide Bausteine dasselbe."""
    html = _html([{"type": "freetext", "html": "Preis <b>wichtig</b>"}])
    assert "<b>wichtig</b>" in html


def test_zeilenumbrueche_bleiben_erhalten():
    """Wer in ein mehrzeiliges Feld schreibt, erwartet mehrere Zeilen."""
    html = _html([{"type": "text", "text": "Zeile eins\nZeile zwei\nZeile drei"}])
    assert html.count("<br>") == 2, html
    assert "Zeile eins<br>Zeile zwei<br>Zeile drei" in html


def test_auszeichnungen_greifen():
    html = _html([{"type": "text", "text": "x", "bold": True, "italic": True,
                   "underline": True, "size": "9pt", "align": "center",
                   "color": "muted"}])
    for erwartet in ("font-weight:bold", "font-style:italic",
                     "text-decoration:underline", "font-size:9pt",
                     "text-align:center", "color:#6b7280"):
        assert erwartet in html, f"{erwartet} fehlt:\n{html}"


def test_ausrichtung_links_erzeugt_keine_regel():
    """Links ist die Vorgabe — eine überflüssige Regel bläht jede Signatur auf."""
    html = _html([{"type": "text", "text": "x", "align": ""}])
    assert "text-align" not in html
    html2 = _html([{"type": "text", "text": "x", "align": "left"}])
    assert "text-align" not in html2


def test_leerer_text_erzeugt_keine_zeile():
    """Sonst steht eine leere Zeile in jeder Signatur, die niemand sieht und
    niemand erklären kann."""
    assert _html([{"type": "text", "text": "   "}]).count("<tr>") == 0


def test_textfassung_uebernimmt_die_zeilen():
    txt = tb.render_txt({"version": 1, "blocks": [
        {"type": "text", "text": "Zeile eins\nZeile zwei", "bold": True}]})
    assert "Zeile eins" in txt and "Zeile zwei" in txt
    assert "<" not in txt


def test_schriftart_kann_kein_attribut_sprengen():
    """Ein Anführungszeichen beendet das style-Attribut, ein Semikolon hängt
    weitere CSS-Regeln an. Beides muss die Schriftart verlieren.

    Die erste Fassung dieser Prüfung enthielt `or True` und bewies nichts —
    bei der Gegenprobe (Schriftart ungeprüft übernehmen) blieb sie stumm.
    """
    html = _html([{"type": "text", "text": "x",
                   "font": 'Arial";color:red;background:url(x)'}])
    # Das GANZE Tag ansehen, nicht am Anführungszeichen splitten: Ein Test, der
    # selbst am `"` trennt, sieht die Sprengung nicht — er liest brav den Teil
    # davor und ist zufrieden. Genau daran blieben die ersten beiden Fassungen
    # dieser Prüfung stumm.
    tag = re.search(r"<td[^>]*>", html).group(0)
    assert tag.count('"') == 2, (
        f"Attribut gesprengt — das Tag trägt mehr als ein Attributpaar: {tag!r}")
    stil = tag.split('style="')[1].rstrip('">')
    # Aus der Fracht darf höchstens eine unsinnige Schriftbezeichnung werden,
    # kein `color` und kein `background`.
    regeln = [t.split(":", 1)[0] for t in stil.split(";") if t]
    assert regeln == ["padding", "font-family"], (
        f"zusätzliche CSS-Regel eingeschleust: {regeln}")
    # Und die harmlose Schriftart bleibt.
    sauber = _html([{"type": "text", "text": "x", "font": "Georgia, serif"}])
    assert "font-family:Georgia, serif" in sauber, sauber
