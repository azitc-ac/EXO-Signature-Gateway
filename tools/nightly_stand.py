#!/usr/bin/env python3
"""Welcher main-Stand wird zum Nightly? — Auswahl für .github/workflows/nightly.yml.

Regel: der jüngste Commit auf main, dessen CI-Lauf (Push-Ereignis) grün war
und dessen Zeitstempel vor dem letzten Stichzeitpunkt 23:59 Europe/Berlin liegt.

Eingabe (stdin): Antwort von
  GET /repos/{repo}/actions/workflows/ci.yml/runs?branch=main&event=push
Ausgabe: die SHA, oder nichts (dann bleibt nightly, wo es ist).

Der Stichzeitpunkt wird hier berechnet, nicht über die Startzeit der Aktion:
GitHub startet geplante Läufe mit Verzögerung, und die Sommerzeit verschiebt
23:59 Berlin gegen UTC. Läuft die Aktion mehrfach, kommt dasselbe heraus.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
STICHZEIT = time(23, 59)


def stichzeitpunkt(jetzt: datetime) -> datetime:
    """Der letzte 23:59 (Berlin), der nicht nach `jetzt` liegt."""
    lokal = jetzt.astimezone(BERLIN)
    heute = datetime.combine(lokal.date(), STICHZEIT, tzinfo=BERLIN)
    return heute if heute <= lokal else heute - timedelta(days=1)


def _zeit(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def waehle(laeufe: list[dict], jetzt: datetime) -> str | None:
    grenze = stichzeitpunkt(jetzt)
    kandidaten = []
    for lauf in laeufe:
        if lauf.get("conclusion") != "success" or lauf.get("event") != "push":
            continue
        if lauf.get("head_branch") != "main":
            continue
        zeit = _zeit((lauf.get("head_commit") or {}).get("timestamp") or lauf["created_at"])
        if zeit <= grenze:
            kandidaten.append((zeit, lauf["head_sha"]))
    return max(kandidaten)[1] if kandidaten else None


def main() -> int:
    daten = json.load(sys.stdin)
    sha = waehle(daten.get("workflow_runs", []), datetime.now(BERLIN))
    if sha:
        print(sha)
    return 0


if __name__ == "__main__":
    sys.exit(main())
