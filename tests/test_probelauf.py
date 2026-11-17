"""Probelauf (Signatur-Vorschau im Webmail): Was der Handler festhält, hält
ein Probelauf nicht fest.

Die Stellen sind die Eingänge der Module, nicht die Aufrufe im Handler — so
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
def test_stelle_fragt_den_probelauf_zuerst(modul, name):
    """Die erste Anweisung nach dem Docstring: `if probelauf.aktiv(): return`."""
    baum = ast.parse(textwrap.dedent(inspect.getsource(getattr(modul, name))))
    rumpf = baum.body[0].body
    if isinstance(rumpf[0], ast.Expr) and isinstance(getattr(rumpf[0], "value", None), ast.Constant):
        rumpf = rumpf[1:]
    wenn = next(s for s in rumpf if not isinstance(s, ast.Import))
    assert isinstance(wenn, ast.If) and ast.unparse(wenn.test) == "probelauf.aktiv()", ast.unparse(wenn)[:80]
    assert isinstance(wenn.body[0], ast.Return)


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
