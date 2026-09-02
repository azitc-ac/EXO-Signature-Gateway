"""Trennung zwischen Gateway-Signatur und zitiertem Text.

WORUM ES GEHT
-------------
Die Signatur wird vor den zitierten Text gesetzt. Ob dabei eine sichtbare
Trennung entsteht, hängt davon ab, WIE der schreibende Client seinen Strich
setzt — und das ist von Client zu Client verschieden:

* **Outlook Desktop** hängt ihn als `border-top` an das Zitat-Div SELBST.
  Einfügen davor schiebt den Strich mit nach unten. Nichts zu tun.
* **Outlook für iOS / OWA** setzen ein eigenständiges `<hr>` DAVOR. Wer vor dem
  Zitat einfügt, landet dahinter — der Strich trennt dann den eigenen Text von
  der Signatur, unten klafft nichts.
* **Apple Mail / Gmail / Thunderbird** setzen gar keinen Strich.

⚠️ Die Unterscheidung ist NICHT „mobil gegen Desktop". Die frühere Annahme
„mobile Clients setzen keine Linie" stammte aus einer Messung an Apple Mail und
war für Outlook für iOS falsch: Das setzt sehr wohl eine, sie wurde nur
überschrieben.

WOHER DIE TESTDATEN STAMMEN
---------------------------
Aus echtem Verkehr, nicht ausgedacht: Die Gerüste unten sind den gesendeten
Nachrichten eines produktiven Postfachs entnommen (14.08.2026), Inhalte durch
Platzhalter ersetzt. Ein Test, der sich seine Eingabe selbst ausdenkt, prüft die
eigene Annahme über den fremden Client — genau daran ist die Sache schon einmal
vorbeigelaufen.

Von Outlook für iOS liegen ZWEI Fassungen vor, beide echt: mit
`mail-editor-reference-message-container` und ohne (dort steht der blosse
`<hr>`). Beide müssen greifen.
"""
import re

import pytest

import mail_processor as mp

SIG = '<table><tr><td>Signatur</td></tr></table>'


def _einfuegen(html: str) -> str:
    return mp._append_html_sig(html, SIG)


def _pos(html: str, muster: str) -> int:
    t = re.search(muster, html, re.IGNORECASE)
    assert t, f"Muster nicht gefunden: {muster}"
    return t.start()


# ── Echte Gerüste ────────────────────────────────────────────────────────────

# Outlook für iOS, Fassung OHNE Container (Antwort vom 14.08.2026):
# leeres Signatur-Div, dann der Strich, dann der Zitatkopf.
IOS_OHNE_CONTAINER = (
    '<html><body><div dir="ltr">Antworttext</div>'
    '<div id="ms-outlook-mobile-signature" dir="ltr" style="font-size:12pt"></div>'
    '<hr tabindex="-1" style="display:inline-block; width:98%">'
    '<div id="divRplyFwdMsg" dir="ltr"><b>Von:</b> Vorname Nachname'
    '<br><b>Gesendet:</b> Freitag, 14. August 2026</div>'
    '<div>zitierter Text</div></body></html>'
)

# Outlook für iOS, Fassung MIT Container (Antwort vom 10.08.2026).
IOS_MIT_CONTAINER = (
    '<html><body><div dir="ltr">Antworttext</div>'
    '<div id="ms-outlook-mobile-signature" dir="ltr" style="font-size:12pt"></div>'
    '<div id="mail-editor-reference-message-container" class="ms-outlook-mobile-reference-message">'
    '<div id="mail-editor-reference-message-container">'
    '<hr style="display:inline-block; width:98%">'
    '<div id="divRplyFwdMsg" dir="ltr"><b>Von:</b> Vorname Nachname</div>'
    '<div>zitierter Text</div></div></div></body></html>'
)

# Outlook Desktop: der Strich hängt am Zitat-Div selbst.
OUTLOOK_DESKTOP = (
    '<html><body><div class="WordSection1"><p class="MsoNormal">Antworttext</p>'
    '<p class="MsoNormal">&nbsp;</p>'
    '<div><div style="border:none; border-top:solid #E1E1E1 1.0pt; padding:3.0pt 0cm 0cm 0cm">'
    '<p class="MsoNormal"><b>Von:</b> Vorname Nachname</p></div>'
    '<p>zitierter Text</p></div></div></body></html>'
)

