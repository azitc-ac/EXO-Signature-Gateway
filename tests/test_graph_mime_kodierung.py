"""Der MIME-Weg in ein Postfach schreibt base64 — nicht roh.

ANLASS (24.08.2026)
-------------------
`deliver_to_mailbox_mime()` schickte die MIME-Bytes roh, weil Docstring und
CLAUDE.md übereinstimmend behaupteten, dieser Endpunkt wolle sie roh (»NOT
base64-encoded (which is what sendMail requires)«). Exchange antwortete jedes
Mal:

    HTTP 400 — ErrorMimeContentInvalidBase64String: Invalid base64 string
    for MIME content.

Auf der Produktions-VM 11 von 11 Versuchen am 20./21.08.2026, kein einziger
Erfolg im Protokollzeitraum ab dem 24.07. Jede dieser Nachrichten fiel danach
auf `deliver_to_mailbox()` zurück — den JSON-Nachbau, der in Outlook Classic
weiss dargestellt wird und den dieser Weg gerade vermeiden soll. Der Fehler war
also nicht folgenlos, sondern hat still das Gegenteil dessen bewirkt, wofür die
Funktion da ist.

Primärquelle (Graph v1.0, »Create message«): "provide the MIME content with the
applicable Internet message headers … all encoded in base64 format in the
request body".

Eine Asymmetrie zu `sendMail` gibt es nicht — der schickt ebenfalls
`Content-Type: text/plain` und ebenfalls base64. Der Unterschied besteht
zwischen MIME und JSON, nicht zwischen den Endpunkten. (Beim Aufräumen war
zuerst »der Unterschied liegt im Content-Type« notiert; auch das war falsch und
fiel erst beim Nachsehen in `send_via_graph_mime` auf.)

Warum kein Test das gemeldet hat: Alle vorhandenen prüfen, was das Gateway aus
einer Nachricht MACHT. Ob das Ergebnis für die Gegenstelle richtig verpackt ist,
sieht man nur an ihrer Antwort — und die kam im Betrieb, nicht im Testlauf.
"""
import base64
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import graph_reinject  # noqa: E402

MIME = (b"From: partner@example.org\r\n"
        b"To: empfaenger@firma.de\r\n"
        b"Subject: Test\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Inhalt mit Umlaut: \xc3\xa4\r\n")


class _Antwort:
    status_code = 201

    @staticmethod
    def json():
        return {"id": "AAMk-test"}

    text = ""


@pytest.fixture
def mitgeschnitten(monkeypatch):
    """Fängt den POST ab, statt Exchange zu rufen."""
    aufrufe = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, content=None, headers=None, **k):
            aufrufe.append({"url": url, "content": content, "headers": headers or {}})
            return _Antwort()

        def patch(self, *a, **k):
            return _Antwort()

    monkeypatch.setattr(graph_reinject.graph_client, "_acquire_token", lambda: "tok")
    monkeypatch.setattr(graph_reinject.httpx, "Client", _Client)
    return aufrufe


def test_rumpf_ist_base64(mitgeschnitten):
    """Roh gesendet quittiert Exchange mit ErrorMimeContentInvalidBase64String."""
    assert graph_reinject.deliver_to_mailbox_mime(
        "partner@example.org", ["empfaenger@firma.de"], MIME)

    rumpf = mitgeschnitten[0]["content"]
    assert rumpf != MIME, (
        "Die MIME-Bytes gehen roh hinaus. Exchange lehnt das mit HTTP 400 "
        "ErrorMimeContentInvalidBase64String ab, und die Zustellung fällt auf "
        "den JSON-Nachbau zurück — in Outlook Classic eine weisse Nachricht.")
    # Muss sich zurückverwandeln lassen, sonst ist es irgendetwas anderes
    assert base64.b64decode(rumpf) == MIME


def test_content_type_bleibt_text_plain(mitgeschnitten):
    """base64 ändert den Content-Type NICHT.

    `text/plain` unterscheidet die MIME- von der JSON-Variante desselben
    Endpunkts; `application/json` mit einem MIME-Rumpf ergibt
    `UnableToDeserializePostBody` — der Fehler, aus dem seinerzeit die falsche
    Lehre »dann eben roh« gezogen wurde.
    """
    graph_reinject.deliver_to_mailbox_mime(
        "partner@example.org", ["empfaenger@firma.de"], MIME)
    assert mitgeschnitten[0]["headers"].get("Content-Type") == "text/plain"


def test_zeilenenden_vor_dem_kodieren(mitgeschnitten):
    """Ein blosses LF muss VOR dem Kodieren zu CRLF werden.

    Andernfalls steckt es im kodierten Rumpf und ist für Exchange nicht mehr
    zu erkennen — derselbe Fallstrick, der bei den ACME-Antworten zu
    »SMTPSEND.BareLinefeedsAreIllegal« geführt hat.
    """
    graph_reinject.deliver_to_mailbox_mime(
        "partner@example.org", ["empfaenger@firma.de"],
        b"From: a@b.de\nTo: c@d.de\nSubject: LF\n\nRumpf\n")
    entpackt = base64.b64decode(mitgeschnitten[0]["content"])
    assert b"\r\n" in entpackt
    assert entpackt.replace(b"\r\n", b"") .find(b"\n") == -1, (
        "Es ist ein blosses LF im kodierten Rumpf verblieben.")
