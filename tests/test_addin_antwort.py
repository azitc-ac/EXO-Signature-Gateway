"""Verfassen-Add-in bei Antworten und Weiterleitungen.

Bis v1.9.177 fügte das Add-in nur bei NEUEN Mails ein; Antworten blieben im
Entwurf leer, und das Gateway setzte die Signatur erst beim Versand — der
Nutzer sah beim Schreiben nicht, was hinausgeht. Office.js löst
OnNewMessageCompose auch bei Antwort/Weiterleitung aus (auch auf iOS/Android),
kennt dort aber nur die `conversationId`, nicht References/In-Reply-To.

Jetzt:
- Das Add-in schickt die `conversationId`; das Gateway holt deren Kennungen
  per Graph und wendet DIESELBE Regel an wie beim Versand
  (`policies.antwort_vorlage`): signierte Mail des Absenders in der
  Konversation → Antwort-Signatur (oder nichts), sonst die volle.
- Hat die Antwort-Regel gegriffen, setzt das Add-in `X-Exo-Sig-Kette: 1` an
  den Entwurf. handler.py übernimmt die Entscheidung und zählt die Wirkung
  („Stapel verhindert") — ohne den Kopf fiele die Kennzahl auf null.
- Ist die Konversation nicht ermittelbar: nichts einfügen, das Gateway
  entscheidet beim Versand wie bisher.
"""
import asyncio
import json
import re

import pytest

import graph_client
import policies
import sig_thread
import signature_engine
from webui.routen import addin, addin_verfassen as av

from test_addin_verfassen import _OK, _Req, _handler_lauf, _token, umgebung  # noqa: F401
from test_zusatz_ohne_signatur import ADDIN_SIG, _mail, _versand, anlage  # noqa: F401

KONV = "AAQkAGI2TG93AAA="


# ── Endpunkt ────────────────────────────────────────────────────────────────

@pytest.fixture
def antwort(umgebung, monkeypatch):  # noqa: F811
    zustand = {"kennungen": ["<a@x>", "<b@x>"], "bekannt": set(), "graph": [],
               "vorlagen": {"sig": "Voll", "min": "Kurz", "oof": "", "banner": "", "disclaimer": ""}}

    async def _bezug(postfach, konv, art="reply", an=None, cc=None):
        zustand["graph"].append((postfach, konv))
        if zustand["kennungen"] is None:
            return None
        return {"kennungen": zustand["kennungen"], "html": zustand.get("html", ""),
                "von": "kunde@extern.de", "zeit": "2026-10-19T15:50:00Z"}
    monkeypatch.setattr(graph_client, "antwort_bezug", _bezug)
    monkeypatch.setattr(sig_thread, "kennt",
                        lambda p, refs: bool(set(refs or []) & zustand["bekannt"]))
    monkeypatch.setattr(policies, "aufloesen", lambda e, *a, **kw: dict(zustand["vorlagen"]))

    async def _user(email):
        return graph_client.UserData(mail=email)
    monkeypatch.setattr(graph_client, "get_user", _user)
    monkeypatch.setattr(signature_engine, "render",
                        lambda u, t=None, template_name=None: (f"<p>SIG {t or template_name}</p>", ""))
    return zustand


def _abruf(konversation=None):
    req = _Req("Bearer " + _token())
    req.query_params = {"client": "iOS"}
    if konversation is not None:
        req.query_params["konversation"] = konversation
    r = asyncio.run(av.verfassen_signatur(req))
    return json.loads(r.body)


def test_erste_antwort_bekommt_die_volle_signatur(antwort):
    body = _abruf(KONV)
    assert body == {"html": addin.markiert("<p>SIG Voll</p>"), "kette": False}
    assert antwort["graph"] == [("erika@zarenko.net", KONV)]


def test_verweis_auf_signierte_mail_allein_reicht_nicht(antwort):
    # Echo-Dienst 17.10.2026: verweist auf die signierte Mail, zitiert sie nicht.
    antwort["bekannt"] = {"<b@x>"}
    assert _abruf(KONV) == {"html": addin.markiert("<p>SIG Voll</p>"), "kette": False}


def test_antwort_auf_die_eigene_signierte_mail_die_antwort_signatur(antwort, caplog):
    antwort["bekannt"] = {"<a@x>"}
    import logging
    with caplog.at_level(logging.INFO):
        body = _abruf(KONV)
    assert body == {"html": addin.markiert("<p>SIG Kurz</p>"), "kette": True}
    assert any(r.getMessage().startswith("SIG-KETTE: Add-in") for r in caplog.records)


def test_in_kette_ohne_antwort_signatur_nichts_aber_kennzeichen(antwort):
    antwort["bekannt"] = {"<a@x>"}
    antwort["vorlagen"]["min"] = ""
    assert _abruf(KONV) == {"html": "", "kette": True}


def test_konversation_nicht_ermittelbar_nichts_einfuegen(antwort):
    antwort["kennungen"] = None
    assert _abruf(KONV) == {"html": ""}


@pytest.mark.parametrize("roh", ["a' or 1 eq 1", "x" * 513, "", "  ", "ab cd", "ä"])
def test_unbrauchbare_kennung_geht_nicht_in_den_graph_filter(antwort, roh):
    assert _abruf(roh) == {"html": ""}
    assert antwort["graph"] == []


