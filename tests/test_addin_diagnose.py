"""Add-in: zu große Signaturen und Fehler aus Outlook im Gateway-Protokoll.

Anlass (12.11.2026): Eine Vorlage mit eingebettetem Bild überschritt die Grenze
von Office.Body.setSignatureAsync (30.000 Zeichen, MS Learn). Outlook Classic
meldete nur „Sys.ArgumentOutOfRangeException … Parameter name: data“, das
Gateway-Protokoll zeigte nichts. Geprüft wird:

1. die Grenze selbst (Office zählt UTF-16-Einheiten, nicht Codepunkte),
2. Verfassen-Ereignis: über der Grenze nichts einfügen, Grund melden und protokollieren,
3. Aufgabenbereich: Signatur weiter liefern, aber als zu groß kennzeichnen,
4. Vorlagen-Editor warnt,
5. /api/addin/meldung schreibt Fehler aus Outlook ins Protokoll — beschnitten
   und mit Obergrenze.
"""
import asyncio
import json
import logging

import pytest

import graph_client
import signature_engine
from webui.routen import addin, addin_verfassen as av
from test_addin_verfassen import _OK, _VOLL, _Req, _handler_lauf, _token, umgebung  # noqa: F401

GRENZE = addin.OUTLOOK_SIGNATUR_MAX


def _gross(n):
    """Rohsignatur, die markiert genau n Zeichen hat."""
    rand = len(addin.markiert(""))
    return "x" * (n - rand)


# ── 1. Grenze ───────────────────────────────────────────────────────────────────

def test_grenze_ist_die_von_outlook():
    assert GRENZE == 30_000
    assert addin.signatur_zu_gross("x" * GRENZE) == 0
    assert addin.signatur_zu_gross("x" * (GRENZE + 1)) == GRENZE + 1


