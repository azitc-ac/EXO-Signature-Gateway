"""Ein Rücklauf des eigenen Graph-Versands darf nicht still verworfen werden.

ANLASS (15.11.2026, gemessen)
-----------------------------
Graph sendMail aus einem GETEILTEN Postfach verwirft alle eigenen X-Köpfe,
auch X-Sig-Applied. Die Transportregel schickt die Mail dann erneut ans
Gateway: gleiche Message-ID, gleiche Empfänger. Die Duplikat-Sperre hielt das
für ein Duplikat, meldete „OK“, und die Mail war verloren.

Merkmal: Der Rücklauf ist eine neue Einlieferung bei Exchange und trägt eine
andere Network-Message-Id. Ein echtes Duplikat (dieselbe Einlieferung
nochmals) trägt dieselbe.
"""
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import graph_reinject  # noqa: E402
import held_mails  # noqa: E402
import stats  # noqa: E402


def _mail(nmid: str, zusatz: str = "") -> bytes:
    return (f"From: rechnungen@firma.de\r\n"
            f"To: partner@example.org\r\n"
            f"Subject: Test\r\n"
            f"Message-ID: <abc123@firma.de>\r\n"
            f"X-MS-Exchange-CrossTenant-Network-Message-Id: {nmid}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            f"Inhalt{zusatz}\r\n").encode()


class _Liste(list):
    status: list


class _Antwort:
    status_code = 202
    text = ""

    def __init__(self, status=202):
        self.status_code = status

    @staticmethod
    def json():
        return {}


@pytest.fixture
def umgebung(monkeypatch):
    gesendet, geparkt = _Liste(), []
    status = [202]

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **k):
            gesendet.append(url)
            return _Antwort(status[0])

    monkeypatch.setattr(graph_reinject.graph_client, "_acquire_token", lambda: "tok")
    monkeypatch.setattr(graph_reinject.httpx, "Client", _Client)
    monkeypatch.setattr(held_mails, "hold",
                        lambda absender, an, roh, **k: geparkt.append((absender, an, roh, k)) or "id")
    monkeypatch.setattr(stats, "increment", lambda *a, **k: None)
    monkeypatch.setattr(graph_reinject, "_sendmail_dedup", {})
    gesendet.status = status
    return gesendet, geparkt


@pytest.mark.parametrize("weg", ["send_via_graph_mime", "send_via_graph"])
def test_ruecklauf_wird_geparkt(umgebung, weg):
    gesendet, geparkt = umgebung
    senden = getattr(graph_reinject, weg)
    erste = _mail("aaaa-1111")
    assert senden("rechnungen@firma.de", ["partner@example.org"], erste)
    assert len(gesendet) == 1
    # Rücklauf: neue Einlieferung, zweiter Durchlauf (doppelt bearbeitet)
    assert senden("rechnungen@firma.de", ["partner@example.org"],
                  _mail("bbbb-2222", " + Banner nochmal"))
    assert len(gesendet) == 1, "Rücklauf darf nicht erneut gesendet werden (Schleife)"
    assert len(geparkt) == 1, "Rücklauf wurde still verworfen statt geparkt"
    absender, an, roh, k = geparkt[0]
    assert k.get("reason") == "ruecklauf"
    assert roh == erste, "geparkt werden muss die Fassung des ERSTEN Durchlaufs"
    assert an == ["partner@example.org"]


@pytest.mark.parametrize("weg", ["send_via_graph_mime", "send_via_graph"])
def test_echtes_duplikat_bleibt_uebersprungen(umgebung, weg):
    gesendet, geparkt = umgebung
    senden = getattr(graph_reinject, weg)
    m = _mail("aaaa-1111")
    assert senden("rechnungen@firma.de", ["partner@example.org"], m)
    assert senden("rechnungen@firma.de", ["partner@example.org"], m)
    assert len(gesendet) == 1 and not geparkt


def test_getrennte_forks_gehen_beide(umgebung):
    gesendet, geparkt = umgebung
    m = _mail("aaaa-1111")
    assert graph_reinject.send_via_graph_mime("rechnungen@firma.de", ["partner@example.org"], m)
    assert graph_reinject.send_via_graph_mime("rechnungen@firma.de", ["kollege@firma.de"], m)
    assert len(gesendet) == 2 and not geparkt


def test_gescheiterter_versand_sperrt_wiederholung_nicht(umgebung):
    """Scheitert der erste Versand, ist die Wiederholung (gleiche Einlieferung,
    z.B. aus der Warteschlange) kein Duplikat — sie muss wirklich gesendet werden."""
    gesendet, geparkt = umgebung
    m = _mail("aaaa-1111")
    gesendet.status[0] = 500
    assert not graph_reinject.send_via_graph_mime("rechnungen@firma.de", ["partner@example.org"], m)
    gesendet.status[0] = 202
    assert graph_reinject.send_via_graph_mime("rechnungen@firma.de", ["partner@example.org"], m)
    assert len(gesendet) == 2, "Wiederholung nach Fehlschlag wurde als Duplikat übersprungen"