def test_neue_mail_unveraendert(antwort):
    assert _abruf() == {"html": addin.markiert("<p>SIG Voll</p>"), "kette": False}
    assert antwort["graph"] == []


def test_gateway_und_addin_teilen_eine_regel():
    v = {"sig": "Voll", "min": "Kurz"}
    assert policies.antwort_vorlage(v, False) == "Voll"
    assert policies.antwort_vorlage(v, True) == "Kurz"
    assert policies.antwort_vorlage({"sig": "Voll", "min": ""}, True) == ""
    from pathlib import Path
    quelle = (Path(__file__).resolve().parents[1] / "app/handler.py").read_text("utf-8")
    assert "antwort_vorlage(_vorlagen, True)" in quelle


def test_signatur_im_text_der_beantworteten_mail_zaehlt_wie_im_gateway(antwort):
    """Gateway-Kennung im Text der beantworteten Mail, ohne bekannte Kennung."""
    antwort["html"] = '<p>Danke</p><div id="x_exo-sig-s">x</div>'
    assert _abruf(KONV)["kette"] is True


SIG_ZEILEN = ("<table><tr><td>Erika Mustermann - Beratung</td></tr>"
              "<tr><td>Musterweg 1, 52066 Aachen</td></tr><tr><td>Tel. +49 241 123456</td></tr>"
              "<tr><td>erika@zarenko.net</td></tr></table>")


@pytest.mark.parametrize("html, erwartet", [
    # Fremdes Outlook hat die Kennung verworfen, der Text ist geblieben.
    ("<p>Gern.</p><div>Von: Erika</div><p>Erika Mustermann - Beratung<br>Musterweg 1,"
     "&nbsp;52066 Aachen<br>Tel.  +49 241 123456</p>", True),
    # Nur eine Zeile (die Adresse steht z. B. im Kopf) — zu wenig.
    ("<p>An: erika@zarenko.net</p><p>Hallo</p>", False),
    ("<table><tr><td>x-sig-applied</td><td>1</td></tr></table>", False),
])
def test_text_der_signatur_in_der_beantworteten_mail(antwort, monkeypatch, html, erwartet):
    monkeypatch.setattr(signature_engine, "render",
                        lambda u, t=None, template_name=None:
                        (SIG_ZEILEN if (t or template_name) == "Voll" else f"<p>SIG {t or template_name}</p>", ""))
    antwort["html"] = html
    assert _abruf(KONV)["kette"] is erwartet


def test_graph_filter_maskiert_hochkomma_und_liest_die_kette(monkeypatch):
    urls = []

    async def _tok():
        return "T"

    class _Antwort:
        status_code = 200

        @staticmethod
        def json():
            return {"value": [
                {"internetMessageId": "<entwurf@x>", "isDraft": True,
                 "receivedDateTime": "2026-10-19T21:00:00Z", "from": {"emailAddress": {"address": "kunde@y"}}},
                {"internetMessageId": "<m2@y>", "isDraft": False, "receivedDateTime": "2026-10-19T15:50:00Z",
                 "from": {"emailAddress": {"address": "Kunde@Y"}},
                 "internetMessageHeaders": [{"name": "References", "value": "<m0@y>\r\n <m1@y>"},
                                            {"name": "In-Reply-To", "value": "<m1@y>"},
                                            {"name": "Subject", "value": "<nicht@kette>"}],
                 "body": {"content": '<p>Hallo</p><div class="exo-gateway-sig">x</div>'}},
            ]}

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            urls.append(url)
            return _Antwort()

    import httpx
    monkeypatch.setattr(graph_client, "_acquire_token_async", _tok)
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    b = asyncio.run(graph_client.antwort_bezug("e@x", "a'b", "reply", ["kunde@y"]))
    assert b["kennungen"] == ["<m2@y>", "<m0@y>", "<m1@y>", "<m1@y>"]
    assert "exo-gateway-sig" in b["html"] and b["von"] == "Kunde@Y"
    from urllib.parse import unquote
    assert "conversationId eq 'a''b'" in unquote(urls[0])
    assert "internetMessageHeaders" in urls[0]


def _m(mid, von, zeit, entwurf=False):
    return {"internetMessageId": mid, "isDraft": entwurf, "receivedDateTime": zeit,
            "from": {"emailAddress": {"address": von}}}


# Die Unterhaltung vom 19.10.2026: Karsten → Stocker → eigene Antwort (signiert).
VERLAUF = [
    _m("<karsten@d>", "karsten@daad.de", "2026-10-19T10:35:00Z"),
    _m("<stocker@d>", "stocker@daad.de", "2026-10-19T15:50:00Z"),
    _m("<ich@z>", "alexander@zarenko.net", "2026-10-19T17:55:00Z"),
    _m("<entwurf@z>", "alexander@zarenko.net", "2026-10-19T21:32:00Z", entwurf=True),
]


def test_allen_antworten_auf_die_mail_vor_der_eigenen_antwort():
    """Nicht die jüngste Nachricht der Konversation (die eigene Antwort),
    sondern die des Absenders, dem geantwortet wird."""
    m = graph_client._beantwortete(VERLAUF, "alexander@zarenko.net", "reply",
                                   ["stocker@daad.de", "karsten@daad.de", "edalati@daad.de"])
    assert m["internetMessageId"] == "<stocker@d>"


