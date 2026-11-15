"""Grenze für übersprungene Tests — ein Skip ist in der CI ein Fehler.

ANLASS (2026-11-15)
-------------------
Ein Test, der sich überspringt, sieht im Ergebnis aus wie ein bestandener: Der
Lauf ist grün. 15 Module hängen an `pytest.importorskip("starlette.testclient")`
— fehlte httpx in der Lock-Datei, verschwänden sie alle still, darunter die
Rechteprüfung (test_rollen.py). Die Browser-Tests liefen bis 13.11. aus genau
diesem Grund nie. Ein Netz, das sich still abmeldet, hält nichts.

Mit `SKIP_GRENZE=1` (in der CI gesetzt) scheitert der Lauf an jedem Skip, der
nicht in ERLAUBT steht. Lokal bleibt ein Skip ein Skip: Dort fehlt mitunter
node, ein Relay-Klon oder ein Browser, und das soll niemanden am Testen hindern.

Eingebunden über tests/conftest.py (pluginmanager.register), damit es auch in
einem Unterprozess mit `-p skip_grenze` wirkt — so prüft es test_skip_grenze.py.
"""
from __future__ import annotations

import os
from pathlib import Path

# Datei → Grund. Gilt NICHT, wenn BROWSER_PFLICHT=1 (Browser-Job): dort darf
# kein Browser-Test fehlen.
ERLAUBT_OHNE_BROWSER = {
    "test_ui_browser.py": "Browser-Tests laufen im eigenen CI-Job mit BROWSER_PFLICHT=1",
    "test_mobil_alle_seiten.py": "Browser-Tests laufen im eigenen CI-Job mit BROWSER_PFLICHT=1",
}

_uebersprungen: list[tuple[str, str]] = []


def erlaubt() -> dict[str, str]:
    return {} if os.environ.get("BROWSER_PFLICHT") == "1" else ERLAUBT_OHNE_BROWSER


def fremde(liste: list[tuple[str, str]], erlaubt_: dict[str, str]) -> list[tuple[str, str]]:
    return [(n, g) for n, g in liste if Path(n.split("::")[0]).name not in erlaubt_]


def _grund(report) -> str:
    lr = report.longrepr
    if isinstance(lr, tuple) and len(lr) == 3:
        return str(lr[2])
    return str(lr)[:200]


def pytest_collectreport(report):
    if report.skipped:
        _uebersprungen.append((report.nodeid, _grund(report)))


def pytest_runtest_logreport(report):
    # xfail zählt mit: auch ein erwarteter Fehlschlag prüft nichts.
    if report.skipped:
        _uebersprungen.append((report.nodeid, _grund(report)))


def pytest_sessionfinish(session, exitstatus):
    if os.environ.get("SKIP_GRENZE") != "1":
        return
    zu_viel = fremde(_uebersprungen, erlaubt())
    if not zu_viel:
        return
    zeilen = "\n".join(f"  {n}: {g}" for n, g in zu_viel)
    tr = session.config.pluginmanager.get_plugin("terminalreporter")
    text = ("SKIP_GRENZE: übersprungene Tests ausserhalb der begründeten Liste "
            f"(tests/skip_grenze.py):\n{zeilen}")
    if tr:
        tr.write_line("")
        tr.write_line(text, red=True, bold=True)
    else:
        print(text)
    session.exitstatus = 1
