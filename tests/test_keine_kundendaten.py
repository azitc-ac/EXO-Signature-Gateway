"""Keine vertraulichen Begriffe (Kundendaten) im öffentlichen Repository — in der CI.

Der commit-msg-Hook prüft lokal gegen ~/.config/exo-gateway/vertraulich.txt.
Er lässt sich umgehen (--no-verify, Bearbeitung im Browser, Rechner ohne
Liste — dann warnt er nur). Dieser Test prüft dasselbe in der CI, ohne dass
ein Begriff im Repository steht: tests/vertraulich_sperrliste.json enthält nur
geschlüsselte Hashes (tools/vertraulich_sperrliste.py erklärt, warum ein
einfacher Hash hier nicht genügt).

Geprüft werden alle versionierten Textdateien und die Nachricht des letzten
Commits. Ohne Schlüssel: in der CI ein Fehler, lokal übersprungen.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "tools"))

import vertraulich_sperrliste as vs  # noqa: E402

SPERRLISTE = WURZEL / "tests" / "vertraulich_sperrliste.json"


def _schluessel() -> bytes | None:
    roh = os.environ.get("VERTRAULICH_SCHLUESSEL", "").strip()
    if roh:
        return vs.schluessel_hex(roh)
    if vs.SCHLUESSEL.is_file():
        return vs.schluessel_hex(vs.SCHLUESSEL.read_text())
    return None


def funde(text: str, schluessel: bytes, kennungen: set[str], wortzahlen: list[int]) -> list[int]:
    """Positionen (Wortindex) aller Wortfolgen, deren Kennung gesperrt ist.
    Jede Folge wird je Aufruf nur einmal gehasht (Zwischenspeicher je Text, nicht
    global — global wären es über eine Million Einträge)."""
    w = vs.woerter(text)
    gesehen: dict[str, bool] = {}
    out = []
    for n in wortzahlen:
        for i in range(len(w) - n + 1):
            folge = " ".join(w[i:i + n])
            treffer = gesehen.get(folge)
            if treffer is None:
                treffer = gesehen[folge] = vs.kennung(schluessel, folge) in kennungen
            if treffer:
                out.append(i)
    return out


@pytest.fixture(scope="module")
def sperre():
    k = _schluessel()
    if k is None:
        if os.environ.get("CI") == "true":
            pytest.fail("VERTRAULICH_SCHLUESSEL fehlt in der CI — Secret setzen: "
                        "python3 tools/vertraulich_sperrliste.py --secret")
        pytest.skip("kein Schlüssel (lokal): ~/.config/exo-gateway/vertraulich.schluessel")
    d = json.loads(SPERRLISTE.read_text(encoding="utf-8"))
    return k, set(d["kennungen"]), d["wortzahlen"]


def test_keine_kundendaten_in_dateien(sperre):
    k, kennungen, zahlen = sperre
    dateien = subprocess.run(["git", "ls-files"], cwd=WURZEL, capture_output=True,
                             text=True, check=True).stdout.splitlines()
    treffer = []
    for name in dateien:
        if name == "tests/vertraulich_sperrliste.json":
            continue
        try:
            text = (WURZEL / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        if funde(text, k, kennungen, zahlen):
            # Nur die Zeilennummern — der Begriff selbst gehört nicht ins CI-Protokoll.
            zeilen = [nr for nr, z in enumerate(text.splitlines(), 1)
                      if funde(z, k, kennungen, zahlen)]
            treffer.append(f"{name}: Zeile {', '.join(map(str, zeilen[:10]))}")
    assert not treffer, ("Vertraulicher Begriff (Kundendaten) im Repository — durch neutrale "
                         "Werte ersetzen (Max Mustermann, firma.at, 10.99.0.x):\n" + "\n".join(treffer))


def test_keine_kundendaten_in_der_letzten_commit_nachricht(sperre):
    k, kennungen, zahlen = sperre
    nachricht = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=WURZEL,
                               capture_output=True, text=True, check=True).stdout
    assert not funde(nachricht, k, kennungen, zahlen), "vertraulicher Begriff in der Commit-Nachricht"


def test_pruefung_findet_begriff_in_jeder_schreibweise():
    k = bytes(range(32))
    gesperrt = {vs.kennung(k, "beispiel kunde"), vs.kennung(k, "firma at")}
    for text in ("Bei Beispiel-Kunde GmbH", "BEISPIEL KUNDE", "mail@firma.at", "x firma.AT y"):
        assert funde(text, k, gesperrt, [1, 2]), text
    assert not funde("Beispielkunde und firmaat", k, gesperrt, [1, 2])
