"""Lexware-Rechnungen müssen linksbündig hinausgehen.

WARUM DIESE PRÜFUNG SPÄT KOMMT
Der Fix gegen die Zentrierung existiert seit v1.6.4 (Juli 2026) — aber ohne
Test. Als Lexware seine Vorlage änderte und die Zentrierung von
`<div align=center>` auf `<table align="center">` wanderte, lief er wirkungslos
mit: Die Rechnungen gingen weiter zentriert hinaus, während der Fix als
erledigt galt. Aufgefallen ist es erst dem Nutzer, ein zweites Mal.

Deshalb wird hier JEDE bekannte Variante geprüft — und zusätzlich, dass nach
dem Fix keine Zentrierung übrigbleibt. Letzteres fängt auch eine Variante, an
die niemand gedacht hat.

KEIN INHALT DARF VERLORENGEHEN
Ein früherer Fix an derselben Funktion fraß Mailinhalt: Sein Ausdruck lief über
das Attributende hinaus bis zum nächsten `;` — dem einer HTML-Entity im
Fließtext. Die Prüfungen unten enthalten deshalb ausdrücklich Entities,
Semikolons und spitze Klammern im sichtbaren Text.
"""
import re

import mail_processor as mp

MARKER = '<td id="templateBody">'


def _zentrierung_uebrig(html: str) -> list[str]:
    """Jedes Merkmal, das den Text zentriert darstellen würde."""
    treffer = []
    if re.search(r'<\w+\b[^>]*\balign\s*=\s*["\']?center', html, re.I):
        treffer.append("align=center")
    if re.search(r"<center\b", html, re.I):
        treffer.append("<center>")
    if re.search(r"text-align\s*:\s*center", html, re.I):
        treffer.append("text-align:center")
    return treffer


def _sichtbar(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html)


# ── Die bekannten Varianten ──────────────────────────────────────────────────

def test_table_align_center_august_2026():
    """Die Fassung, die den Fehler wieder auftreten liess."""
    roh = (f'<html><body><div><table align="center" border="0">'
           f'<tr>{MARKER}Rechnung RE202608-0061</td></tr></table></div></body></html>')
    neu = mp._fix_lexware_centering(roh)
    assert not _zentrierung_uebrig(neu), f"noch zentriert: {_zentrierung_uebrig(neu)}\n{neu}"
    assert 'align="left"' in neu
    assert "RE202608-0061" in neu


def test_div_align_center_altbestand():
    roh = f"<html><body><div align=center>{MARKER}Inhalt</td></div></body></html>"
    neu = mp._fix_lexware_centering(roh)
    assert not _zentrierung_uebrig(neu), _zentrierung_uebrig(neu)
    assert "Inhalt" in neu


def test_center_tag_variante():
    roh = f"<html><body><center>{MARKER}Inhalt</td></center></body></html>"
    neu = mp._fix_lexware_centering(roh)
    assert not _zentrierung_uebrig(neu), _zentrierung_uebrig(neu)
    assert "Inhalt" in neu


def test_mehrere_zentrierungen_gleichzeitig():
    """Vorlagen mischen die Formen — eine allein zu beheben genügt nicht."""
    roh = (f'<html><body><table align="center"><tr><td>'
           f'<div align="center">{MARKER}Text</td></div></td></tr></table></body></html>')
    neu = mp._fix_lexware_centering(roh)
    assert not _zentrierung_uebrig(neu), _zentrierung_uebrig(neu)


# ── Was NICHT angefasst werden darf ──────────────────────────────────────────

def test_ohne_lexware_marker_bleibt_alles():
    """Fremde Mails mit zentrierten Layouttabellen dürfen nicht verändert werden."""
    roh = '<html><body><table align="center"><tr><td>Newsletter</td></tr></table></body></html>'
    assert mp._fix_lexware_centering(roh) == roh


def test_kein_inhalt_geht_verloren():
    """Der Fehler, der schon einmal passiert ist: Ein Ausdruck lief über das
    Attributende hinaus und verschluckte Mailinhalt."""
    text = ('Grüße &amp; Dank; Betrag: 1.234,56 &euro;; '
            'Hinweis: a &lt; b &gt; c; Fällig: 14 Tage')
    roh = (f'<html><body><table align="center"><tr>{MARKER}{text}</td>'
           f'</tr></table></body></html>')
    neu = mp._fix_lexware_centering(roh)
    assert _sichtbar(neu) == _sichtbar(roh), (
        f"sichtbarer Text verändert:\n{_sichtbar(roh)!r}\n{_sichtbar(neu)!r}")


def test_andere_ausrichtungen_bleiben():
    """Nur `center` wird gedreht — `right` ist eine Absicht, keine Panne."""
    roh = f'<html><body><table align="right"><tr>{MARKER}x</td></tr></table></body></html>'
    assert 'align="right"' in mp._fix_lexware_centering(roh)


def test_echte_rechnung_wird_linksbuendig(tmp_path):
    """Gegen eine nachgebaute Rechnung im Aufbau der Fassung vom August 2026.

    Nachgebaut, nicht die echte: Der Bestand gehört nicht ins öffentliche
    Repository. Der Aufbau ist derselbe — <style>-Block im Kopf, ein
    <div> um eine zentrierte Tabelle, Marker in einer Zelle darin.
    """
    roh = (
        '<html><head><style>\n.templateContainer\n\t{width:600px!important}\n'
        '</style></head><body><div>'
        '<table align="center" border="0" cellpadding="0" cellspacing="0" '
        'class="templateContainer"><tbody><tr>'
        f'{MARKER}<p>Sehr geehrte Damen und Herren,</p>'
        '<p>anbei die Rechnung RE202608-0061 über 1.234,56 &euro;.</p></td>'
        '</tr></tbody></table></div></body></html>')
    neu = mp._fix_lexware_centering(roh)
    assert not _zentrierung_uebrig(neu), _zentrierung_uebrig(neu)
    assert _sichtbar(neu) == _sichtbar(roh), "Inhalt verändert"
    assert "RE202608-0061" in neu and "1.234,56" in neu


def test_unbekannte_zentrierung_wird_gemeldet(caplog):
    """Der Wächter gegen den nächsten Vorlagenwechsel.

    Wenn Lexware eine Form wählt, die die Korrektur nicht kennt, soll das im
    Protokoll stehen — statt still weiterzulaufen wie im Juli 2026.
    """
    import logging
    roh = (f'<html><body><div style="text-align:center">'
           f'{MARKER}Inhalt</td></div></body></html>')
    with caplog.at_level(logging.WARNING):
        neu = mp._fix_lexware_centering(roh)
    assert any("neue Vorlagenfassung" in r.message for r in caplog.records), (
        "unbekannte Zentrierung blieb unbemerkt")


def test_bekannte_form_meldet_nicht(caplog):
    """Gegenprobe — sonst warnt es bei jeder Rechnung und wird überlesen."""
    import logging
    roh = (f'<html><body><table align="center"><tr>{MARKER}x</td>'
           f'</tr></table></body></html>')
    with caplog.at_level(logging.WARNING):
        mp._fix_lexware_centering(roh)
    assert not [r for r in caplog.records if "neue Vorlagenfassung" in r.message]