def test_grenze_zaehlt_wie_office_js():
    """😀 ist EIN Codepunkt, für JavaScript aber zwei Einheiten."""
    s = "😀" * (GRENZE // 2 + 1)
    assert len(s) < GRENZE
    assert addin.signatur_zu_gross(s) == GRENZE + 2


# ── 2. Verfassen-Ereignis ───────────────────────────────────────────────────────

@pytest.fixture
def gross_rendern(monkeypatch):
    groesse = {"n": GRENZE + 500}

    async def _user(email):
        return graph_client.UserData(mail=email)
    monkeypatch.setattr(graph_client, "get_user", _user)
    monkeypatch.setattr(signature_engine, "render",
                        lambda u, t: (_gross(groesse["n"]), ""))
    return groesse


def test_ereignis_liefert_ueber_der_grenze_nichts(umgebung, gross_rendern, caplog):
    caplog.set_level(logging.INFO)
    r = asyncio.run(av.verfassen_signatur(_Req("Bearer " + _token())))
    body = json.loads(r.body)
    assert body["html"] == ""
    assert body["zu_gross"] == GRENZE + 500 and body["max"] == GRENZE
    assert body["vorlage"] == "default"
    zeilen = [s for s in caplog.records if s.levelno == logging.WARNING]
    assert any("30500 Zeichen" in s.getMessage() and "erika@zarenko.net" in s.getMessage()
               for s in zeilen)


def test_ereignis_an_der_grenze_wie_bisher(umgebung, gross_rendern):
    gross_rendern["n"] = GRENZE
    body = json.loads(asyncio.run(av.verfassen_signatur(_Req("Bearer " + _token()))).body)
    assert len(body["html"]) == GRENZE and "zu_gross" not in body


def test_handler_zeigt_den_grund_und_setzt_nichts():
    p = _handler_lauf(_VOLL, plattform="PC",
                      antwort="{html: '', vorlage: 'Grossbild', zu_gross: 41234, max: 30000}")
    assert p["gesetzt"] is None and p["fertig"] == 1
    assert p["hinweise"] == ["Signatur „Grossbild“ ist für Outlook zu groß "
                             "(41.234 Zeichen, erlaubt 30.000) – das Gateway fügt sie "
                             "beim Versand an."]


def test_handler_meldet_office_fehler_ans_gateway():
    """setSignatureAsync scheitert → Hinweis in Outlook UND Bericht ans Gateway."""
    item = (_VOLL.replace("cb({status: 'succeeded'}); }",
                          "cb({status: 'failed', error: {message: 'Specified argument was "
                          "out of the range of valid values. Parameter name: data'}}); }"))
    assert item != _VOLL
    p = _handler_lauf(item, plattform="PC")
    assert p["fertig"] == 1
    assert p["hinweise"][0].startswith("Gateway-Signatur nicht eingefügt: Specified argument")
    assert p["abrufe"] == 2                                  # Signatur + Bericht


# ── 3. Aufgabenbereich ──────────────────────────────────────────────────────────

class _TaskReq:
    cookies: dict = {}
    headers: dict = {}


def _taskpane(email="erika@zarenko.net"):
    r = asyncio.run(addin.api_addin_signature(_TaskReq(), email=email, template="",
                                              user=email))
    return json.loads(r.body)


def test_aufgabenbereich_kennzeichnet_zu_gross(umgebung, gross_rendern, caplog):
    caplog.set_level(logging.INFO)
    body = _taskpane()
    assert body["zu_gross"] == GRENZE + 500
    assert body["marked_html"]                    # wird über den ganzen Text eingefügt
    assert any(s.levelno == logging.WARNING and "Aufgabenbereich" in s.getMessage()
               for s in caplog.records)


def test_aufgabenbereich_protokolliert_auch_den_erfolg(umgebung, gross_rendern, caplog):
    """Damit eine 403 im Aufgabenbereich zuzuordnen ist: Steht diese Zeile im
    Protokoll, hat das Gateway geliefert — die 403 kam von davor."""
    caplog.set_level(logging.INFO)
    gross_rendern["n"] = 100
    assert _taskpane()["zu_gross"] == 0
    assert any("geliefert (100 Zeichen)" in s.getMessage() for s in caplog.records)


def test_taskpane_faellt_bei_zu_gross_auf_den_ganzen_text_zurueck():
    import pathlib
    html = (pathlib.Path(addin.__file__).resolve().parents[1] / "templates"
            / "addin_compose.html").read_text(encoding="utf-8")
    assert "if (_signaturbereich() && !_zuGross) {" in html
    assert "_zuGross = d.zu_gross || 0;" in html
    assert "if (cls === 'err') _melden(msg);" in html      # jede Fehlerzeile ins Protokoll


# ── 4. Vorlagen-Editor ──────────────────────────────────────────────────────────

def test_editor_warnt_bei_zu_grosser_vorlage(monkeypatch):
    from webui.routen import vorlagen
    monkeypatch.setattr(signature_engine, "render", lambda u, t: (_gross(GRENZE + 1), ""))
    assert vorlagen._outlook_zu_gross("grossbild") == GRENZE + 1
    monkeypatch.setattr(signature_engine, "render", lambda u, t: (_gross(GRENZE), ""))
    assert vorlagen._outlook_zu_gross("grossbild") == 0


def test_editor_zeigt_die_warnung():
    import pathlib
    html = (pathlib.Path(addin.__file__).resolve().parents[1] / "templates"
            / "template_editor.html").read_text(encoding="utf-8")
    assert "{% if outlook_zu_gross %}" in html


# ── 5. Meldungen aus Outlook ────────────────────────────────────────────────────

class _MeldReq:
    def __init__(self, daten, auth=None):
        self.headers = {"authorization": auth} if auth else {}
        self.cookies = {}
        self.query_params = {}
        self._daten = daten

    async def json(self):
        return self._daten


@pytest.fixture
def meldungen(monkeypatch):
    monkeypatch.setattr(av, "_meldungen", {})


def _melden(daten, auth=None):
    return asyncio.run(av.addin_meldung(_MeldReq(daten, auth)))


def test_meldung_landet_im_protokoll(umgebung, meldungen, caplog):
    r = _melden({"quelle": "verfassen", "text": "Fehler\nmit\x00Steuerzeichen " + "x" * 400,
                 "client": "PC", "vorlage": "Grossbild"}, "Bearer " + _token())
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == "*"
    zeile = [s.getMessage() for s in caplog.records if s.levelno == logging.WARNING][-1]
    assert "verfassen, erika@zarenko.net, PC, Vorlage Grossbild" in zeile
    assert "\n" not in zeile and "\x00" not in zeile
    assert zeile.endswith("x" * 10) and len(zeile) < 450     # Text auf 300 Zeichen gekürzt


def test_meldung_ohne_anmeldung_wird_gekennzeichnet(umgebung, meldungen, caplog):
    _melden({"quelle": "verfassen", "text": "Token-Abruf gescheitert"})
    assert any("ohne Anmeldung" in s.getMessage() for s in caplog.records
               if s.levelno == logging.WARNING)


def test_meldung_hat_eine_obergrenze(umgebung, meldungen, caplog):
    for _ in range(av.MELDUNG_JE_STUNDE + 5):
        _melden({"text": "x"})
    zeilen = [s for s in caplog.records if "Outlook-Add-in meldet" in s.getMessage()]
    assert len(zeilen) == av.MELDUNG_JE_STUNDE


def test_meldung_vorabanfrage_erlaubt_post_und_kopf():
    r = asyncio.run(av.addin_meldung_vorab())
    assert r.status_code == 204
    assert "POST" in r.headers["access-control-allow-methods"]
    assert "X-Addin-Session" in r.headers["access-control-allow-headers"]


def test_laufzeit_kennt_die_meldeadresse(umgebung):
    assert av._konfiguration("https://sig.example")["meldung"] == \
        "https://sig.example" + av.MELDUNG_API
