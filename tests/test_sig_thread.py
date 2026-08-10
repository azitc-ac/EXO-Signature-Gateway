"""Erkennung „in dieser Kette schon signiert" über References/In-Reply-To.

WARUM DIESE TESTS ANDERS GEBAUT SIND
------------------------------------
Der bestehende `app/self_test.py` prüft dieselbe Fachlichkeit an MIME, das er
sich selbst zusammensetzt — mitsamt den Gateway-Markern. Er war 12/12 grün,
während die Erkennung im Betrieb zwölf Tage lang kein einziges Mal griff: Die
Marker, die er einbaut, kommen in echter Post nie an.

Diese Datei prüft deshalb gegen die STRUKTUR, die tatsächlich ankommt — eine
von Outlooks Word-Editor umgeschriebene Antwort, in der es keinen Marker mehr
gibt, dafür aber `References`. Der Aufbau unten ist einer echten Mail
nachgebildet (Anfrage → Antwort → Antwort, drei Kennungen in `References`).

ANLASS (10.08.2026)
-------------------
Eine Antwort an einen Kunden trug die vollständige Signatur ein zweites Mal,
obwohl sie in der zitierten Kette bereits stand. Ursache war nicht das
Signaturbanner, sondern: Alle drei Erkennungsmerkmale saßen im HTML-Körper
(`<!-- exo-sig-start -->`, `id="exo-sig-s"`, `class="exo-gateway-sig"`), und
Outlook verwirft beim Zitieren Kommentare, IDs und Klassen. An 400 echten
Mails nachgemessen: bei internen Absendern erhalten, bei keinem einzigen
externen.
"""
from __future__ import annotations

import email
import email.message

import pytest

import sig_thread


# Eine echte Antwortkette: Anfrage (Kunde) → Antwort (wir) → Antwort (Kunde).
# Die Kennungen sind Exchange-Format, wie sie tatsächlich vorkommen.
MID_KUNDE_1 = "<FRVP281MB58326A8B265B0DEDA6CAE27C80DE2@FRVP281MB5832.DEUP281.PROD.OUTLOOK.COM>"
MID_WIR_1   = "<AMBPR05MB12050195DF9DA9E44CA168C1AA1DE2@AMBPR05MB12050.eurprd05.prod.outlook.com>"
MID_KUNDE_2 = "<FRVP281MB58321704792433EB85B8258080DE2@FRVP281MB5832.DEUP281.PROD.OUTLOOK.COM>"

# So sieht die zitierte Signatur an, nachdem Outlooks Word-Editor sie
# umgeschrieben hat: kein Kommentar, keine Klasse, keine Kennung — nur noch
# MsoNormal-Absätze mit Zentimeter-Einheiten.
OUTLOOK_ZITAT = (
    '<html><body><p class="MsoNormal">Danke für die Rückmeldung.</p>'
    '<div><div style="border:none; border-top:solid #E1E1E1 1.0pt; padding:3.0pt 0cm 0cm 0cm">'
    '<p class="MsoNormal"><b>Von:</b> Alexander Zarenko</p></div>'
    '<p class="MsoNormal"><span style="font-family:Roboto">Alexander Zarenko - IT Consulting</span></p>'
    '<p class="MsoNormal"><span style="font-family:Roboto">Triebelsstr. 4, 52066 Aachen</span></p>'
    '</div></body></html>'
)


def _antwort(refs: list[str], in_reply_to: str | None = None) -> email.message.Message:
    m = email.message.Message()
    m["Subject"] = "AW: Anfrage zu Unterstützung"
    m["From"] = "kunde@example.org"
    m["Message-ID"] = "<neue-antwort@example.org>"
    if refs:
        m["References"] = " ".join(refs)
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    m.set_payload(OUTLOOK_ZITAT)
    m.set_type("text/html")
    return m


@pytest.fixture(autouse=True)
def _eigene_db(tmp_path, monkeypatch):
    """Jeder Test bekommt eine eigene Datenbank — niemals die echte anfassen."""
    monkeypatch.setattr(sig_thread, "DB_PATH", tmp_path / "sig_thread.db")


# ── Kern: erkennt die Kette, obwohl im Körper kein Marker mehr steckt ────────

def test_erkennt_kette_obwohl_im_html_kein_marker_mehr_steht():
    """DER Fall, an dem die alte Erkennung gescheitert ist."""
    assert "exo-sig" not in OUTLOOK_ZITAT, "Vorlage soll bewusst markerfrei sein"
    sig_thread.merken("alexander@zarenko.net", MID_WIR_1)
    msg = _antwort([MID_KUNDE_1, MID_WIR_1], in_reply_to=MID_WIR_1)
    assert sig_thread.kennt("alexander@zarenko.net", sig_thread.kennungen(msg))


def test_ohne_vorherige_signatur_kein_treffer():
    """Erste eigene Mail im Thread: volle Signatur, kein Unterdrücken."""
    msg = _antwort([MID_KUNDE_1], in_reply_to=MID_KUNDE_1)
    assert not sig_thread.kennt("alexander@zarenko.net", sig_thread.kennungen(msg))


