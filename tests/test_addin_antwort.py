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

    async def _kennungen(postfach, konv):
        zustand["graph"].append((postfach, konv))
        return zustand["kennungen"]
    monkeypatch.setattr(graph_client, "konversation_kennungen", _kennungen)
    monkeypatch.setattr(sig_thread, "kennt",
                        lambda p, refs: bool(set(refs or []) & zustand["bekannt"]))
    monkeypatch.setattr(policies, "aufloesen", lambda e, *a, **kw: dict(zustand["vorlagen"]))

    async def _user(email):
        return graph_client.UserData(mail=email)
    monkeypatch.setattr(graph_client, "get_user", _user)
    monkeypatch.setattr(signature_engine, "render", lambda u, t: (f"<p>SIG {t}</p>", ""))
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


def test_in_signierter_kette_die_antwort_signatur_und_kennzeichen(antwort, caplog):
    antwort["bekannt"] = {"<b@x>"}
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


def test_graph_filter_maskiert_hochkomma(monkeypatch):
    urls = []

    async def _tok():
        return "T"

    class _Antwort:
        status_code = 200

        @staticmethod
        def json():
            return {"value": [{"internetMessageId": "<a@x>", "isDraft": False},
                              {"internetMessageId": "<entwurf@x>", "isDraft": True},
                              {"isDraft": False}]}

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
    ids = asyncio.run(graph_client.konversation_kennungen("e@x", "a'b"))
    assert ids == ["<a@x>"]                       # Entwurf und Leeres verworfen
    from urllib.parse import unquote
    assert "conversationId eq 'a''b'" in unquote(urls[0])


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


def test_ohne_kette_kein_kopf():
    p = _lauf_mit_antwort(_item("reply", "'K'"), '{"html": "<p>V</p>", "kette": false}')
    assert "kopf" not in p and p["gesetzt"] == "<p>V</p>"


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
    html = _versand(anlage, _mit_kopf(_mail(betreff="AW: Hallo", rumpf="<p>T</p>" + ADDIN_SIG)))
    assert "Add-in-Signatur" in html and "[Sig]" not in html
    assert zaehler.count("stapel_verhindert") == 1


def test_von_hand_eingefuegter_block_ohne_kopf_zaehlt_nicht(anlage, zaehler):  # noqa: F811
    anlage["_kette"]["an"] = True
    _versand(anlage, _mail(betreff="AW: Hallo", rumpf="<p>T</p>" + ADDIN_SIG))
    assert "stapel_verhindert" not in zaehler


def test_kopf_ohne_signatur_gateway_folgt_der_kette(anlage, zaehler):  # noqa: F811
    """Add-in hat die Kette erkannt, aber keine Antwort-Signatur eingefügt
    (keine gewählt) — References allein kennt das Gateway nicht."""
    html = _versand(anlage, _mit_kopf(_mail(betreff="AW: Hallo")))
    assert "[Sig]" not in html
    assert zaehler.count("stapel_verhindert") == 1


def test_kopf_mit_antwortvorlage_setzt_die_kurze(anlage, zaehler):  # noqa: F811
    anlage["MAILBOX_CONFIG"]["max@firma.de"]["min_template"] = "Kurz"
    html = _versand(anlage, _mit_kopf(_mail(betreff="AW: Hallo")))
    assert "[Kurz]" in html and "[Sig]" not in html


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


def _graph_liste(monkeypatch, nachrichten):
    async def _tok():
        return "T"

    class _Antwort:
        status_code = 200

        @staticmethod
        def json():
            return {"value": nachrichten}

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return _Antwort()

    import httpx
    monkeypatch.setattr(graph_client, "_acquire_token_async", _tok)
    monkeypatch.setattr(httpx, "AsyncClient", _Client)


def _n(betreff, konv, von, entwurf=False):
    return {"subject": betreff, "conversationId": konv, "isDraft": entwurf,
            "from": {"emailAddress": {"address": von}}}


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
