"""Schreibt das Gateway den Betreff um, zieht Thread-Topic mit.

Exchange nimmt Thread-Topic als Unterhaltungsthema, wenn der Betreff darauf
ENDET, und behandelt alles davor wie „AW:“ (MS-OXCMAIL, Conversation Topic /
Normalizing the Subject). Gemessen 11.11.2026: „[signiert von …] Betreff“ mit
altem Thread-Topic „Betreff“ — OWA zeigte den Tag nicht. Thread-Topic SOLL
der Betreff ohne Präfix sein.
"""
import email
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import handler  # noqa: E402


def _msg(betreff, thema=None):
    kopf = f"Subject: {betreff}\r\n" + (f"Thread-Topic: {thema}\r\n" if thema else "")
    return email.message_from_string(kopf + "\r\nx\r\n")


@pytest.mark.parametrize("neu,thema", [
    ("[signiert von erika@x.de] Angebot", "[signiert von erika@x.de] Angebot"),
    ("AW: Angebot", "Angebot"),
    ("WG: Angebot", "Angebot"),
    ("[verschlüsselt] AW: Angebot", "[verschlüsselt] AW: Angebot"),
    ("Angebot", "Angebot"),
])
def test_thread_topic_folgt_dem_betreff(neu, thema):
    m = _msg("Angebot", "Angebot")
    handler._betreff_setzen(m, neu)
    assert handler._decode_subject(m["Subject"]) == neu
    assert handler._decode_subject(m["Thread-Topic"]) == thema
    assert len(m.get_all("Thread-Topic")) == 1


def test_ohne_thread_topic_wird_keiner_angelegt():
    m = _msg("Angebot")
    handler._betreff_setzen(m, "[signiert von a] Angebot")
    assert "Thread-Topic" not in m


def test_eingangs_tag_mit_altem_thread_topic(monkeypatch):
    """Der beobachtete Fall: vorangestellter Tag, Betreff endete auf Thread-Topic."""
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: None)
    m = _msg("Rueckmeldung Angebot", "Rueckmeldung Angebot")
    handler._apply_subject_tag(m, "[signiert von erika@zarenko.net]", "Rueckmeldung Angebot")
    betreff = handler._decode_subject(m["Subject"])
    thema = handler._decode_subject(m["Thread-Topic"])
    assert betreff == "[signiert von erika@zarenko.net] Rueckmeldung Angebot"
    # Exchange-Regel nachgebildet: endet der Betreff auf dem Thema und ist das
    # Präfix nicht leer, wird der Tag zum „Präfix“ und verschwindet aus der Ansicht.
    assert not (betreff.endswith(thema) and betreff != thema)
