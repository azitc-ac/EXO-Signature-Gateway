"""Sichtbarkeit abgewiesener Anmeldungen.

Diese Tests MÜSSEN fehlschlagen, wenn:
  - das Delta-Modell (seit letztem Bericht) nicht mehr verbrauchend zählt,
  - die Quellen (web/587/port25) vermischt werden,
  - die jüngsten Ereignisse nicht neueste-zuerst kommen.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import anmelde_ereignisse as ae


@pytest.fixture(autouse=True)
def frisch(monkeypatch):
    # Jeder Test startet mit leeren Zählern (Modul-Zustand ist prozessweit).
    monkeypatch.setattr(ae, "_recent", ae.deque(maxlen=200))
    monkeypatch.setattr(ae, "_gesamt", {})
    monkeypatch.setattr(ae, "_snapshot", {})
    yield


def test_merke_und_stand_je_quelle():
    ae.merke("web", "1.1.1.1", "falsch")
    ae.merke("web", "1.1.1.1", "falsch")
    ae.merke("587", "2.2.2.2", "abgewiesen")
    assert ae.stand() == {"web": 2, "587": 1}          # port25 fehlt (0 → nicht gelistet)


def test_letzte_neueste_zuerst():
    ae.merke("web", "1.1.1.1", "a")
    ae.merke("587", "2.2.2.2", "b")
    letzte = ae.letzte(10)
    assert letzte[0]["quelle"] == "587" and letzte[0]["ip"] == "2.2.2.2"
    assert letzte[1]["quelle"] == "web"


def test_seit_letztem_bericht_verbraucht_wie_snapshot():
    ae.merke("web", "1.1.1.1")
    ae.merke("port25", "3.3.3.3")
    erste = ae.seit_letztem_bericht()
    assert erste == {"web": 1, "port25": 1}
    # Sofort erneut: nichts Neues → leer (nicht dieselbe Zahl doppelt melden).
    assert ae.seit_letztem_bericht() == {}
    # Danach ein weiterer Versuch → nur dieser im nächsten Delta.
    ae.merke("web", "1.1.1.1")
    assert ae.seit_letztem_bericht() == {"web": 1}


def test_stand_ist_nicht_verbrauchend():
    ae.merke("587", "2.2.2.2")
    assert ae.stand() == {"587": 1}
    assert ae.stand() == {"587": 1}                    # mehrfach lesbar, unverändert


def test_labels_decken_alle_quellen_ab():
    for q in ae.QUELLEN:
        assert q in ae.LABELS
