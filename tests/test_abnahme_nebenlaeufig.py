"""Abnahme: Prüfpunkte nebeneinander, Weboberfläche bleibt ansprechbar (v1.12.128).

Gemessen 20.11.2026 (Raspi): 41,5 s für 39 Punkte, davon 33 s in zwei Punkten,
die je eine PowerShell-Sitzung zu Exchange aufbauen. Die Route rief bericht()
direkt in einer async-Funktion — die ganze Oberfläche (auch Add-in, Self-Service)
stand so lange still."""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import abnahme  # noqa: E402


def _langsam(name, dauer=0.4):
    def f():
        time.sleep(dauer)
        return abnahme._punkt(name, abnahme.OK, "")
    f.__name__ = "_" + name
    return f


def test_pruefpunkte_laufen_nebeneinander_reihenfolge_bleibt(monkeypatch):
    def kaputt():
        raise RuntimeError("Exchange weg")
    monkeypatch.setattr(abnahme, "PRUEFUNGEN", (_langsam("a"), _langsam("b"), kaputt, _langsam("c", 0.1)))
    t = time.monotonic()
    r = abnahme.bericht()
    dauer = time.monotonic() - t
    assert dauer < 0.75, dauer                      # nacheinander wären es 0,9 s
    assert [p["name"] for p in r["punkte"]] == ["a", "b", "Kaputt", "c"]
    assert r["punkte"][2]["zustand"] == abnahme.UNBEKANNT and r["bereit"] is False


def test_route_blockiert_die_ereignisschleife_nicht(monkeypatch):
    from webui.routen import betrieb
    monkeypatch.setattr(abnahme, "bericht", lambda: (time.sleep(0.5), {"punkte": []})[1])

    async def ablauf():
        ticks = 0

        async def zaehlen():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1
        z = asyncio.create_task(zaehlen())
        await betrieb.api_abnahme("admin")
        z.cancel()
        return ticks
    assert asyncio.run(ablauf()) >= 5               # Schleife lief während der Prüfung weiter
