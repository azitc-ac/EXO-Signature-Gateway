"""Keine async-Route ruft eine Funktion, die PowerShell (oder einen anderen
Unterprozess) startet, direkt auf — sonst steht die ganze Oberfläche (auch
Add-in und Self-Service) für die Dauer der Exchange-Sitzung still.

Anlass 20.11.2026: /api/abnahme rief abnahme.bericht() direkt (41 s auf dem
Raspi), ebenso drei Einrichtungsschritte in setup.py. Erlaubt ist der Aufruf
über asyncio.to_thread / run_in_executor (dort steht die Funktion als Wert oder
in einem lambda, nicht als Aufruf)."""
import ast
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
# Module, deren Funktionen Unterprozesse starten (pwsh) oder Exchange-Regeln lesen.
MODULE = ["exo_mailboxes", "health_check", "exo_mailusers", "setup_wizard", "waechter_regel",
          "regel_kennung", "regel_bedingung", "abnahme", "regel_split", "regel_probe"]
MERKMALE = ("pwsh", "ps_regelskript", "_run_ps", "subprocess.run")


def _blockierende() -> set[tuple[str, str]]:
    namen = set()
    for m in MODULE:
        p = APP / f"{m}.py"
        if not p.exists():
            continue
        quelle = p.read_text(encoding="utf-8")
        for f in ast.parse(quelle).body:
            if isinstance(f, ast.FunctionDef):
                if any(x in (ast.get_source_segment(quelle, f) or "") for x in MERKMALE):
                    namen.add((m, f.name))
    return namen


def _direkte_aufrufe(knoten):
    """Aufrufe im Rumpf, ohne in lambdas oder verschachtelte Funktionen abzusteigen."""
    for kind in ast.iter_child_nodes(knoten):
        if isinstance(kind, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(kind, ast.Call):
            yield kind
        yield from _direkte_aufrufe(kind)


def test_blockierende_liste_ist_nicht_leer():
    namen = _blockierende()
    assert ("setup_wizard", "run_exo_connector_setup") in namen
    assert ("regel_kennung", "pruefen") in namen or ("waechter_regel", "ps_regelskript") in namen


def test_keine_async_route_ruft_powershell_direkt():
    blockierend = _blockierende() | {("abnahme", "bericht")}
    treffer = []
    dateien = sorted((APP / "webui" / "routen").glob("*.py")) + [APP / "webui" / "app.py"]
    for p in dateien:
        baum = ast.parse(p.read_text(encoding="utf-8"))
        for fn in ast.walk(baum):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            for c in _direkte_aufrufe(fn):
                f = c.func
                if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                        and (f.value.id.lstrip("_"), f.attr) in blockierend:
                    treffer.append(f"{p.name}:{c.lineno} {fn.name}() ruft {f.value.id}.{f.attr}() direkt")
    assert not treffer, "Mit asyncio.to_thread auslagern:\n" + "\n".join(treffer)


def test_einrichtungsschritt_laeuft_nicht_doppelt(monkeypatch):
    """Review 20.11.: Mit to_thread liefen zwei Klicks gleichzeitig an denselben
    Exchange-Regeln. Der zweite bekommt jetzt 409."""
    import asyncio
    import sys
    import time
    sys.path.insert(0, str(APP))
    import settings_store
    import setup_wizard
    from fastapi import HTTPException
    from webui.routen import setup as route
    werte = {"CLIENT_ID": "x", "TENANT_DOMAIN": "firma.at"}
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))
    laeufe = []

    def langsam(**kw):
        laeufe.append(1)
        time.sleep(0.4)
        return {"ok": True, "output": "ok"}
    monkeypatch.setattr(setup_wizard, "run_smime_rules_setup", langsam)

    async def zwei():
        class R:
            async def json(self): return {}
        return await asyncio.gather(route.api_setup_smime_rules(R(), "admin"),
                                    route.api_setup_smime_rules(R(), "admin"), return_exceptions=True)
    erg = asyncio.run(zwei())
    assert len(laeufe) == 1
    assert sum(isinstance(e, HTTPException) and e.status_code == 409 for e in erg) == 1