def test_erster_empfaenger_entscheidet_vor_den_uebrigen():
    m = graph_client._beantwortete(VERLAUF, "alexander@zarenko.net", "reply",
                                   ["karsten@daad.de", "stocker@daad.de"])
    assert m["internetMessageId"] == "<karsten@d>"


def test_weiterleitung_nimmt_die_juengste_fremde():
    m = graph_client._beantwortete(VERLAUF, "alexander@zarenko.net", "forward", [])
    assert m["internetMessageId"] == "<stocker@d>"


def test_nur_eigene_nachrichten_nimmt_die_juengste():
    eigene = [_m("<a@z>", "ich@z", "2026-10-19T11:30:56Z"), _m("<b@z>", "ich@z", "2026-10-19T11:31:02Z")]
    assert graph_client._beantwortete(eigene, "ich@z", "reply", ["ich@z"])["internetMessageId"] == "<b@z>"
    assert graph_client._beantwortete([], "ich@z", "reply", []) is None


# ── Handler-Skript (Node) ────────────────────────────────────────────────────

def _item(art, konv="null", mit_kopf=True, betreff=None, an=None):
    kopf = (", internetHeaders: {setAsync: function (k, cb) { protokoll.kopf = k; "
            "cb({status: 'succeeded'}); }}") if mit_kopf else ""
    felder = ""
    if betreff is not None:
        felder += ", subject: {getAsync: " + _OK % json.dumps(betreff) + "}"
    if an is not None:
        felder += ", to: {getAsync: " + _OK % json.dumps(
            [{"emailAddress": a, "displayName": "X"} for a in an]) + "}"
    return ("{getComposeTypeAsync: " + _OK % ("{composeType: '%s'}" % art) +
            ", conversationId: " + konv + felder +
            ", disableClientSignatureAsync: " + _OK % "null" +
            ", body: {setSignatureAsync: function (h, o, cb) { protokoll.gesetzt = h; "
            "cb({status: 'succeeded'}); }}" + kopf + "}")


@pytest.mark.parametrize("art", ["reply", "forward"])
def test_antwort_fragt_mit_konversation_und_setzt_signatur(art):
    p = _handler_lauf(_item(art, "'K+/='"))
    assert p["url"] == f"https://x/api?client=iOS&art={art}&konversation=K%2B%2F%3D"
    assert p["gesetzt"] == "<p>SIG</p>" and p["fertig"] == 1


def test_ohne_kennung_schickt_betreff_und_empfaenger():
    """iOS: conversationId im Antwortentwurf leer (VM 19.10.2026)."""
    p = _handler_lauf(_item("reply", betreff="AW: Angebot & Termin",
                            an=["kunde@extern.de", "b@x.de"]))
    assert p["url"] == ("https://x/api?client=iOS&art=reply"
                        "&betreff=AW%3A%20Angebot%20%26%20Termin&an=kunde%40extern.de%2Cb%40x.de")
    assert p["gesetzt"] == "<p>SIG</p>" and p["fertig"] == 1


def test_scheiternde_feld_api_bricht_nicht_ab():
    item = _item("reply").replace(
        ", disableClientSignatureAsync",
        ", subject: {getAsync: function (cb) { cb({status: 'failed', error: {message: 'x'}}); }}"
        ", disableClientSignatureAsync", 1)
    p = _handler_lauf(item)
    assert p["url"] == "https://x/api?client=iOS&art=reply" and p["fertig"] == 1
    assert not p["hinweise"]


def test_neue_mail_ohne_konversation_in_der_adresse():
    p = _handler_lauf(_item("newMail", "'K'"))
    assert p["url"] == "https://x/api?client=iOS"


def _lauf_mit_antwort(item, antwort_json):
    """Wie _handler_lauf, aber mit eigener Gateway-Antwort und Kettenkopf-Name."""
    import test_addin_verfassen as tav
    alt = tav.av.HANDLER_DATEI.read_text(encoding="utf-8")
    vorspann = ("globalThis.__antwort = %s;\n" % antwort_json)
    ersatz = ("globalThis.fetch = function (url) { protokoll.abrufe++; protokoll.url = url;"
              " return Promise.resolve({ok: true, json: function () {"
              " return Promise.resolve(globalThis.__antwort); }}); };\n"
              "EXO_VERFASSEN.kettenkopf = 'X-Exo-Sig-Kette';\n")

    class _Datei:
        @staticmethod
        def read_text(encoding="utf-8"):
            return vorspann + ersatz + alt
    echt = tav.av.HANDLER_DATEI
    tav.av.HANDLER_DATEI = _Datei
    try:
        return _handler_lauf(item)
    finally:
        tav.av.HANDLER_DATEI = echt


def test_kette_setzt_den_kopf_auch_ohne_signatur():
    p = _lauf_mit_antwort(_item("reply", "'K'"), '{"html": "", "kette": true}')
    assert p["kopf"] == {"X-Exo-Sig-Kette": "1"} and p["gesetzt"] is None and p["fertig"] == 1


def test_volle_wahl_wird_ebenfalls_gekennzeichnet():
    """„0“ = Add-in wählte die volle Signatur — das Gateway prüft beim Versand nach."""
    p = _lauf_mit_antwort(_item("reply", "'K'"), '{"html": "<p>V</p>", "kette": false}')
    assert p["kopf"] == {"X-Exo-Sig-Kette": "0"} and p["gesetzt"] == "<p>V</p>"


