"""Reihenfolge der Karten auf der SMTP-Relay-Seite.

Die Kernfunktion des Relays — Geräte einliefern lassen — steht oben:
Statistik, Lernmodus, Geräte, Abgewiesen, Protokoll. Sende-Identitäten sind
eine Erweiterung und gehören dahinter. Domänen-Routing, Direktversand und
Hybrid-Koexistenz stehen seit v1.12.18 unter Einstellungen → Mail-Routing.
Sie waren schon einmal nach oben gerutscht und mussten auf Wunsch wieder ans
Ende (2026-10-12); deshalb hält dieser Test die Reihenfolge fest.
"""
import re
from pathlib import Path

VORLAGEN = Path(__file__).resolve().parent.parent / "app" / "webui" / "templates"
SEITE = VORLAGEN / "relay.html"


def _karten(datei):
    karten = re.findall(r"^<!-- ── ([^─]+?) ─", (VORLAGEN / datei).read_text(encoding="utf-8"), re.M)
    return [k.split(" (")[0].strip() for k in karten]


def test_erweiterungen_stehen_hinter_den_kernkarten():
    karten = _karten("relay.html")
    assert karten == ["Statistik", "Lernmodus", "Geräte", "Abgewiesen", "Protokoll",
                      "Sende-Identitäten"], karten


def test_routing_karten_stehen_unter_einstellungen():
    """Die Einrichtung des Routings ist eine Einstellung, keine Relay-Funktion."""
    assert _karten("settings_routing.html") == ["Domänen-Routing", "Direktversand",
                                                "Hybrid-Koexistenz"]