# Apple Mail: kein Strich, nur das Zitat.
APPLE_MAIL = (
    '<html><body><div>Antworttext</div>'
    '<blockquote type="cite">Am 12.08.2026 um 23:18 schrieb Vorname Nachname:<br><br></blockquote>'
    '<blockquote type="cite"><div dir="ltr">zitierter Text</div></blockquote>'
    '</body></html>'
)

# Erstmail ohne jedes Zitat.
ERSTMAIL = '<html><body><div>Ein neuer Text ohne Zitat.</div></body></html>'


# ── Outlook für iOS: den vorhandenen Strich benutzen ─────────────────────────

@pytest.mark.parametrize("gerueest,name", [
    (IOS_OHNE_CONTAINER, "ohne Container"),
    (IOS_MIT_CONTAINER, "mit Container"),
])
def test_ios_signatur_steht_vor_dem_strich_des_clients(gerueest, name):
    """Der Strich des Clients muss UNTER der Signatur landen, nicht darüber.

    Genau das ging schief: Die Signatur wurde hinter dem `<hr>` eingefügt, der
    Strich trennte dadurch Text und Signatur, und zwischen Signatur und Zitat
    blieb nichts.
    """
    aus = _einfuegen(gerueest)
    assert _pos(aus, r'class="exo-gateway-sig"') < _pos(aus, r'<hr\b'), (
        f"[{name}] Signatur steht hinter dem Strich — die Trennung zum Zitat fehlt")
    assert _pos(aus, r'<hr\b') < _pos(aus, r'id="divRplyFwdMsg"'), (
        f"[{name}] Der Strich steht nicht mehr vor dem Zitat")


@pytest.mark.parametrize("gerueest", [IOS_OHNE_CONTAINER, IOS_MIT_CONTAINER])
def test_ios_bekommt_keine_zweite_linie(gerueest):
    """Ein vorhandener Strich wird benutzt, nicht ergänzt — sonst zwei Linien."""
    assert _einfuegen(gerueest).count("<hr") == gerueest.count("<hr")


# ── Outlook Desktop: unverändert lassen ──────────────────────────────────────

def test_outlook_desktop_bekommt_keine_linie():
    """Hier trägt das Zitat-Div den Strich selbst — eine eigene wäre die zweite."""
    aus = _einfuegen(OUTLOOK_DESKTOP)
    assert "<hr" not in aus, "Outlook Desktop hat bereits einen Strich am Zitat-Div"
    assert _pos(aus, r'class="exo-gateway-sig"') < _pos(aus, r'border-top:solid'), \
        "Signatur muss vor dem Trenner-Div stehen, damit der Strich sie vom Zitat trennt"


# ── Apple Mail: eigene Linie setzen ──────────────────────────────────────────

def test_apple_mail_bekommt_eine_linie():
    """Apple Mail bringt keinen Strich mit — hier wird selbst einer gesetzt."""
    aus = _einfuegen(APPLE_MAIL)
    assert aus.count("<hr") == 1, "genau eine Trennlinie erwartet"
    assert _pos(aus, r'class="exo-gateway-sig"') < _pos(aus, r'<hr\b') < _pos(aus, r'<blockquote'), \
        "Reihenfolge muss Signatur → Linie → Zitat sein"


def test_die_linie_sieht_aus_wie_die_von_outlook():
    """Einheitliche Form: dasselbe Element, das Outlook für iOS/OWA setzt.

    `tabindex` bleibt weg — es steuert die Sprungreihenfolge im Editorfenster
    und hat in einer versendeten Nachricht keine Bedeutung.
    """
    assert mp._SIG_TRENNER == '<hr style="display:inline-block; width:98%">'
    assert "tabindex" not in mp._SIG_TRENNER
    assert mp._SIG_TRENNER in _einfuegen(APPLE_MAIL)


