"""Bypass-Banner: „Erneut prüfen“ statt Ausblenden; ausblendbare Hinweise.

Nach dem Einrichtungsassistenten stand das Banner auf einem veralteten
Regelzustand (v1.10.76). Ein Ausblenden-Knopf hätte auch einen echten Bypass
weggeklickt; „Erneut prüfen“ liest die Regel live und lässt das Banner nur
stehen, wenn es stimmt.
"""
import asyncio
import json
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import waechter_state  # noqa: E402
import waechter_regel  # noqa: E402
from webui import deps  # noqa: E402
from webui.routen import waechter as route  # noqa: E402

BASE = (WURZEL / "app/webui/templates/base.html").read_text(encoding="utf-8")


def _mit_waechter(monkeypatch, tmp_path):
    monkeypatch.setattr(waechter_state, "PFAD", tmp_path / "s.json")
    monkeypatch.setattr(deps.settings_store, "get",
                        lambda k, *a, **kw: True if k == "WATCHDOG_ENABLED" else None)


def _aufruf():
    antwort = asyncio.run(route.watchdog_regel_pruefen(user="admin"))
    return json.loads(antwort.body)


def test_fehlalarm_verschwindet_nach_pruefung(monkeypatch, tmp_path):
    _mit_waechter(monkeypatch, tmp_path)
    waechter_state.merge(rule_state="Disabled", rule_checked="2026-11-06T23:03:59Z")
    assert deps._bypass_gemeldet()["geprueft"] == "06.11.2026 23:03 UTC"

    def lesen():
        waechter_state.merge(rule_state="Enabled", rule_checked="2026-11-07T10:00:00Z")
        return {"state": "Enabled", "sent_to_scope": ""}
    monkeypatch.setattr(waechter_regel, "regel_lesen", lesen)
    assert _aufruf() == {"ok": True, "rule_state": "Enabled", "bypass": False}


def test_echter_bypass_bleibt(monkeypatch, tmp_path):
    _mit_waechter(monkeypatch, tmp_path)
    waechter_state.merge(rule_state="Disabled", rule_checked="2026-11-06T23:03:59Z")
    monkeypatch.setattr(waechter_regel, "regel_lesen",
                        lambda: waechter_state.merge(rule_state="Disabled") and
                        {"state": "Disabled", "sent_to_scope": ""})
    assert _aufruf()["bypass"] is True


def test_unlesbare_regel_meldet_fehler(monkeypatch, tmp_path):
    _mit_waechter(monkeypatch, tmp_path)
    monkeypatch.setattr(waechter_regel, "regel_lesen", lambda: None)
    d = _aufruf()
    assert d["ok"] is False and d["error"]


def test_banner_hat_pruefknopf_und_kein_ausblenden():
    start = BASE.index('id="bypass-banner"')
    banner = BASE[start:BASE.index("</script>", start)]
    assert "bypassPruefen()" in banner and "/api/watchdog/regel-pruefen" in banner
    assert "data-ausblendbar" not in banner, "Alarm darf nicht ausblendbar sein"


def test_ausblendbare_hinweise():
    assert "document.querySelectorAll('[data-ausblendbar]')" in BASE
    smime = (WURZEL / "app/webui/templates/settings_smime.html").read_text(encoding="utf-8")
    assert 'data-ausblendbar="smime-regeln-erprobt"' in smime


def test_update_dienst_heisst_nicht_watcher():
    for name in ("setup.html", "backup.html", "advanced.html"):
        text = (WURZEL / "app/webui/templates" / name).read_text(encoding="utf-8")
        for alt in ("Watcher aktiv", "Update-Watcher-Service</span>", "an Watcher übergeben",
                    "Erfordert den Watcher-Service", ">Update-Watcher</a>"):
            assert alt not in text, (name, alt)
