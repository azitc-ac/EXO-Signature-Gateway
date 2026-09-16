"""Domänenliste der Sende-Identitäten: zwei Cache-Ebenen (Speicher + Platte).

Der EXO-Abruf dauert ~30–60 s. Damit das Domänen-Dropdown nicht bei jedem
Neustart wieder wartet, wird die Liste auf der Platte gecacht. Diese Tests MÜSSEN
fehlschlagen, wenn:
  - ein vorhandener Cache dennoch EXO befragt (die Verzögerung, die wir vermeiden),
  - ein gescheiterter Neu-Abruf den letzten guten Stand verwirft.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import setup_wizard


def test_platten_cache_vermeidet_zweiten_exo_abruf(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_wizard, "_DOMAENEN_DATEI", tmp_path / "d.json")
    monkeypatch.setattr(setup_wizard, "_domaenen_cache", None)
    calls = {"n": 0}

    def fake_ps(_body):
        calls["n"] += 1
        return {"ok": True, "domaenen": ["main.zarenko.net", "azitc.eu"],
                "default_exo": "z.onmicrosoft.com"}
    monkeypatch.setattr(setup_wizard, "_run_verify_ps", fake_ps)

    r1 = setup_wizard.list_accepted_domains()               # 1. Abruf → EXO + Platte
    assert r1["domaenen"] == ["main.zarenko.net", "azitc.eu"]
    assert calls["n"] == 1
    assert (tmp_path / "d.json").exists()

    monkeypatch.setattr(setup_wizard, "_domaenen_cache", None)   # Speicher-Cache leeren
    r2 = setup_wizard.list_accepted_domains()               # 2. Abruf → Platte, KEIN EXO
    assert r2["domaenen"] == ["main.zarenko.net", "azitc.eu"]
    assert calls["n"] == 1, "der Platten-Cache hätte einen zweiten EXO-Abruf sparen müssen"


def test_gescheiterter_refresh_behaelt_letzten_guten_stand(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_wizard, "_DOMAENEN_DATEI", tmp_path / "d.json")
    monkeypatch.setattr(setup_wizard, "_domaenen_cache", None)
    monkeypatch.setattr(setup_wizard, "_run_verify_ps",
                        lambda _b: {"ok": True, "domaenen": ["azitc.eu"], "default_exo": ""})
    setup_wizard.list_accepted_domains()                   # guten Stand auf die Platte legen

    monkeypatch.setattr(setup_wizard, "_domaenen_cache", None)
    monkeypatch.setattr(setup_wizard, "_run_verify_ps",
                        lambda _b: {"ok": False, "error": "EXO down"})
    r = setup_wizard.list_accepted_domains(refresh=True)   # Neu-Abruf scheitert
    assert r["ok"] is True
    assert r["domaenen"] == ["azitc.eu"]                    # Rückfall auf die Platte, kein Verlust
