"""Eine Auswahlliste zeigt nur, was sie kennt — also muss sie den Katalog holen.

ANLASS (19.08.2026): Im Auswahlfeld „Zertifizierungsstelle" fehlten Anbieter.
Nichts war kaputt: Der Anbieterkatalog liegt in einem Zwischenspeicher, den der
Zeitplaner füllt, und ein frisch gestarteter Prozess hat ihn noch nicht.
`list_backends()` meldete daraufhin wahrheitsgemäss nur die örtlichen
Bezugswege — in einer Auswahlliste sieht das aber nicht nach „noch nicht
geladen" aus, sondern nach „gibt es nicht".

Zwei von drei Stellen frischten nicht auf. Die dritte tat es von Hand, mit
eigenem try/except. Deshalb jetzt EIN Einstieg (`list_backends_aktuell()`) und
diese Wache, damit es nicht wieder auseinanderläuft.
"""
import ast
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

# Hier darf `list_backends()` roh benutzt werden — es ist die Quelle selbst.
ERLAUBT = {"app/ca_backends/registry.py"}


def test_niemand_umgeht_den_frischen_einstieg():
    """⚠️ Wer `list_backends()` direkt aufruft, zeigt womöglich eine halbe Liste."""
    treffer = []
    for datei in sorted((WURZEL / "app").rglob("*.py")):
        rel = str(datei.relative_to(WURZEL))
        if rel in ERLAUBT:
            continue
        quelle = datei.read_text("utf-8")
        for n in ast.walk(ast.parse(quelle)):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "list_backends"):
                treffer.append(f"{rel}:{n.lineno}")
    assert not treffer, (
        "Diese Stellen holen die Bezugswege ohne frischen Katalog. "
        "`await ca_backends.list_backends_aktuell()` benutzen:\n  "
        + "\n  ".join(treffer))


def test_der_einstieg_frischt_wirklich_auf(monkeypatch):
    import asyncio
    import ca_backends
    import hub_catalog

    gerufen = []

    async def _refresh():
        gerufen.append(True)
    monkeypatch.setattr(hub_catalog, "refresh", _refresh)
    monkeypatch.setattr(ca_backends.registry, "list_backends", lambda: [{"name": "x"}])
    aus = asyncio.run(ca_backends.list_backends_aktuell())
    assert gerufen, "Katalog wurde nicht aufgefrischt"
    assert aus == [{"name": "x"}]


def test_ein_nicht_erreichbarer_hub_liefert_den_letzten_stand(monkeypatch):
    """⚠️ Kein Abbruch: Der letzte bekannte Stand ist besser als eine leere
    Auswahl — und eine Ausnahme mitten im Seitenaufbau wäre eine Fehlerseite,
    wo eine unvollständige Liste genügt hätte."""
    import asyncio
    import ca_backends
    import hub_catalog

    async def _kaputt():
        raise RuntimeError("Hub nicht erreichbar")
    monkeypatch.setattr(hub_catalog, "refresh", _kaputt)
    monkeypatch.setattr(ca_backends.registry, "list_backends", lambda: [{"name": "castle_acme"}])
    assert asyncio.run(ca_backends.list_backends_aktuell()) == [{"name": "castle_acme"}]
