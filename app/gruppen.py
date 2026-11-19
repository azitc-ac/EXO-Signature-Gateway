"""Interne Gruppen — EINE Quelle für alle Leser.

`INTERNAL_GROUPS = {name: [config_key, …]}` hält die vom Betreiber gepflegten
Gruppen. Dazu kommt die vordefinierte Gruppe „Alle Postfächer", deren Mitglieder
NICHT gespeichert, sondern bei jedem Zugriff aus `MAILBOX_CONFIG` berechnet werden
— ein neu aktiviertes Postfach ist damit sofort Mitglied, ohne dass jemand die
Gruppe pflegt.

⚠️ Wer Gruppen liest, ruft `interne_gruppen()` — nie `settings_store.get(
"INTERNAL_GROUPS")` direkt (sonst fehlt „Alle Postfächer"; geprüft von
tests/test_gruppen.py).

Dynamische Gruppen (seit v1.12.98): Steht der Name zusätzlich in
`DYNAMIC_GROUPS`, berechnet `dynamische_gruppen.mitglieder()` die Mitglieder aus
Regel, Entra-Schnappschuss und „immer“/„nie“; der gespeicherte Eintrag in
`INTERNAL_GROUPS` hält dann nur die Reihenfolge. Leser merken davon nichts —
sie bekommen hier wie bisher {name: [config_key, …]}.
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
    dyn = settings_store.get("DYNAMIC_GROUPS") or {}
    berechnet: dict = {}
    if dyn and isinstance(dyn, dict) and isinstance(mc, dict):
        import dynamische_gruppen
        berechnet = dynamische_gruppen.mitglieder(
            {n: m for n, m in gespeichert.items() if not ist_berechnet(n)}, dyn, mc)
    return _zusammensetzen(gespeichert, berechnet, mc)


def interne_gruppen_aus(daten: dict) -> dict[str, list[str]]:
    """`interne_gruppen()` aus einem Einstellungs-Wörterbuch — für Leser, die
    den Speicher nicht befragen dürfen (Migration in `settings_store`, vor
    `init()`).

    Dynamische Gruppen über `dynamische_gruppen.mitglieder_lesend`: dieselbe
    Berechnung wie `mitglieder()` (Schnappschuss, sonst letzter Stand), aber
    ohne deren Seiteneffekte — kein `settings_store.revision()` (vor `init()`
    nicht aufrufbar), kein Stand-Schreiben, kein Audit."""
    gespeichert = daten.get("INTERNAL_GROUPS")
    gespeichert = gespeichert if isinstance(gespeichert, dict) else {}
    mc = daten.get("MAILBOX_CONFIG")
    dyn = daten.get("DYNAMIC_GROUPS")
    berechnet: dict = {}
    if dyn and isinstance(dyn, dict) and isinstance(mc, dict):
        import dynamische_gruppen
        berechnet = dynamische_gruppen.mitglieder_lesend(
            {n: m for n, m in gespeichert.items() if not ist_berechnet(n)}, dyn, mc)
    return _zusammensetzen(gespeichert, berechnet, mc)


def _zusammensetzen(gespeichert, berechnet: dict, mc) -> dict[str, list[str]]:
    gespeichert = gespeichert if isinstance(gespeichert, dict) else {}
    ergebnis = {name: list(berechnet[name] if name in berechnet else (mitglieder or []))
                for name, mitglieder in gespeichert.items()
                if not ist_berechnet(name)}
    ergebnis[ALLE] = list(mc.keys()) if isinstance(mc, dict) else []
    return ergebnis


def dynamische_namen() -> list[str]:
    """Namen der dynamischen Gruppen, in Gruppenreihenfolge. Eine Definition
    ohne Gruppe (verwaist) zählt nicht."""
    dyn = settings_store.get("DYNAMIC_GROUPS") or {}
    if not isinstance(dyn, dict) or not dyn:
        return []
    gespeichert = settings_store.get("INTERNAL_GROUPS") or {}
    return [n for n in gespeichert if n in dyn and not ist_berechnet(n)]


def nur_gespeicherte(gruppen: dict) -> dict:
    """Für den Schreibweg: berechnete Gruppen nie persistieren."""
    return {name: mitglieder for name, mitglieder in (gruppen or {}).items()
            if not ist_berechnet(name)}
