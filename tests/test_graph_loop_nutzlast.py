"""Der Graph-Hauptweg trägt die Schleifenmarkierung in der NUTZLAST.

`send_via_graph` ist der Standard-Rückweg (reinject.py ruft ihn an drei
Stellen). Fehlt `X-Sig-Applied` in der sendMail-Nutzlast, routet die
FromMemberOf-Regel die Mail endlos zurück ans Gateway (Schleife, siehe
CLAUDE.md „LOOP-GEFAHR“).

Bis v1.12.19 prüften das nur Textsuchen (test_loop_header, test_loop_kennung):
Sie fanden die ZUWEISUNG `internet_headers = [...]`, nicht, ob die Liste in
die Nutzlast kommt. Alle Tests, die den Weg benutzten, ersetzten
`send_via_graph` per monkeypatch. Hier läuft die Funktion echt durch, nur der
HTTP-Aufruf ist abgefangen.
"""
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import graph_reinject  # noqa: E402
import loop_detector  # noqa: E402

MAIL = (b"From: max@firma.de\r\n"
        b"To: kunde@extern.de\r\n"
        b"Subject: Hallo\r\n"
        b"Message-ID: <nutzlast-1@firma.de>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b"<html><body><p>Hallo</p></body></html>\r\n")


class _Antwort:
    status_code = 202
    text = ""

    @staticmethod
    def json():
        return {}


@pytest.fixture
def gesendet(monkeypatch):
    aufrufe = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None, **k):
            aufrufe.append({"url": url, "json": json})
            return _Antwort()

    monkeypatch.setattr(graph_reinject.graph_client, "_acquire_token", lambda: "tok")
    monkeypatch.setattr(graph_reinject.httpx, "Client", _Client)
    graph_reinject._sendmail_dedup.clear()
    yield aufrufe
    graph_reinject._sendmail_dedup.clear()


def test_sendmail_nutzlast_traegt_den_schleifenmarker(gesendet):
    assert graph_reinject.send_via_graph("max@firma.de", ["kunde@extern.de"], MAIL) is True
    sendmail = [a for a in gesendet if a["url"].endswith("/sendMail")]
    assert len(sendmail) == 1, gesendet
    koepfe = sendmail[0]["json"]["message"].get("internetMessageHeaders") or []
    assert {"name": loop_detector.header_name(), "value": loop_detector.marker_wert()} in koepfe, (
        f"X-Sig-Applied fehlt in der sendMail-Nutzlast: {koepfe} — die Regel "
        "schickte die Mail endlos zurück ans Gateway")