def test_ohne_entscheidung_kein_kopf():
    """Gateway fand die beantwortete Mail nicht (kein "kette") — nichts zu prüfen."""
    p = _lauf_mit_antwort(_item("reply", "'K'"), '{"html": ""}')
    assert "kopf" not in p


def test_neue_mail_ohne_kopf():
    p = _lauf_mit_antwort(_item("newMail"), '{"html": "<p>V</p>", "kette": false}')
    assert "kopf" not in p and p["gesetzt"] == "<p>V</p>"


def test_inreplyto_und_cc_gehen_mit():
    item = _item("reply", betreff="AW: X", an=["a@x.de"]).replace(
        "conversationId: null", "conversationId: null, inReplyTo: '<orig@x.de>', "
        "cc: {getAsync: " + _OK % json.dumps([{"emailAddress": "c@x.de"}]) + "}", 1)
    p = _handler_lauf(item)
    assert p["url"] == ("https://x/api?client=iOS&art=reply&bezug=%3Corig%40x.de%3E"
                        "&betreff=AW%3A%20X&an=a%40x.de&cc=c%40x.de")


def test_cc_unlesbar_wird_nicht_geschickt():
    p = _handler_lauf(_item("reply", betreff="AW: X", an=["a@x.de"]))
    assert "&cc=" not in p["url"]


def test_ohne_header_api_trotzdem_signatur():
    p = _lauf_mit_antwort(_item("reply", "'K'", mit_kopf=False),
                          '{"html": "<p>K</p>", "kette": true}')
    assert p["gesetzt"] == "<p>K</p>" and p["fertig"] == 1 and not p["hinweise"]


# ── Gateway beim Versand ─────────────────────────────────────────────────────

@pytest.fixture
def zaehler(monkeypatch):
    import stats
    gezaehlt = []
    monkeypatch.setattr(stats, "increment", lambda k: gezaehlt.append(k))
    return gezaehlt


def _mit_kopf(content: bytes) -> bytes:
    return b"X-Exo-Sig-Kette: 1\r\nReferences: <alt@firma.de>\r\n" + content


def test_addin_antwortsignatur_mit_kopf_zaehlt_als_verhindert(anlage, zaehler):  # noqa: F811
    anlage["_kette"]["an"] = True                 # Gateway sieht es genauso
    html = _versand(anlage, _mit_kopf(_mail(betreff="AW: Hallo", rumpf="<p>T</p>" + ADDIN_SIG)))
    assert "Add-in-Signatur" in html and "[Sig]" not in html
    assert zaehler.count("stapel_verhindert") == 1 and "addin_korrigiert" not in zaehler


# ── Letztes Wort: Gateway prüft die Wahl des Add-ins nach ────────────────────

def _kopf(wert: str, content: bytes) -> bytes:
    return f"X-Exo-Sig-Kette: {wert}\r\nReferences: <alt@firma.de>\r\n".encode() + content


def test_gateway_ersetzt_falsche_antwortsignatur_durch_die_volle(anlage, zaehler):  # noqa: F811
    """Add-in schätzte „schon signiert“ (kurz), die beantwortete Mail trägt die
    eigene Signatur aber nicht — Empfänger bekämen die volle nie."""
    html = _versand(anlage, _kopf("1", _mail(betreff="AW: Hallo", rumpf="<p>T</p>" + ADDIN_SIG)))
    assert "Add-in-Signatur" not in html and "[Sig]" in html
    assert html.index("<p>T</p>") < html.index("[Sig]")
    assert zaehler.count("addin_korrigiert") == 1 and "stapel_verhindert" not in zaehler


def test_gateway_ersetzt_falsche_volle_durch_die_antwortsignatur(anlage, zaehler):  # noqa: F811
    """Antwort auf die eigene Mail (VM 20.10.2026, 00:30) als Antwort auf eine
    fremde geschätzt — volle Signatur ein zweites Mal."""
    anlage["MAILBOX_CONFIG"]["max@firma.de"]["min_template"] = "Kurz"
    anlage["_kette"]["an"] = True
    html = _versand(anlage, _kopf("0", _mail(betreff="AW: Hallo", rumpf="<p>T</p>" + ADDIN_SIG)))
    assert "Add-in-Signatur" not in html and "[Kurz]" in html and "[Sig]" not in html
    assert zaehler.count("addin_korrigiert") == 1 and zaehler.count("stapel_verhindert") == 1


def test_mit_letztem_wort_beim_addin_bleibt_der_entwurf(anlage, zaehler):  # noqa: F811
    anlage["ANTWORT_LETZTES_WORT"] = "addin"
    html = _versand(anlage, _kopf("1", _mail(betreff="AW: Hallo", rumpf="<p>T</p>" + ADDIN_SIG)))
    assert "Add-in-Signatur" in html and "[Sig]" not in html
    assert "addin_korrigiert" not in zaehler


def test_ohne_kopf_bleibt_eine_knopf_signatur_unberuehrt(anlage, zaehler):  # noqa: F811
    anlage["_kette"]["an"] = True
    html = _versand(anlage, _mail(betreff="AW: Hallo", rumpf="<p>T</p>" + ADDIN_SIG))
    assert "Add-in-Signatur" in html and "addin_korrigiert" not in zaehler


@pytest.mark.parametrize("roh, erwartet", [("gateway", "gateway"), ("addin", "addin"),
                                           ("", "gateway"), (None, "gateway"), ("Addin", "gateway")])
