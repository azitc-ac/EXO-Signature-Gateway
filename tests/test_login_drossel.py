"""Login-Drosselung: nach Fehlversuchen greift ein Backoff, Erfolg setzt zurück.

Die Uhr wird gestellt (`_jetzt`), damit der Test nicht an echter Zeit hängt.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import login_drossel as ld  # noqa: E402


def _uhr(monkeypatch, start=1000.0):
    box = [start]
    monkeypatch.setattr(ld, "_jetzt", lambda: box[0])
    ld._FEHLER.clear()
    return box


def test_erste_versuche_sind_frei(monkeypatch):
    _uhr(monkeypatch)
    k = "1.2.3.4"
    for _ in range(ld._FREI):
        ld.fehlversuch(k)
    assert ld.sperr_sekunden(k) == 0.0
    assert not ld.gesperrt(k)


def test_backoff_ab_ueberschreitung(monkeypatch):
    _uhr(monkeypatch)
    k = "1.2.3.4"
    for _ in range(ld._FREI + 1):
        ld.fehlversuch(k)
    assert ld.gesperrt(k), "nach Überschreiten der Freigrenze muss gesperrt sein"


def test_sperre_laeuft_ab(monkeypatch):
    box = _uhr(monkeypatch)
    k = "1.2.3.4"
    for _ in range(ld._FREI + 1):
        ld.fehlversuch(k)
    assert ld.gesperrt(k)
    box[0] += ld._DECKEL + 1          # weit über die längstmögliche Sperre
    assert not ld.gesperrt(k)


def test_erfolg_setzt_zurueck(monkeypatch):
    _uhr(monkeypatch)
    k = "1.2.3.4"
    for _ in range(ld._FREI + 3):
        ld.fehlversuch(k)
    assert ld.gesperrt(k)
    ld.erfolg(k)
    assert ld.sperr_sekunden(k) == 0.0


def test_getrennte_schluessel_stoeren_sich_nicht(monkeypatch):
    _uhr(monkeypatch)
    for _ in range(ld._FREI + 1):
        ld.fehlversuch("1.1.1.1")
    assert ld.gesperrt("1.1.1.1")
    assert not ld.gesperrt("2.2.2.2")
