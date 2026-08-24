"""Ein Port-Scanner darf die Fehlerstufe nicht unbrauchbar machen.

ANLASS (24.08.2026)
-------------------
Im Protokoll der Produktions-VM (24.07.–22.08.) waren **143 von 144 ERROR-Zeilen**
abgebrochene Fremdverbindungen, jede mit vollständigem Traceback, jede beim
TLS-Handshake. Wer nach einem Betriebsfehler suchte, fand 143 Scanner und einen
Befund — die Stufe ERROR war als Suchmerkmal wertlos.

Gefährlich war das nie: Die Quell-IP-Prüfung sitzt in `handler.handle_DATA`,
und diese Verbindungen brechen lange davor ab. Ein reines Diagnoseproblem — aber
eines, das jede Fehlersuche im Protokoll behindert.

Die Prüfung hier deckt beide Richtungen ab: Das Rauschen wird leiser, ein echter
Fehler bleibt ein Fehler. Der zweite Teil ist der wichtigere — ein Filter, der zu
viel schluckt, wäre schlimmer als das Rauschen.
"""
import asyncio
import logging
import ssl
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import smtp_rauschen  # noqa: E402


def _satz(fehler: BaseException | None,
          meldung: str = "%r SMTP session exception") -> logging.LogRecord:
    """Ein Datensatz, wie aiosmtpd ihn erzeugt (smtp.py: log.exception(...))."""
    r = logging.LogRecord("mail.log", logging.ERROR, "smtp.py", 608,
                          meldung, (("203.0.113.9", 61000),),
                          (type(fehler), fehler, None) if fehler else None)
    return r


@pytest.fixture
def leiser():
    return smtp_rauschen.AbbruchLeiser()


@pytest.mark.parametrize("fehler", [
    ssl.SSLError("handshake failure"),
    ConnectionResetError("peer went away"),
    BrokenPipeError(),
    TimeoutError(),
    asyncio.IncompleteReadError(b"", 4),
])
def test_abbruch_wird_leiser(leiser, fehler):
    r = _satz(fehler)
    assert leiser.filter(r) is True, "die Zeile bleibt — sie wird nur leiser"
    assert r.levelno == logging.INFO
    assert r.levelname == "INFO"
    assert r.exc_info is None, "kein Traceback für einen abgebrochenen Handshake"
    assert r.exc_text is None, (
        "exc_text muss mit weg — sonst hängt ein bereits formatierter Traceback "
        "trotzdem an der Ausgabe.")


def test_gegenstelle_bleibt_erhalten(leiser):
    """Ohne die Adresse liesse sich ein Angriff nicht von Rauschen trennen —
    genau deshalb ein Filter statt des Handler-Hooks, der sie nicht kennt."""
    r = _satz(ssl.SSLError("x"))
    leiser.filter(r)
    assert "203.0.113.9" in (r.msg % r.args)


def test_echter_fehler_bleibt_fehler(leiser):
    """Der wichtigere Teil: Ein Filter, der zu viel schluckt, ist schlimmer als
    das Rauschen, das er beseitigen soll."""
    r = _satz(ValueError("kaputte Kopfzeile"))
    assert leiser.filter(r) is True
    assert r.levelno == logging.ERROR, "unbeteiligte Ausnahmen bleiben ERROR"
    assert r.exc_info is not None, "und behalten ihren Traceback"


def test_fremde_meldung_bleibt_unangetastet(leiser):
    """Der Filter hängt am Wortlaut von aiosmtpd. Trifft er nicht, darf er auch
    nichts anfassen — dann ist es wieder laut, aber nichts geht verloren."""
    r = _satz(ssl.SSLError("x"), meldung="%r irgendetwas anderes")
    assert leiser.filter(r) is True
    assert r.levelno == logging.ERROR


def test_ohne_ausnahme_unveraendert(leiser):
    """`log.error(...)` ohne exc_info ist kein Verbindungsabbruch."""
    r = _satz(None)
    assert leiser.filter(r) is True
    assert r.levelno == logging.ERROR


def test_filter_haengt_am_logger():
    """Die Verdrahtung selbst — sonst ist der Filter fertig und wirkungslos.

    Gesucht wird die Zeile in `main.py`; ein Test, der nur die Klasse prüft,
    hätte den vergessenen `addFilter()` nicht bemerkt.
    """
    quelle = (WURZEL / "app" / "main.py").read_text("utf-8")
    assert "smtp_rauschen.AbbruchLeiser()" in quelle
    assert 'getLogger("mail.log").addFilter' in quelle
