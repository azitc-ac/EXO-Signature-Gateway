"""Interne Gruppen — EINE Quelle für alle Leser.

`INTERNAL_GROUPS = {name: [config_key, …]}` hält die vom Betreiber gepflegten
Gruppen. Dazu kommt die vordefinierte Gruppe „Alle Postfächer", deren Mitglieder
NICHT gespeichert, sondern bei jedem Zugriff aus `MAILBOX_CONFIG` berechnet werden
— ein neu aktiviertes Postfach ist damit sofort Mitglied, ohne dass jemand die
Gruppe pflegt.

⚠️ Wer Gruppen liest, ruft `interne_gruppen()` — nie `settings_store.get(
"INTERNAL_GROUPS")` direkt (sonst fehlt „Alle Postfächer"; geprüft von
tests/test_gruppen.py).
"""
from __future__ import annotations

import settings_store

ALLE = "Alle Postfächer"


def ist_berechnet(name: str) -> bool:
    """Wird die Gruppe berechnet (nicht gespeichert, nicht bearbeitbar)?"""
    return name == ALLE


def interne_gruppen() -> dict[str, list[str]]:
    """{gruppenname: [config_key, …]} inklusive „Alle Postfächer".

    „Alle Postfächer" steht ZULETZT: Bei first-match-wins über die Gruppen-
    Reihenfolge (Gruppen-Variablen) gewinnt so jede gezielte Gruppe vor der
    allgemeinen. Eine gespeicherte Gruppe gleichen Namens wird ignoriert — die
    berechnete Mitgliedschaft hat Vorrang.
    """
    gespeichert = settings_store.get("INTERNAL_GROUPS") or {}
    mc = settings_store.get("MAILBOX_CONFIG") or {}
    ergebnis = {name: list(mitglieder or []) for name, mitglieder in gespeichert.items()
                if not ist_berechnet(name)}
    ergebnis[ALLE] = list(mc.keys()) if isinstance(mc, dict) else []
    return ergebnis


def nur_gespeicherte(gruppen: dict) -> dict:
    """Für den Schreibweg: berechnete Gruppen nie persistieren."""
    return {name: mitglieder for name, mitglieder in (gruppen or {}).items()
            if not ist_berechnet(name)}