def test_letztes_wort_im_zweifel_gateway(monkeypatch, roh, erwartet):
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: roh)
    assert settings_store.antwort_letztes_wort() == erwartet


# ── Entfernen der Add-in-Signatur ────────────────────────────────────────────

import mail_processor  # noqa: E402

ZITAT = '<div id="divRplyFwdMsg">Von: X</div><div>alt <div id="x_exo-sig-s">ALT-SIG</div></div>'


@pytest.mark.parametrize("html, rest", [
    ('<p>T</p><div id="exo-sig-s"><div><p>S</p></div><div>2</div></div><p>nach</p>' + ZITAT,
     '<p>T</p><p>nach</p>' + ZITAT),
    ('<p>T</p><!-- exo-sig-start --><div id="exo-sig-s" class="exo-gateway-sig">S</div>'
     '<!-- exo-sig-end -->' + ZITAT, '<p>T</p>' + ZITAT),
])
def test_addin_block_wird_nur_im_verfassenbereich_entfernt(html, rest):
    assert mail_processor._addin_block_entfernen(html) == rest


def test_signatur_nur_im_zitat_bleibt():
    assert mail_processor._addin_block_entfernen("<p>T</p>" + ZITAT) is None


def test_kaputtes_html_wird_nicht_angefasst():
    assert mail_processor._addin_block_entfernen('<p>T</p><div id="exo-sig-s"><div>offen') is None


# ── Schätzung über An und Cc ─────────────────────────────────────────────────

def _mk(mid, von, zeit, an, cc=()):
    return {"internetMessageId": mid, "isDraft": False, "receivedDateTime": zeit,
            "from": {"emailAddress": {"address": von}},
            "toRecipients": [{"emailAddress": {"address": a}} for a in an],
            "ccRecipients": [{"emailAddress": {"address": a}} for a in cc]}


ICH = "alexander@zarenko.net"
DAAD = [  # wie auf der VM am 19.10.2026
    _mk("<k@d>", "karsten@daad.de", "2026-10-19T10:35:00Z", ["edalati@daad.de", "Alexander@zarenko.net", "stocker@daad.de"]),
    _mk("<s@d>", "stocker@daad.de", "2026-10-19T15:50:00Z", ["karsten@daad.de", "edalati@daad.de", "Alexander@zarenko.net"]),
    _mk("<ich@z>", ICH, "2026-10-19T17:55:00Z", ["stocker@daad.de"], ["karsten@daad.de", "edalati@daad.de"]),
]


def test_cc_trennt_die_eigene_mail_von_einer_juengeren_fremden():
    """Stocker schreibt später nur an mich; ich antworte allen auf MEINE Mail
    von 17:55 (An Stocker, Cc Karsten+Edalati). Stockers jüngere Mail ergäbe
    An Stocker ohne Cc — nur der Cc-Vergleich schliesst sie aus."""
    verlauf = DAAD + [_mk("<s2@d>", "stocker@daad.de", "2026-10-19T18:30:00Z", [ICH])]
    m = graph_client._beantwortete(verlauf, ICH, "reply", ["stocker@daad.de"],
                                   ["karsten@daad.de", "edalati@daad.de"])
    assert m["internetMessageId"] == "<ich@z>"


@pytest.mark.parametrize("an, cc, erwartet", [
    (["stocker@daad.de", "karsten@daad.de", "edalati@daad.de"], [], "<s@d>"),   # Allen antworten auf Stocker
    (["stocker@daad.de"], ["karsten@daad.de", "edalati@daad.de"], "<ich@z>"),   # Allen antworten auf die eigene
    (["stocker@daad.de"], None, "<ich@z>"),          # Cc unbekannt: jüngste passende
    # „Allen antworten“ auf Karsten ergibt dieselben Empfänger wie auf Stocker —
    # nicht unterscheidbar, die jüngere gewinnt (beide ohne eigene Signatur).
    (["karsten@daad.de", "edalati@daad.de", "stocker@daad.de"], [], "<s@d>"),
    (["jemand@neu.de"], [], "<s@d>"),                # nichts passt: Rückfall jüngste fremde
])
def test_schaetzung_ueber_an_und_cc(an, cc, erwartet):
    m = graph_client._beantwortete(DAAD, ICH, "reply", an, cc)
    assert m["internetMessageId"] == erwartet


# ── inReplyTo: genau statt geschätzt ─────────────────────────────────────────

def test_inreplyto_wird_genutzt_und_schaetzung_entfaellt(antwort, betreff_suche, monkeypatch):
    aufrufe = []

    async def _genau(postfach, mid):
        aufrufe.append(mid)
        return {"kennungen": ["<orig@x>"], "marker": False, "von": "a@x", "zeit": "z"}
    monkeypatch.setattr(graph_client, "bezug_per_kennung", _genau)
    antwort["bekannt"] = {"<orig@x>"}
    body = _abruf_mit(antwort, art="reply", bezug="<orig@x>", betreff="AW: X")
    assert body["kette"] is True and aufrufe == ["<orig@x>"]
    assert betreff_suche == [] and antwort["graph"] == []


def test_inreplyto_nicht_gefunden_faellt_auf_die_schaetzung_zurueck(antwort, betreff_suche, monkeypatch):
    async def _nichts(postfach, mid):
        return None
    monkeypatch.setattr(graph_client, "bezug_per_kennung", _nichts)
    antwort["gefunden"] = "KB"
    _abruf_mit(antwort, art="reply", bezug="<orig@x>", betreff="AW: X")
    assert betreff_suche and antwort["graph"] == [("erika@zarenko.net", "KB")]


