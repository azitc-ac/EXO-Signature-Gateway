"""Probelauf (Signatur-Vorschau im Webmail): Was der Handler festhält, hält
ein Probelauf nicht fest.

Die Stellen tragen `@probelauf.ohne_wirkung` am Eingang, nicht die Aufrufe im Handler — so
deckt eine Prüfung jeden Aufrufer ab. Kommt eine neue festhaltende Stelle
dazu, gehört sie hier hinein; den Durchlauf durch den echten Handler prüft
tests/test_notbetrieb_webmail.py (test_vorschau_haelt_nichts_fest).
"""
from __future__ import annotations

import ast
import inspect
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import held_mails  # noqa: E402
import mail_audit  # noqa: E402
import probelauf  # noqa: E402
import relay_stats  # noqa: E402
import sig_thread  # noqa: E402
import stats  # noqa: E402

FESTHALTENDE = [(stats, "increment"), (mail_audit, "log_event"), (sig_thread, "merken"),
                (held_mails, "hold"), (relay_stats, "merke"), (relay_stats, "merke_routing")]


@pytest.mark.parametrize("modul,name", FESTHALTENDE, ids=lambda x: getattr(x, "__name__", x))
def test_stelle_traegt_die_wache(modul, name):
    """Am Eingang: `@probelauf.ohne_wirkung(…)` — und sie wirkt wirklich
    (die gerufene Funktion ist die Wache, nicht das Original)."""
    funktion = getattr(modul, name)
    baum = ast.parse(textwrap.dedent(inspect.getsource(funktion)))
    dekoratoren = [ast.unparse(d) for d in baum.body[0].decorator_list]
    assert any(d.startswith("probelauf.ohne_wirkung(") for d in dekoratoren), dekoratoren
    assert funktion.__wrapped__ is not None and funktion.__code__ is not funktion.__wrapped__.__code__


def test_zaehler_bleibt_im_probelauf_stehen(monkeypatch):
    spur = []
    monkeypatch.setattr(stats, "_persist_total", lambda: spur.append("gesamt"))
    monkeypatch.setattr(stats, "_increment_daily", lambda k: spur.append(k))
    vorher = dict(stats._stats)
    with probelauf.an():
        stats.increment("probe_zaehler")
    assert spur == [] and stats._stats == vorher
    stats.increment("probe_zaehler")                 # Gegenprobe: ohne Probelauf zählt er
    assert spur == ["gesamt", "probe_zaehler"]
    stats._stats.pop("probe_zaehler", None)


def test_markierung_gilt_nur_im_block():
    assert not probelauf.aktiv()
    with probelauf.an():
        assert probelauf.aktiv()
    assert not probelauf.aktiv()
