"""Gespiegelte Dateien Gateway ↔ EXO SMTP Relay — wenn das Relay daneben liegt.

Die Liste, WAS gespiegelt ist, führt das Relay (tools/driftcheck.py dort);
das Gateway liest sie von da. Liegt kein Relay-Klon neben dem Gateway, wird
übersprungen — in der CI checkt der Workflow ihn aus.
"""
import subprocess
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "tools"))

import driftcheck  # noqa: E402


def test_relay_spiegelung():
    if not (driftcheck.RELAY / "tools" / "driftcheck.py").is_file():
        pytest.skip("kein Relay-Klon neben dem Gateway")
    r = subprocess.run([sys.executable, str(WURZEL / "tools" / "driftcheck.py"),
                        "--gateway-only", "--relay", str(driftcheck.RELAY)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "identisch mit dem Relay" in r.stdout


def test_ohne_relay_nur_hinweis(tmp_path):
    """Fehlt der Baum, ist das kein Fehler — sonst wäre jeder Lauf ohne den
    zweiten Klon rot, und die Prüfung würde abgeschaltet statt gepflegt."""
    rep = driftcheck.Report()
    driftcheck.check_relay_mirrored(rep, tmp_path / "gibts-nicht")
    assert not rep.problems
    assert any("uebersprungen" in n for n in rep.notes)


def test_abweichung_wird_gemeldet(tmp_path):
    relay = tmp_path / "exo-smtp-relay"
    (relay / "tools").mkdir(parents=True)
    (relay / "tools" / "driftcheck.py").write_text(
        'MIRRORED = [("app/smtp_relay.py", "Regeln")]\n', encoding="utf-8")
    (relay / "app").mkdir()
    (relay / "app" / "smtp_relay.py").write_text("# andere Fassung\n", encoding="utf-8")
    rep = driftcheck.Report()
    driftcheck.check_relay_mirrored(rep, relay)
    assert len(rep.problems) == 1
    assert "weicht vom Relay ab" in rep.problems[0]
