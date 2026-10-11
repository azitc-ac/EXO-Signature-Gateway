"""Reihenfolge der Karten auf der SMTP-Relay-Seite.

Die Kernfunktion des Relays — Geräte einliefern lassen — steht oben:
Statistik, Lernmodus, Geräte, Abgewiesen, Protokoll. Sende-Identitäten,
Domänen-Routing und Hybrid-Koexistenz sind Erweiterungen und gehören dahinter.
Sie waren schon einmal nach oben gerutscht und mussten auf Wunsch wieder ans
Ende (2026-10-12); deshalb hält dieser Test die Reihenfolge fest.
"""
import re
from pathlib import Path

SEITE = Path(__file__).resolve().parent.parent / "app" / "webui" / "templates" / "relay.html"


def test_erweiterungen_stehen_hinter_den_kernkarten():
    karten = re.findall(r"^<!-- ── ([^─]+?) ─", SEITE.read_text(encoding="utf-8"), re.M)
    karten = [k.split(" (")[0].strip() for k in karten]
    assert karten == ["Statistik", "Lernmodus", "Geräte", "Abgewiesen", "Protokoll",
                      "Sende-Identitäten", "Domänen-Routing", "Hybrid-Koexistenz"], karten
