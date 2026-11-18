"""Der Kopien-Stand (tools/kopien_stand.json) darf nur mit Begründung steigen.

ANLASS (2026-11-15)
-------------------
kopiencheck lässt in der CI nur einen ZUWACHS gegenüber dem Stand scheitern.
Den Stand selbst schreibt `--schreiben` beliebig neu — er wurde nach seiner
Einführung fünfmal angehoben, die Begründung stand jeweils nur als Prosa in
der Commit-Nachricht. Erzwungen hat sie nichts.

Jetzt: Steigt ein Wert gegenüber der vorherigen Fassung, muss die neue Fassung
in `begruendungen` einen Eintrag dafür tragen:

    {"datei": "app/webui/templates/x.html", "von": 0, "auf": 60, "grund": "…"}

Geprüft wird jedes Paar aufeinanderfolgender Fassungen seit Einführung der
Regel (erkennbar am Schlüssel `begruendungen`), dazu die ungespeicherte Fassung
im Arbeitsbaum gegen HEAD. Braucht die volle Git-Historie (CI: fetch-depth 0).

Die Gegenrichtung — ein Rückgang, der nicht festgeschrieben wird — lässt
kopiencheck selbst scheitern (Exit 1 seit v1.12.24).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
STAND = "tools/kopien_stand.json"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=WURZEL, capture_output=True, text=True,
                          check=True).stdout


def _fassungen() -> list[tuple[str, dict]]:
    """(Bezeichnung, Inhalt) — neueste zuerst, bis zur Einführung der Regel."""
    assert _git("rev-parse", "--is-shallow-repository").strip() == "false", (
        "flacher Klon: der Kopien-Stand braucht die Historie (actions/checkout fetch-depth: 0)")
    out = [("Arbeitsbaum", json.loads((WURZEL / STAND).read_text(encoding="utf-8")))]
    for commit in _git("log", "--format=%H", "--", STAND).split():
        inhalt = json.loads(_git("show", f"{commit}:{STAND}"))
        out.append((commit[:7], inhalt))
        if "begruendungen" not in inhalt:
            break                              # Fassung vor der Regel: Ausgangspunkt
    return out


def anstiege_ohne_grund(neu: dict, alt: dict, nachgetragen: list | None = None) -> list[str]:
    """`nachgetragen`: Begründungen aus SPÄTEREN Fassungen. Ein vergessener
    Eintrag lässt sich so im nächsten Commit nachholen, ohne die Historie
    umzuschreiben (v1.12.64 → v1.12.65)."""
    gruende = (neu.get("begruendungen") or []) + list(nachgetragen or [])
    fehlt = []
    for datei, n in neu["tokens_in_kopien"].items():
        vorher = alt["tokens_in_kopien"].get(datei, 0)
        if n <= vorher:
            continue
        if not any(g.get("datei") == datei and g.get("auf", 0) >= n and (g.get("grund") or "").strip()
                   for g in gruende):
            fehlt.append(f"{datei}: {vorher} → {n}")
    return fehlt


def test_stand_traegt_begruendungen():
    stand = json.loads((WURZEL / STAND).read_text(encoding="utf-8"))
    assert isinstance(stand.get("begruendungen"), list), (
        "Schlüssel `begruendungen` fehlt — ohne ihn gilt die Regel nicht")


def test_kein_anstieg_ohne_begruendung():
    fassungen = _fassungen()
    funde = []
    for i, ((name_neu, neu), (name_alt, alt)) in enumerate(zip(fassungen, fassungen[1:])):
        if "begruendungen" not in neu:
            break
        spaeter = [g for _, f in fassungen[:i] for g in (f.get("begruendungen") or [])]
        for f in anstiege_ohne_grund(neu, alt, spaeter):
            funde.append(f"{name_alt} → {name_neu}: {f}")
    assert not funde, ("Kopien-Stand gestiegen ohne Eintrag in `begruendungen` "
                       "({datei, von, auf, grund}):\n" + "\n".join(funde))


def test_pruefung_erkennt_anstieg():
    alt = {"tokens_in_kopien": {"a.html": 10}}
    assert anstiege_ohne_grund({"tokens_in_kopien": {"a.html": 12}, "begruendungen": []}, alt)
    assert anstiege_ohne_grund({"tokens_in_kopien": {"b.html": 60}, "begruendungen": []}, alt)
    mit = {"tokens_in_kopien": {"a.html": 12},
           "begruendungen": [{"datei": "a.html", "von": 10, "auf": 12, "grund": "gewollt"}]}
    assert not anstiege_ohne_grund(mit, alt)
    leer = {"tokens_in_kopien": {"a.html": 12},
            "begruendungen": [{"datei": "a.html", "von": 10, "auf": 12, "grund": " "}]}
    assert anstiege_ohne_grund(leer, alt)