@pytest.mark.parametrize("roh", ["keine-kennung", "<a b@x>", "<" + "a" * 1000 + "@x>"])
def test_unbrauchbares_inreplyto_wird_ignoriert(antwort, betreff_suche, monkeypatch, roh):
    aufrufe = []

    async def _genau(postfach, mid):
        aufrufe.append(mid)
    monkeypatch.setattr(graph_client, "bezug_per_kennung", _genau)
    _abruf_mit(antwort, art="reply", bezug=roh)
    assert aufrufe == []


def test_von_hand_eingefuegter_block_ohne_kopf_zaehlt_nicht(anlage, zaehler):  # noqa: F811
    anlage["_kette"]["an"] = True
    _versand(anlage, _mail(betreff="AW: Hallo", rumpf="<p>T</p>" + ADDIN_SIG))
    assert "stapel_verhindert" not in zaehler


def test_kopf_allein_nimmt_dem_gateway_die_signatur_nicht(anlage, zaehler):  # noqa: F811
    """Bis v1.9.180 galt der Kopf als Kette. Lag das Add-in falsch (ganze
    Konversation statt beantworteter Mail), fehlte die Signatur auch beim
    Versand. Der Kopf zählt nur noch — die Kette bestimmt das Gateway."""
    html = _versand(anlage, _mit_kopf(_mail(betreff="AW: Hallo")))
    assert "[Sig]" in html
    assert "stapel_verhindert" not in zaehler


def test_kette_des_gateways_mit_antwortvorlage_setzt_die_kurze(anlage, zaehler):  # noqa: F811
    anlage["MAILBOX_CONFIG"]["max@firma.de"]["min_template"] = "Kurz"
    anlage["_kette"]["an"] = True
    html = _versand(anlage, _mit_kopf(_mail(betreff="AW: Hallo")))
    assert "[Kurz]" in html and "[Sig]" not in html
    assert zaehler.count("stapel_verhindert") == 1


# ── Rückfall über Betreff und Empfänger (iOS liefert keine conversationId) ──

def _abruf_mit(antwort, **q):
    req = _Req("Bearer " + _token())
    req.query_params = {"client": "iOS", **q}
    return json.loads(asyncio.run(av.verfassen_signatur(req)).body)


@pytest.fixture
def betreff_suche(antwort, monkeypatch):
    aufrufe = []

    async def _suche(postfach, betreff, an):
        aufrufe.append((postfach, betreff, an))
        return antwort.get("gefunden")
    monkeypatch.setattr(graph_client, "konversation_per_betreff", _suche)
    return aufrufe


def test_ohne_kennung_findet_die_konversation_ueber_den_betreff(antwort, betreff_suche, caplog):
    antwort["gefunden"] = "KB"
    antwort["bekannt"] = {"<a@x>"}
    import logging
    with caplog.at_level(logging.INFO):
        body = _abruf_mit(antwort, art="reply", betreff="AW: Angebot", an="Kunde@Extern.de,x")
    assert body == {"html": addin.markiert("<p>SIG Kurz</p>"), "kette": True}
    assert betreff_suche == [("erika@zarenko.net", "AW: Angebot", ["kunde@extern.de"])]
    assert antwort["graph"] == [("erika@zarenko.net", "KB")]
    assert any("Kennung nein, Betreff ja, Konversation über Betreff" in r.getMessage()
               for r in caplog.records)


def test_betreff_ohne_treffer_nichts_einfuegen(antwort, betreff_suche, caplog):
    import logging
    with caplog.at_level(logging.INFO):
        assert _abruf_mit(antwort, art="forward", betreff="WG: X") == {"html": ""}
    assert antwort["graph"] == []
    assert any("Weiterleitung" in r.getMessage() and "nicht gefunden" in r.getMessage()
               for r in caplog.records)


def test_antwort_ohne_kennung_und_betreff_fragt_graph_nicht(antwort, betreff_suche):
    assert _abruf_mit(antwort, art="reply") == {"html": ""}
    assert betreff_suche == [] and antwort["graph"] == []


def test_kennung_hat_vorrang_vor_dem_betreff(antwort, betreff_suche):
    _abruf_mit(antwort, art="reply", konversation=KONV, betreff="AW: X")
    assert betreff_suche == [] and antwort["graph"] == [("erika@zarenko.net", KONV)]


@pytest.mark.parametrize("roh, kern", [
    ("AW: Angebot", "angebot"), ("Re: AW: WG: Angebot", "angebot"),
    ("RE[2]: Angebot", "angebot"), ("Fwd:  Angebot  neu", "angebot neu"),
    ("Antwort: Angebot", "angebot"), ("Angebot: Teil 2", "angebot: teil 2"),
    ("Area: Nord", "area: nord"), ("", ""),
])
def test_betreff_kern(roh, kern):
    assert graph_client.betreff_kern(roh) == kern


