"""Herzschlag des Scheduler-Fadens (scheduler.stillstand_min, v1.12.197).

Alle Hintergrundaufgaben laufen in EINEM Faden. Hängt er, fielen Nachstellen
zurückgehaltener Post, Statusprüfung, Regelprüfung und Zertifikatsprüfungen
zugleich und still aus.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import aufgaben
import scheduler
from webui import deps


def test_stillstand_erst_nach_der_grenze(monkeypatch):
    monkeypatch.setattr(scheduler, "_takt_zuletzt", 1000.0)
    assert scheduler.stillstand_min(1000.0 + 59 * 60) is None
    assert scheduler.stillstand_min(1000.0 + 75 * 60) == 75
    monkeypatch.setattr(scheduler, "_takt_zuletzt", None)
    assert scheduler.stillstand_min(10 ** 9) is None          # nie gestartet: kein Band


def test_jeder_durchlauf_setzt_den_herzschlag():
    quelle = inspect.getsource(scheduler._loop)
    assert quelle.index("_takt_zuletzt = time.monotonic()") < quelle.index("try:")


def _seite(monkeypatch, steht):
    monkeypatch.setattr(aufgaben, "offene", lambda: [])
    g = deps.templates.env.globals
    for name, wert in (("wartung_gemeldet", None), ("waechter_gemeldet", None),
                       ("lernmodus_rest_min", 0), ("bypass_gemeldet", None),
                       ("exo_abgewiesen_gemeldet", None)):
        monkeypatch.setitem(g, name, lambda w=wert: w)
    monkeypatch.setitem(g, "zustellfehler_stand", lambda: {"wiederholt": 0, "aufgegeben": 0})
    monkeypatch.setitem(g, "scheduler_steht", lambda: steht)
    req = SimpleNamespace(state=SimpleNamespace(user_role="admin", angemeldet=True),
                          url=SimpleNamespace(path="/"))
    return deps.templates.env.get_template("base.html").render(request=req, active="")


def test_band_bei_stillstand(monkeypatch):
    html = _seite(monkeypatch, 75)
    assert 'id="scheduler-banner"' in html and "seit 75 Minuten" in html
    assert 'id="scheduler-banner"' not in _seite(monkeypatch, None)
