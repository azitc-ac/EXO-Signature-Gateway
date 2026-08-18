"""Bestätigungslink aus der Mail der Zertifizierungsstelle.

Nach einer Bestellung schickt die CA eine Mail an das Postfach; erst der Klick
darin löst die Ausstellung aus. Wer die Anlage betreut, sitzt aber nicht
zwangsläufig an diesem Postfach — am 18.08.2026 lag die Mail seit einer Stunde
da, und in der Oberfläche stand nur, dass kein Zertifikat vorhanden sei.

Invarianten:

1. ⚠️ Gefunden wird über die **Referenz der Zertifizierungsstelle**, nicht über
   „die neueste Mail". Im Postfach lagen zwei Bestellungen kurz hintereinander;
   „die neueste" wäre in der Hälfte der Fälle die falsche gewesen.
2. Nur Mails der Zertifizierungsstelle zählen — sonst genügte eine
   untergeschobene Mail mit passendem Betreff, um einen fremden Link
   einzuschleusen.
3. Ohne Referenz wird gar nicht erst gesucht.
4. Bilder und Fusszeilen der Mail führen ebenfalls auf die CA-Domäne und dürfen
   nicht als Bestätigungsadresse durchgehen.
"""
import asyncio
import types

import pytest

import hub_orders

ECHT = ("https://certmanager.test.certum.pl/emailVerification?themeId=default"
        "&partnerId=b62e2d1f415df9f0d232af0e92b025d658d4c126&verificationId=cQIBvusg"
        "kPtZftoiw2w1viZSnO44BaEbivHfsS8-Wtx905RmUSFp0z&language=en")


def _mail(betreff, absender="autoresponder2@certum.pl", koerper=None):
    return {"subject": betreff,
            "from": {"emailAddress": {"address": absender}},
            "body": {"content": koerper if koerper is not None else
                     f'<img src="https://repository.certum.pl/mail/info.png">'
                     f'<a href="{ECHT.replace("&", "&amp;")}">Verify</a>'}}


@pytest.fixture
def postfach(monkeypatch):
    """Graph-Antwort ersetzen — kein Netz, kein Token."""
    stand = {"mails": []}

    class _Antwort:
        status_code = 200
        def json(self): return {"value": stand["mails"]}

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None): return _Antwort()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())
    import graph_client
    async def _token(): return "tok"
    monkeypatch.setattr(graph_client, "_acquire_token_async", _token)
    return stand


def _hole(email="mig3@azitc.eu", ref="exo7a8796b72f5943e8"):
    return asyncio.run(hub_orders.bestaetigungslink(email, ref))


def test_findet_die_mail_zur_referenz(postfach):
    postfach["mails"] = [_mail("E-mail verification - exo7a8796b72f5943e8")]
    assert _hole() == ECHT


def test_nimmt_nicht_die_neueste_sondern_die_passende(postfach):
    """⚠️ Der Fall, der live vorlag: zwei Bestellungen, zwei Mails."""
    anderer = ECHT.replace("cQIBvusg", "FALSCHER")
    postfach["mails"] = [
        _mail("E-mail verification - exo2765cc18758249fd",
              koerper=f'<a href="{anderer}">Verify</a>'),      # neuer, aber fremd
        _mail("E-mail verification - exo7a8796b72f5943e8"),
    ]
    assert _hole() == ECHT


def test_fremder_absender_wird_ignoriert(postfach):
    """Sonst genügt eine Mail mit passendem Betreff, um einen Link
    einzuschleusen — geklickt würde er von einem Menschen im Vertrauen auf
    die Anzeige."""
    postfach["mails"] = [_mail("E-mail verification - exo7a8796b72f5943e8",
                               absender="angreifer@example.org")]
    assert _hole() == ""


def test_ohne_referenz_wird_nicht_gesucht(postfach):
    postfach["mails"] = [_mail("E-mail verification - exo7a8796b72f5943e8")]
    assert _hole(ref="") == ""


def test_bilder_und_fusszeilen_sind_keine_bestaetigungsadresse(postfach):
    postfach["mails"] = [_mail(
        "E-mail verification - exo7a8796b72f5943e8",
        koerper='<img src="https://repository.certum.pl/mail/banner-arrow.png">'
                '<a href="https://www.certum.eu/en/repository/">Repository</a>')]
    assert _hole() == ""


def test_ohne_passende_mail_bleibt_es_leer(postfach):
    postfach["mails"] = [_mail("Ganz andere Nachricht")]
    assert _hole() == ""


# ── Zeitfenster ──────────────────────────────────────────────────────────────

def test_suche_beginnt_vor_dem_bestellzeitpunkt():
    """⚠️ Live aufgetreten: Die Mail war ÄLTER als der Vorgang, zu dem sie
    gehört. Die Zertifizierungsstelle verschickt sie beim Annehmen der
    Bestellung; der lokale Zeitstempel entsteht erst nach Antwort und Speichern
    (23:51:1x gegen 23:51:25). Ein Filter ab dem Bestellzeitpunkt findet sie nie.
    """
    filt = hub_orders._zeitfilter("2026-08-17T23:51:25.892073+00:00")
    assert "23:36:25" in filt, f"kein Vorlauf im Filter: {filt}"


def test_ohne_zeitangabe_kein_filter():
    assert hub_orders._zeitfilter("") == ""


def test_unlesbare_zeitangabe_filtert_nicht_statt_zu_scheitern():
    """Lieber alle 25 Nachrichten durchsehen als gar nicht suchen."""
    assert hub_orders._zeitfilter("kein Datum") == ""