def _graph_liste(monkeypatch, nachrichten, je_absender=None):
    """Graph-Attrappe: ohne $filter die Liste `nachrichten`, mit Absender-Filter
    die Liste aus `je_absender[adresse]`. Gibt die abgefragten URLs zurück."""
    from urllib.parse import unquote
    urls = []

    async def _tok():
        return "T"

    class _Antwort:
        def __init__(self, wert):
            self.status_code, self._wert = 200, wert

        def json(self):
            return {"value": self._wert}

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            urls.append(unquote(url))
            m = re.search(r"from/emailAddress/address eq '([^']*)'", unquote(url))
            if m:
                return _Antwort((je_absender or {}).get(m.group(1), []))
            return _Antwort(nachrichten)

    import httpx
    monkeypatch.setattr(graph_client, "_acquire_token_async", _tok)
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return urls


def _n(betreff, konv, von, entwurf=False, zeit="2026-10-19T12:00:00Z"):
    return {"subject": betreff, "conversationId": konv, "isDraft": entwurf,
            "from": {"emailAddress": {"address": von}}, "receivedDateTime": zeit}


def test_betreff_suche_bevorzugt_den_absender_unter_den_empfaengern(monkeypatch):
    _graph_liste(monkeypatch, [
        _n("AW: Angebot", "ENTWURF", "ich@x", entwurf=True),
        _n("Angebot", "NEUESTE", "fremd@y"),
        _n("Angebot", "RICHTIG", "Kunde@Extern.de"),
        _n("Anderes", "X", "kunde@extern.de"),
    ])
    k = asyncio.run(graph_client.konversation_per_betreff("ich@x", "AW: Angebot", ["kunde@extern.de"]))
    assert k == "RICHTIG"
    assert asyncio.run(graph_client.konversation_per_betreff("ich@x", "WG: Angebot", [])) == "NEUESTE"
    assert asyncio.run(graph_client.konversation_per_betreff("ich@x", "Gibt es nicht", [])) is None
    assert asyncio.run(graph_client.konversation_per_betreff("ich@x", "AW: ", [])) is None


def _suche(postfach, betreff, an):
    return asyncio.run(graph_client.konversation_per_betreff(postfach, betreff, an))


def test_aeltere_mail_wird_ueber_den_absender_gefunden(monkeypatch):
    # VM 20.10.2026: Die beantwortete Mail vom 17.10. lag ausserhalb der letzten 100.
    urls = _graph_liste(monkeypatch, [_n("Anderes", "X", "a@b.de")], je_absender={
        "echo25@mail.de": [_n("Header-Echo: (ohne Betreff)", "ALT", "echo25@mail.de",
                              zeit="2026-10-17T19:06:00Z"),
                           _n("Header-Echo: Test", "SEPT", "echo25@mail.de",
                              zeit="2026-09-05T16:29:00Z")],
        "zweit@x.de": [_n("Re: Header-Echo: (ohne Betreff)", "AELTER", "zweit@x.de",
                          zeit="2026-10-01T08:00:00Z")]})
    k = _suche("ich@x.de", "Re: Header-Echo: (ohne Betreff)", ["zweit@x.de", "echo25@mail.de", "ich@x.de"])
    assert k == "ALT"                                   # jüngste über alle Absender
    gefiltert = [u for u in urls if "$filter=" in u]
    assert len(gefiltert) == 2                          # das eigene Postfach nicht
    assert all("receivedDateTime ge" in u and "$orderby=receivedDateTime desc" in u for u in gefiltert)


def test_absender_suche_nur_wenn_die_letzten_100_nichts_finden(monkeypatch):
    # Antwort auf die eigene, neue Mail: Ein alter gleichnamiger Faden des
    # Empfängers darf nicht gewinnen.
    urls = _graph_liste(monkeypatch, [_n("Angebot", "EIGENE", "ich@x.de")], je_absender={
        "kunde@extern.de": [_n("Angebot", "ALT", "kunde@extern.de", zeit="2025-01-01T00:00:00Z")]})
    assert _suche("ich@x.de", "AW: Angebot", ["kunde@extern.de"]) == "EIGENE"
    assert not [u for u in urls if "$filter=" in u]


@pytest.mark.parametrize("adresse", [
    "a'or'1@x.de", "a@x.de') or (1 eq 1", "a b@x.de", "a@x", "a(@x.de", "@x.de",
])
def test_unsichere_adresse_geht_nicht_in_den_filter(monkeypatch, adresse):
    urls = _graph_liste(monkeypatch, [])
    assert _suche("ich@x.de", "AW: Angebot", [adresse]) is None
    assert not [u for u in urls if "$filter=" in u]


def test_absender_suche_hoechstens_fuenf_adressen(monkeypatch):
    urls = _graph_liste(monkeypatch, [])
    _suche("ich@x.de", "AW: Angebot", [f"a{i}@x.de" for i in range(9)])
    assert len([u for u in urls if "$filter=" in u]) == 5


@pytest.mark.parametrize("aufruf", [
    lambda mid: graph_client.update_sent_item("ich@x.de", mid, "<p>x</p>"),
    lambda mid: graph_client.cleanup_sent_items("ich@x.de", mid, "<p>x</p>"),
])
def test_message_id_mit_hochkomma_bleibt_im_filter(monkeypatch, aufruf):
    # Die Message-ID kommt aus dem Mailkopf; ein ' darf die Abfrage nicht
    # verlassen — das gefundene Element wird danach geändert oder gelöscht.
    urls = _graph_liste(monkeypatch, [])
    asyncio.run(aufruf("<a' or internetMessageId ne 'b@x>"))
    assert urls and "internetMessageId eq '<a'' or internetMessageId ne ''b@x>'" in urls[0]


