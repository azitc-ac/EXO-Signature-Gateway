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

def _item(art, konv="null", mit_kopf=True):
    kopf = (", internetHeaders: {setAsync: function (k, cb) { protokoll.kopf = k; "
            "cb({status: 'succeeded'}); }}") if mit_kopf else ""
    return ("{getComposeTypeAsync: " + _OK % ("{composeType: '%s'}" % art) +
            ", conversationId: " + konv +
            ", disableClientSignatureAsync: " + _OK % "null" +
            ", body: {setSignatureAsync: function (h, o, cb) { protokoll.gesetzt = h; "
            "cb({status: 'succeeded'}); }}" + kopf + "}")


@pytest.mark.parametrize("art", ["reply", "forward"])
def test_antwort_fragt_mit_konversation_und_setzt_signatur(art):
    p = _handler_lauf(_item(art, "'K+/='"))
    assert p["url"] == "https://x/api?client=iOS&konversation=K%2B%2F%3D"
    assert p["gesetzt"] == "<p>SIG</p>" and p["fertig"] == 1


def test_antwort_ohne_konversation_fragt_nicht():
    p = _handler_lauf(_item("reply"))
    assert p["abrufe"] == 0 and p["gesetzt"] is None and p["fertig"] == 1


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
