"""Private Mails: private Signatur, kein Banner, kein Disclaimer.

Auslöser ist das Kürzel im Betreff (PRIVAT_TRIGGER, Vorgabe „#privat“) oder die
Wahl „Privat“ im Aufgabenbereich des Verfassen-Add-ins (Kopf X-Exo-Sig-Privat).
Beides wirkt nur, wenn PRIVAT_SIG_AKTIV an ist UND das Postfach in
PRIVAT_SIGNATUREN eine Vorlage hat — sonst bleibt alles wie bisher, Banner und
Disclaimer eingeschlossen.

Durch den echten `handle_DATA`, mit der Anlage aus test_zusatz_ohne_signatur.
"""
import asyncio
import email

import pytest

import handler
import policies
from test_zusatz_ohne_signatur import ABS, ADDIN_SIG, _Sitzung, _Umschlag, _mail, anlage  # noqa: F401


def _versand(anlage, content):
    asyncio.run(handler.SignatureHandler().handle_DATA(None, _Sitzung(), _Umschlag(content)))
    daten = next(a for a in anlage["_zugestellt"][-1] if isinstance(a, (bytes, bytearray)))
    msg = email.message_from_bytes(daten)
    teil = next(p for p in msg.walk() if p.get_content_type() == "text/html")
    return msg, teil.get_payload(decode=True).decode()


def _mit_kopf(content: bytes) -> bytes:
    return b"X-Exo-Sig-Privat: 1\r\n" + content


@pytest.fixture
def privat(anlage):
    anlage.update(PRIVAT_SIG_AKTIV=True, PRIVAT_TRIGGER="#privat",
                  PRIVAT_SIGNATUREN={ABS: "Priv"})
    return anlage


def test_kuerzel_setzt_private_signatur_ohne_zusatz(privat):
    msg, html = _versand(privat, _mail(betreff="Grillen am Samstag #privat"))
    assert "[Priv]" in html
    assert "[Sig]" not in html and "[Ban]" not in html and "[Dis]" not in html
    assert msg["Subject"] == "Grillen am Samstag", "Kürzel muss aus dem Betreff"


def test_kuerzel_ersetzt_geschaeftliche_signatur_des_addins(privat):
    _, html = _versand(privat, _mail(betreff="#privat Hallo", rumpf="<p>Text</p>" + ADDIN_SIG))
    assert "Add-in-Signatur" not in html
    assert "[Priv]" in html and "[Ban]" not in html


def test_kopf_des_addins_laesst_seine_signatur_stehen(privat):
    msg, html = _versand(privat, _mit_kopf(_mail(rumpf="<p>Text</p>" + ADDIN_SIG)))
    assert "Add-in-Signatur" in html, "die private Signatur aus dem Add-in bleibt"
    assert "[Priv]" not in html and "[Sig]" not in html
    assert "[Ban]" not in html and "[Dis]" not in html
    assert msg["X-Exo-Sig-Privat"] is None, "der Kopf geht nicht mit hinaus"


def test_ausgeschaltet_bleibt_alles_wie_bisher(privat):
    privat["PRIVAT_SIG_AKTIV"] = False
    msg, html = _versand(privat, _mit_kopf(_mail(betreff="Hallo #privat")))
    assert "[Sig]" in html and "[Ban]" in html and "[Dis]" in html
    assert "#privat" in msg["Subject"], "ausgeschaltet wird nichts aus dem Betreff entfernt"
    assert msg["X-Exo-Sig-Privat"] is None


def test_postfach_ohne_private_signatur_behaelt_banner_und_disclaimer(privat):
    privat["PRIVAT_SIGNATUREN"] = {"andere@firma.de": "Priv"}
    _, html = _versand(privat, _mit_kopf(_mail(betreff="Hallo #privat")))
    assert "[Priv]" not in html
    assert "[Sig]" in html and "[Ban]" in html and "[Dis]" in html


def test_ohne_ausloeser_normal(privat):
    _, html = _versand(privat, _mail())
    assert "[Sig]" in html and "[Ban]" in html and "[Priv]" not in html


def test_entscheiden_privat(privat):
    erg = policies.entscheiden(ABS, in_kette=True, alle_intern=False, privat=True)
    assert erg["art"] == "privat" and erg["signatur"]["vorlage"] == "Priv"
    assert erg["zusatz"] == frozenset() and erg["banner"] is None and erg["disclaimer"] is None
    assert policies.entscheiden(ABS, in_kette=False, alle_intern=False)["art"] == "standard"
