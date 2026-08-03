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
    """Nur der Text, den ein Leser sieht.

    Formatvorlagen zählen NICHT mit: Ihr Inhalt ist CSS, und genau der wird von
    den Korrekturen absichtlich verändert. Ohne diesen Ausschluss meldete die
    Prüfung auf Inhaltsverlust jede gewollte Änderung als Schaden — sie war
    zuerst genau so gebaut und schlug prompt an.
    """
    ohne = re.sub(r"<(style|script)\b[^>]*>.*?</\1>", "", html,
                  flags=re.S | re.I)
    return re.sub(r"<[^>]+>", "", ohne)


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


# ── Sichtbarkeit: eine Protokollzeile liest niemand ──────────────────────────

def test_unbekannte_form_wird_gezaehlt():
    """Die Warnung muss den Tagesbericht erreichen, nicht nur das Protokoll.

    Der Fix lief von Juli bis August 2026 wirkungslos mit. Eine Zeile im
    Protokoll hätte daran nichts geändert — sie liest niemand. Gezählt
    erscheint der Fall im Tagesbericht beim Betreiber.
    """
    import stats
    vorher = stats.get_daily().get("lexware_unbekannt", 0)
    roh = (f'<html><body><div style="text-align:center">'
           f'{MARKER}Inhalt</td></div></body></html>')
    mp._fix_lexware_centering(roh)
    assert stats.get_daily().get("lexware_unbekannt", 0) == vorher + 1, (
        "unbekannte Zentrierung wurde nicht gezählt — sie bliebe im "
        "Tagesbericht unsichtbar")


def test_bekannte_form_zaehlt_nicht():
    """Sonst stünde die Zahl bei jeder Rechnung im Bericht und würde
    bedeutungslos."""
    import stats
    vorher = stats.get_daily().get("lexware_unbekannt", 0)
    roh = f'<html><body><table align="center"><tr>{MARKER}x</td></tr></table></body></html>'
    mp._fix_lexware_centering(roh)
    assert stats.get_daily().get("lexware_unbekannt", 0) == vorher


def test_tagesbericht_zeigt_die_zeile_nur_bei_bedarf():
    """Eine Dauerzeile mit 0 würde überlesen — und darauf kommt es hier an."""
    import inspect
    import notification
    quelle = inspect.getsource(notification.send_daily_report)
    assert 'if dval("lexware_unbekannt")' in quelle, (
        "die Zeile erscheint unbedingt — dann fällt sie nicht mehr auf")
    assert "Belege mit unbekanntem Aufbau" in quelle


# ── Zeilenhöhe: die Newsletter-Vorlage ist für Belege zu luftig ──────────────
#
# Lexware verschickt Belege über eine Newsletter-Vorlage (erkennbar an den
# `mcn…`-Klassen — Mailchimps Namensschema). Deren 150 % Zeilenhöhe ist für
# einen Rundbrief richtig; zusammen mit den Doppelumbrüchen zwischen den
# Absätzen ergibt sie in einer Rechnung drei volle Zeilenhöhen Abstand.
# In Lexware lässt sich das nicht ändern.

def test_zu_grosse_zeilenhoehe_wird_gesenkt():
    roh = (f'<html><head><style>#templateBody .mcnTextContent'
           f'{{font-size:16px;line-height:150%;text-align:left}}</style>'
           f'<body><table><tr>{MARKER}Text</td></tr></table></body></html>')
    neu = mp._fix_lexware_zeilenhoehe(roh)
    assert "line-height:130%" in neu, neu
    assert "line-height:150%" not in neu


def test_briefartige_zeilenhoehe_bleibt():
    """Unterhalb der Grenze ist bereits Briefmaß — daran wird nicht gedreht."""
    for wert in ("120%", "125%", "130%", "139%"):
        roh = (f'<html><style>p{{line-height:{wert}}}</style>'
               f'<body>{MARKER}x</td></body></html>')
        assert f"line-height:{wert}" in mp._fix_lexware_zeilenhoehe(roh), wert


def test_absolute_zeilenhoehen_bleiben():
    """Nur PROZENTwerte werden gesenkt.

    Absolute Angaben gehören zu Trennlinien und Abstandszellen; dort wäre eine
    Änderung ein Layoutfehler. Geprüft wird mit einem Wert ÜBER der Grenze —
    mit `12px` würde die Grenze schützen und der Test bewiese nichts über den
    Ausdruck selbst. Genau so war er zuerst gebaut und blieb bei der
    Gegenprobe stumm.
    """
    for wert in ("150px", "200px", "12px", "1.5em"):
        roh = (f'<html><style>.mcnDivider{{line-height:{wert}}}</style>'
               f'<body>{MARKER}x</td></body></html>')
        assert f"line-height:{wert}" in mp._fix_lexware_zeilenhoehe(roh), wert


def test_ohne_marker_keine_aenderung():
    roh = '<html><style>p{line-height:150%}</style><body>Newsletter</body></html>'
    assert mp._fix_lexware_zeilenhoehe(roh) == roh


def test_zeilenhoehe_frisst_keinen_inhalt():
    """Derselbe Vorbehalt wie bei der Ausrichtung — ein früherer Ausdruck an
    dieser Datei lief über das Attributende hinaus."""
    text = "Betrag: 1.234,56 &euro;; Frist: 14 Tage; a &lt; b &gt; c"
    roh = (f'<html><style>p{{line-height:150%}}</style><body><table><tr>'
           f'{MARKER}{text}</td></tr></table></body></html>')
    neu = mp._fix_lexware_zeilenhoehe(roh)
    assert _sichtbar(neu) == _sichtbar(roh), (
        f"Text verändert:\n{_sichtbar(roh)!r}\n{_sichtbar(neu)!r}")


def test_unbekannt_grosse_zeilenhoehe_wird_gemeldet(caplog):
    """Wenn eine Form durchrutscht, muss sie im Tagesbericht landen."""
    import logging
    import stats
    vorher = stats.get_daily().get("lexware_unbekannt", 0)
    # Eine Schreibweise, die der Ersetzer nicht trifft (Dezimalfaktor statt %).
    roh = (f'<html><style>p{{line-height : 180 %}}</style>'
           f'<body>{MARKER}x</td></body></html>')
    with caplog.at_level(logging.WARNING):
        neu = mp._fix_lexware_zeilenhoehe(roh)
    if "line-height : 180 %" in neu:          # nicht ersetzt -> muss melden
        assert stats.get_daily().get("lexware_unbekannt", 0) > vorher


def test_ganze_kette_senkt_die_zeilenhoehe(monkeypatch):
    """Über _fix_lexware_format — sonst wäre die Funktion gebaut, aber nie
    aufgerufen."""
    import settings_store
    monkeypatch.setattr(settings_store, "get",
                        lambda k, d=None: True if k == "LEXWARE_FIX_FORMAT" else d)
    roh = (f'<html><head><style>#templateBody .mcnTextContent'
           f'{{line-height:150%}}</style><body><table align="center"><tr>'
           f'{MARKER}Sehr geehrte Damen und Herren,<br><br>Text</td>'
           f'</tr></table></body></html>')
    neu = mp._fix_lexware_format(roh)
    assert "line-height:130%" in neu, "Zeilenhöhe nicht gesenkt"
    assert not _zentrierung_uebrig(neu), "Zentrierung nicht behoben"
    assert "Sehr geehrte Damen und Herren," in neu