# ── Gateway: Antwort-Signatur nur, wenn die eigene in der beantworteten Mail steht ──

def _antwort_mail(rumpf, in_reply_to=None, references=None):
    roh = _mail(betreff="AW: Hallo", rumpf=rumpf).decode()
    kopf = "".join(f"{n}: {w}\r\n" for n, w in (("In-Reply-To", in_reply_to),
                                                  ("References", references)) if w)
    return roh.replace("Message-ID: <m1@firma.de>\r\n", "Message-ID: <m1@firma.de>\r\n" + kopf).encode()


GW_SIG = ("<table><tr><td>Max Muster - Beratung</td></tr><tr><td>Musterweg 1, 52066 Aachen</td></tr>"
          "<tr><td>Tel. +49 241 123456</td></tr><tr><td>max@firma.de</td></tr></table>")


@pytest.fixture
def gw_kette(anlage, monkeypatch):  # noqa: F811
    anlage["MAILBOX_CONFIG"]["max@firma.de"]["min_template"] = "Kurz"
    bekannt = {"<signiert@firma.de>"}
    monkeypatch.setattr(sig_thread, "kennt", lambda s, refs: bool(set(refs or []) & bekannt))
    monkeypatch.setattr(signature_engine, "render",
                        lambda u, template_name=None, **kw:
                        (GW_SIG if template_name == "Sig" else f"<p>[{template_name}]</p>", "x"))
    return anlage


def _bekam(html):
    return "Kurz" if "[Kurz]" in html else "Voll" if html.count("Musterweg 1") else "nichts"


def test_gw_verweis_ohne_zitat_bekommt_die_volle(gw_kette):
    # Echo-Dienst (VM 17.10.2026): References nennt die signierte Mail, die
    # beantwortete Mail selbst ist eine andere und zitiert sie nicht.
    html = _versand(gw_kette, _antwort_mail("<p>Danke</p>", in_reply_to="<echo@x.de>",
                                            references="<signiert@firma.de> <echo@x.de>"))
    assert _bekam(html) == "Voll"


def test_gw_antwort_auf_die_eigene_signierte_mail(gw_kette):
    html = _versand(gw_kette, _antwort_mail("<p>Nachtrag</p>", in_reply_to="<signiert@firma.de>"))
    assert _bekam(html) == "Kurz"


def test_gw_ohne_in_reply_to_zaehlt_der_letzte_verweis(gw_kette):
    html = _versand(gw_kette, _antwort_mail("<p>x</p>", references="<a@x.de> <signiert@firma.de>"))
    assert _bekam(html) == "Kurz"


def test_gw_signaturtext_im_zitat_ohne_kennung(gw_kette):
    # Fremdes Outlook hat die Gateway-Kennung beim Zitieren verworfen.
    zitat = ('<div id="divRplyFwdMsg">Von: Kunde</div><div><p>Frage</p><p>Max Muster - Beratung<br>'
             'Musterweg 1, 52066 Aachen<br>Tel. +49 241 123456</p></div>')
    html = _versand(gw_kette, _antwort_mail("<p>Antwort</p>" + zitat, in_reply_to="<kunde@x.de>"))
    assert _bekam(html) == "Kurz"


def test_gw_signaturtext_nur_im_verfassenbereich_zaehlt_nicht(gw_kette):
    # v1.4.312–316: Die Client-Signatur im eigenen Text darf keine Kette vortäuschen.
    eigen = "<p>Antwort</p><p>Max Muster - Beratung<br>Musterweg 1, 52066 Aachen<br>Tel. +49 241 123456</p>"
    html = _versand(gw_kette, _antwort_mail(eigen, in_reply_to="<kunde@x.de>"))
    assert "[Kurz]" not in html


def test_gw_signaturtext_ueber_dem_zitat_zaehlt_nicht(gw_kette):
    # Client-Signatur im eigenen Text, darunter ein Zitat OHNE die eigene.
    eigen = "<p>Antwort</p><p>Max Muster - Beratung<br>Musterweg 1, 52066 Aachen<br>Tel. +49 241 123456</p>"
    zitat = '<div id="divRplyFwdMsg">Von: Kunde</div><div><p>Frage ohne Signatur von Max</p></div>'
    html = _versand(gw_kette, _antwort_mail(eigen + zitat, in_reply_to="<kunde@x.de>"))
    assert "[Kurz]" not in html


def test_gw_antwort_signatur_im_zitat_zaehlt_auch(gw_kette, monkeypatch):
    # Die vorige Antwort trug nur die Antwort-Signatur — auch sie ist „zu lesen“.
    kurz = "<p>Viele Grüsse, Max M.</p><p>Mobil 0151 99887766</p>"
    monkeypatch.setattr(signature_engine, "render",
                        lambda u, template_name=None, **kw:
                        (GW_SIG if template_name == "Sig" else kurz if template_name == "Kurz"
                         else f"<p>[{template_name}]</p>", "x"))
    zitat = ('<div id="divRplyFwdMsg">Von: Kunde</div><div><p>Frage</p>'
             '<p>Viele Grüsse, Max M.</p><p>Mobil 0151 99887766</p></div>')
    html = _versand(gw_kette, _antwort_mail("<p>Antwort</p>" + zitat, in_reply_to="<kunde@x.de>"))
    assert "Musterweg 1" not in html and html.count("Mobil 0151 99887766") == 2
