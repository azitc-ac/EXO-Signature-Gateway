"""Post einer Sende-Identität geht über Graph — unabhängig vom reinject_mode.

Der Vorteil (Exchange sendet aus dem eigenen Haus → SPF/DKIM/DMARC ohne Gateway-IP
im SPF) hängt daran, dass Identitätspost NICHT dem allgemeinen Rückweg folgt. Diese
Tests MÜSSEN fehlschlagen, wenn:
  - Identitätspost trotz `smtp`-Modus über den Smarthost statt Graph ginge,
  - der Abschalter `IDENT_DELIVER_VIA_GRAPH=False` ignoriert würde,
  - ein Graph-Fehler NICHT auf den allgemeinen Rückweg zurückfiele (Verlustgefahr).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import reinject
import graph_reinject
import sende_identitaeten
import settings_store
import exo_mailboxes
import stats

_ROH = b"Subject: Scan\r\nFrom: drucker.eg@firma.de\r\n\r\nAnbei.\r\n"
_IDENT = "drucker.eg@firma.de"


@pytest.fixture
def welt(monkeypatch):
    """Modus `smtp`, eine Identitätsadresse, Graph+Smarthost als Attrappen."""
    monkeypatch.setattr(settings_store, "reinject_mode", lambda: "smtp")
    monkeypatch.setattr(exo_mailboxes, "known_addresses", lambda: set())
    monkeypatch.setattr(sende_identitaeten, "ist_identitaets_adresse",
                        lambda a: (a or "").lower() == _IDENT)
    monkeypatch.setattr(stats, "increment", lambda *a, **k: None)
    # Getrennt zählen: mime = Raw-MIME (send_via_graph_mime), json = Rekonstruktion.
    ruf = {"mime": [], "json": [], "smtp": []}
    monkeypatch.setattr(graph_reinject, "send_via_graph",
                        lambda mf, rc, cb: (ruf["json"].append((mf, tuple(rc))) or True))
    monkeypatch.setattr(graph_reinject, "send_via_graph_mime",
                        lambda mf, rc, cb: (ruf["mime"].append((mf, tuple(rc))) or True))
    monkeypatch.setattr(reinject, "_send_smtp",
                        lambda mf, rc, cb: ruf["smtp"].append((mf, tuple(rc))))
    return ruf


def _setting(monkeypatch, an: bool):
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a, **kw: (an if k == "IDENT_DELIVER_VIA_GRAPH" else None))


def test_identitaetspost_geht_ueber_graph_als_raw_mime(welt, monkeypatch):
    _setting(monkeypatch, True)
    reinject.send(_IDENT, ["kunde@extern.de"], _ROH)
    # Über Graph — und zwar Raw-MIME, NICHT die JSON-Rekonstruktion (sonst TNEF/
    # winmail.dat bzw. weiß in Outlook Classic).
    assert welt["mime"] == [(_IDENT, ("kunde@extern.de",))]
    assert welt["json"] == []
    assert welt["smtp"] == []                    # NICHT über den Smarthost


def test_abschalter_laesst_identitaetspost_dem_modus_folgen(welt, monkeypatch):
    _setting(monkeypatch, False)                 # Notnagel: aus
    reinject.send(_IDENT, ["kunde@extern.de"], _ROH)
    assert welt["mime"] == [] and welt["json"] == []   # kein Graph-Zwang
    assert welt["smtp"] == [(_IDENT, ("kunde@extern.de",))]


def test_fremde_post_bleibt_beim_modus(welt, monkeypatch):
    _setting(monkeypatch, True)
    reinject.send("normal@firma.de", ["kunde@extern.de"], _ROH)   # keine Identität
    assert welt["mime"] == [] and welt["json"] == []
    assert welt["smtp"] == [("normal@firma.de", ("kunde@extern.de",))]


def test_graph_fehler_faellt_auf_den_modus_zurueck(welt, monkeypatch):
    _setting(monkeypatch, True)
    # Identitätspost geht über den Raw-MIME-Weg → diesen scheitern lassen.
    monkeypatch.setattr(graph_reinject, "send_via_graph_mime", lambda mf, rc, cb: False)
    reinject.send(_IDENT, ["kunde@extern.de"], _ROH)
    # Graph versucht (und gescheitert) → Rückfall auf den Smarthost, kein Verlust.
    assert welt["smtp"] == [(_IDENT, ("kunde@extern.de",))]