def test_in_reply_to_allein_genuegt():
    """Manche Programme setzen nur In-Reply-To, kein References."""
    sig_thread.merken("alexander@zarenko.net", MID_WIR_1)
    msg = _antwort([], in_reply_to=MID_WIR_1)
    assert sig_thread.kennt("alexander@zarenko.net", sig_thread.kennungen(msg))


def test_lange_kette_findet_treffer_in_der_mitte():
    sig_thread.merken("alexander@zarenko.net", MID_WIR_1)
    msg = _antwort([MID_KUNDE_1, MID_WIR_1, MID_KUNDE_2], in_reply_to=MID_KUNDE_2)
    assert sig_thread.kennt("alexander@zarenko.net", sig_thread.kennungen(msg))


# ── Abgrenzung: keine falschen Treffer ──────────────────────────────────────

def test_anderes_postfach_wird_nicht_unterdrueckt():
    """Sonst würde eine Kette von Postfach A die Signatur von B abschalten."""
    sig_thread.merken("alexander@zarenko.net", MID_WIR_1)
    msg = _antwort([MID_WIR_1], in_reply_to=MID_WIR_1)
    assert not sig_thread.kennt("karen@zarenko.net", sig_thread.kennungen(msg))


def test_bookings_fall_erzeugt_keinen_fehlalarm():
    """Der Fehler, an dem die frühere Textheuristik gescheitert ist.

    Microsoft Bookings verschickt Benachrichtigungen UNTER der Adresse des
    Veranstalters — im Zitat steht dann „Von: alexander@…", obwohl er in
    diesem Thread nie geschrieben hat. Der `Von:`-Zeilenabgleich hielt das für
    einen eigenen Beitrag und unterdrückte die Signatur.

    Ein Identitätsvergleich kann diesen Fehler nicht machen: Die
    Benachrichtigung verweist auf keine Kennung, die dieses Gateway signiert
    hat — auch wenn der eigene Name im Text steht.
    """
    msg = _antwort(["<bookings-notification-xyz@microsoft.com>"])
    msg.set_payload(
        '<html><body><p class="MsoNormal"><b>Von:</b> alexander@zarenko.net</p>'
        '<p class="MsoNormal">Alexander Zarenko - IT Consulting</p></body></html>')
    assert not sig_thread.kennt("alexander@zarenko.net", sig_thread.kennungen(msg))


def test_ohne_references_kein_treffer():
    """Neue Mail mit kopiertem Betreff ist keine Antwort — volle Signatur."""
    msg = _antwort([])
    assert sig_thread.kennungen(msg) == []
    assert not sig_thread.kennt("alexander@zarenko.net", sig_thread.kennungen(msg))


def test_eigene_message_id_zaehlt_nicht_als_bezug():
    """Sonst gälte jede Mail als Antwort auf sich selbst."""
    msg = _antwort([])
    msg.replace_header("Message-ID", MID_WIR_1)
    sig_thread.merken("alexander@zarenko.net", MID_WIR_1)
    assert MID_WIR_1 not in sig_thread.kennungen(msg)
    assert not sig_thread.kennt("alexander@zarenko.net", sig_thread.kennungen(msg))


# ── Speicherform ────────────────────────────────────────────────────────────

def test_kennungen_liegen_nicht_im_klartext_auf_der_platte():
    """Gespeichert wird ein Hash — es sollen keine Nachrichtenkennungen aus
    Kundenkorrespondenz auf der Platte liegen."""
    sig_thread.merken("alexander@zarenko.net", MID_WIR_1)
    roh = sig_thread.DB_PATH.read_bytes()
    assert MID_WIR_1.encode() not in roh
    assert b"alexander@zarenko.net" not in roh


def test_doppeltes_merken_ist_harmlos():
    """Bifurkierte Mehr-Empfänger-Mails laufen mehrfach durch dieselbe Stelle."""
    for _ in range(3):
        sig_thread.merken("alexander@zarenko.net", MID_WIR_1)
    msg = _antwort([MID_WIR_1])
    assert sig_thread.kennt("alexander@zarenko.net", sig_thread.kennungen(msg))


def test_leere_eingaben_werfen_nicht():
    sig_thread.merken("", MID_WIR_1)
    sig_thread.merken("alexander@zarenko.net", "")
    assert not sig_thread.kennt("", [MID_WIR_1])
    assert not sig_thread.kennt("alexander@zarenko.net", [])
    assert not sig_thread.kennt("alexander@zarenko.net", None)


def test_aufraeumen_verwirft_nur_alte_monate():
    import sqlite3
    sig_thread.merken("alexander@zarenko.net", MID_WIR_1)      # legt aktuellen Monat an
    with sqlite3.connect(str(sig_thread.DB_PATH)) as c:
        for alt in ("sig_202001", "sig_202002", "sig_202003"):
            c.execute(f"CREATE TABLE IF NOT EXISTS {alt} (h BLOB PRIMARY KEY) WITHOUT ROWID")
    weg = sig_thread.aufraeumen()
    assert weg >= 1, "alte Monatstabellen wurden nicht verworfen"
    # Der aktuelle Monat muss den Lauf überleben — sonst wäre jede Kette sofort vergessen.
    msg = _antwort([MID_WIR_1])
    assert sig_thread.kennt("alexander@zarenko.net", sig_thread.kennungen(msg))