# ── Erstmail: nichts zu trennen ──────────────────────────────────────────────

def test_erstmail_ohne_zitat_bekommt_keine_linie():
    """Ohne zitierten Text gäbe es nichts, wovon zu trennen wäre — die Linie
    stünde einsam unter der Signatur."""
    assert "<hr" not in _einfuegen(ERSTMAIL)


# ── Idempotenz ───────────────────────────────────────────────────────────────

# ── Sicherheit des Rückziehers ───────────────────────────────────────────────

def test_rueckzieher_ueberspringt_niemals_sichtbaren_text():
    """Die Signatur darf nie über geschriebenen Text hinweg nach vorn wandern.

    Der Rückzieher bewegt sie nur, wenn zwischen Strich und Zitat NICHTS steht
    ausser Leerraum, `<br>` und leeren Divs. Steht dort Text, gehört der Strich
    zum Text des Absenders und nicht zum Zitat — dann bleibt die Signatur, wo
    sie ist, und bekommt eine eigene Linie.
    """
    mit_text = (
        '<html><body><div>Antworttext</div>'
        '<hr style="display:inline-block; width:98%">'
        '<div>Noch ein Nachsatz nach der Linie.</div>'
        '<div id="divRplyFwdMsg"><b>Von:</b> Vorname Nachname</div></body></html>'
    )
    aus = _einfuegen(mit_text)
    assert _pos(aus, r'<hr\b') < _pos(aus, r'Noch ein Nachsatz'), \
        "Der Strich des Absenders wurde verschoben"
    assert _pos(aus, r'Noch ein Nachsatz') < _pos(aus, r'class="exo-gateway-sig"'), \
        "Die Signatur ist über geschriebenen Text hinweg nach vorn gerutscht"


def test_rueckzieher_geht_nur_nach_vorn():
    """Er kann die Signatur nie TIEFER in den zitierten Text schieben.

    Das ist die eigentliche Zusicherung: Der Rückzieher wählt ausschliesslich
    Positionen vor der ursprünglichen Einfügestelle. Schlimmstenfalls steht die
    Signatur damit etwas früher — nie mitten im Zitat.
    """
    for gerueest in (IOS_OHNE_CONTAINER, IOS_MIT_CONTAINER, OUTLOOK_DESKTOP, APPLE_MAIL):
        anker = min(m.start() for m in
                    [re.search(r'<div\b[^>]*id="divRplyFwdMsg"', gerueest, re.I),
                     re.search(r'<blockquote\b', gerueest, re.I),
                     re.search(r'border-top:solid', gerueest, re.I)] if m)
        neu, _, _ = mp._trennstelle(gerueest, anker)
        assert neu <= anker, "Die Einfügestelle wurde nach hinten verschoben"


def test_die_linie_liegt_innerhalb_der_marker():
    """Sonst bliebe sie beim nächsten Durchlauf stehen und vermehrte sich.

    `_strip_client_sig_divs()` entfernt alles zwischen `<!-- exo-sig-start -->`
    und `<!-- exo-sig-end -->`. Eine Linie ausserhalb überlebte das — und jede
    weitere Verarbeitung legte eine neue dazu.
    """
    aus = _einfuegen(APPLE_MAIL)
    a = aus.index(mp._SIG_MARKER_START)
    e = aus.index(mp._SIG_MARKER_END)
    assert a < aus.index("<hr") < e, "Die Trennlinie steht ausserhalb der Marker"


def test_zweiter_durchlauf_erzeugt_keine_zweite_linie(monkeypatch):
    """Der vollständige Weg: einfügen, entfernen, wieder einfügen."""
    monkeypatch.setattr(mp.settings_store, "get", lambda k, *a, **kw:
                        None if k == "STRIP_CLIENT_SIGS_MOBILE" else False)
    einmal = _einfuegen(APPLE_MAIL)
    zurueck = mp._strip_client_sig_divs(einmal, SIG)
    assert "<hr" not in zurueck, "Die eigene Linie wurde beim Entfernen nicht mitgenommen"
    assert _einfuegen(zurueck).count("<hr") == 1
