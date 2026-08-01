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
