"""Bypass-Wächter (Azure): Code bestehender Wächter aktualisierbar, Exchange-Sitzung
nach einem Fehler aufgeräumt (v1.11.64).

ANLASS (12.11.2026): Während einer Exchange-Störung brach im Wächter
Connect-ExchangeOnline ab. Danach meldete er bei jedem Lauf „Module could not be
correctly formed. Please run Connect-ExchangeOnline again.“ — er lebte, hätte die
Regel im Ausfall aber nicht schalten können. Der Fix steckt in run.ps1; bis
v1.11.63 lud das Gateway diesen Code aber NUR beim Anlegen hoch, bestehende
Wächter hätten ihn nie bekommen.
"""
import asyncio
import shutil
from pathlib import Path

import pytest

import abnahme
import aufgaben
import waechter_register
import watchdog_deploy

WURZEL = Path(__file__).resolve().parent.parent
RUN = WURZEL / "azure" / "watchdog" / "Watchdog" / "run.ps1"


# ── run.ps1: Sitzung aufräumen ─────────────────────────────────────────────────

def test_run_raeumt_vor_dem_verbinden_und_nach_fehlern_auf():
    s = RUN.read_text(encoding="utf-8")
    code = s.split("param($Timer)", 1)[1]
    assert "Get-Module -Name 'tmp_*'" in code and "Remove-Module -Force" in code
    vor = code.index("Clear-ExoSitzung\n    Connect-ExchangeOnline")
    assert vor > code.index("function Clear-ExoSitzung")
    fang = code.rsplit("} catch {", 1)[1]
    assert "Clear-ExoSitzung" in fang.split("Send-Heartbeat")[0]
    # Heartbeat läuft weiterhin in jedem Fall (nach dem try/catch).
    assert code.rstrip().endswith("Send-Heartbeat $healthy $bypassActive $exoError")


# ── Code-Stand ────────────────────────────────────────────────────────────────

def test_code_stand_folgt_dem_inhalt(tmp_path, monkeypatch):
    quelle = tmp_path / "watchdog"
    shutil.copytree(WURZEL / "azure" / "watchdog", quelle)
    monkeypatch.setattr(watchdog_deploy, "_FUNC_SRC", quelle)
    a = watchdog_deploy.code_stand()
    assert a == watchdog_deploy.code_stand()                    # stabil (keine Zeitstempel)
    (quelle / "Watchdog" / "run.ps1").write_text("# geändert\n", encoding="utf-8")
    assert watchdog_deploy.code_stand() != a


# ── Prüfung + Aufgabe ─────────────────────────────────────────────────────────

@pytest.fixture
def register(tmp_path, monkeypatch):
    monkeypatch.setattr(waechter_register, "PFAD", tmp_path / "watchdog_watchers.json")
    return waechter_register


def test_ohne_azure_waechter_entfaellt(register):
    register.registrieren(id="wd_c", name="cron", kind="cron", token_hash="h")
    assert abnahme._waechter_code()["zustand"] == abnahme.ENTFAELLT


def test_alter_oder_unbekannter_code_ist_offen(register):
    register.registrieren(id="wd_a", name="Alt", kind="azure", token_hash="h",
                          azure={"app_name": "a"})                    # vor v1.11.64: kein Stand
    p = abnahme._waechter_code()
    assert p["zustand"] == abnahme.OFFEN and "Alt" in p["befund"]
    register.merke_azure("wd_a", code_stand="0000000000000000")
    assert abnahme._waechter_code()["zustand"] == abnahme.OFFEN


def test_aktueller_code_ist_ok(register):
    register.registrieren(id="wd_a", name="A", kind="azure", token_hash="h",
                          azure={"app_name": "a", "code_stand": watchdog_deploy.code_stand()})
    assert abnahme._waechter_code()["zustand"] == abnahme.OK


def test_aufgabe_und_abnahme_teilen_die_pruefung():
    assert abnahme._waechter_code in abnahme.PRUEFUNGEN
    a = next(x for x in aufgaben.AUFGABEN if x.kennung == "waechter-code")
    assert a.pruefung is abnahme._waechter_code and a.seit == "1.11.64"


def test_anlegen_merkt_den_code_stand():
    """Neue Wächter bekommen den Stand beim Anlegen — sonst stünden sie sofort
    als veraltet da."""
    s = (WURZEL / "app" / "webui" / "routen" / "waechter.py").read_text(encoding="utf-8")
    deploy = s.split("async def _run_deploy", 1)[1].split("\nasync def ", 1)[0]
    assert "wd.deploy_code(" in deploy
    assert "merke_azure(wid, code_stand=wd.code_stand())" in deploy


# ── Route ─────────────────────────────────────────────────────────────────────

from test_watchdog_config import anlage  # noqa: E402,F401


def _azure(register, wid="wd_z"):
    register.registrieren(id=wid, name="Z", kind="azure", token_hash="h",
                          azure={"subscription": "sub-1", "resource_group": "rg-1",
                                 "app_name": "app-1"})


def test_aktualisieren_laedt_auf_genau_diese_function(anlage, monkeypatch):
    c, _ = anlage
    from webui.routen import waechter
    import keyvault
    _azure(waechter_register)
    aufrufe = []

    async def _deploy(sub, rg, app, token, zip_bytes=None):
        aufrufe.append((sub, rg, app, token))
        return True, "ok"
    monkeypatch.setattr(watchdog_deploy, "deploy_code", _deploy)
    monkeypatch.setattr(keyvault, "get_user_arm_token", lambda upn: "ARM")
    monkeypatch.setattr(waechter, "_get_session_user", lambda req: "admin@firma.de")
    r = c.post("/api/watchdog/code-aktualisieren", json={"id": "wd_z"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert aufrufe == [("sub-1", "rg-1", "app-1", "ARM")]
    assert waechter_register.holen("wd_z")["azure"]["code_stand"] == watchdog_deploy.code_stand()


def test_aktualisieren_scheitert_ohne_stand_zu_merken(anlage, monkeypatch):
    c, _ = anlage
    from webui.routen import waechter
    import keyvault
    _azure(waechter_register)

    async def _deploy(*a, **k):
        return False, "ZipDeploy (HTTP 401)"
    monkeypatch.setattr(watchdog_deploy, "deploy_code", _deploy)
    monkeypatch.setattr(keyvault, "get_user_arm_token", lambda upn: "ARM")
    monkeypatch.setattr(waechter, "_get_session_user", lambda req: "admin@firma.de")
    r = c.post("/api/watchdog/code-aktualisieren", json={"id": "wd_z"})
    assert r.status_code == 502 and "401" in r.json()["detail"]
    assert "code_stand" not in waechter_register.holen("wd_z")["azure"]


def test_aktualisieren_ohne_azure_login_400(anlage, monkeypatch):
    c, _ = anlage
    from webui.routen import waechter
    import keyvault
    _azure(waechter_register)
    monkeypatch.setattr(keyvault, "get_user_arm_token", lambda upn: None)
    monkeypatch.setattr(waechter, "_get_session_user", lambda req: "admin@firma.de")
    r = c.post("/api/watchdog/code-aktualisieren", json={"id": "wd_z"})
    assert r.status_code == 400 and "Azure-Zugriff" in r.json()["detail"]


def test_aktualisieren_nur_fuer_azure(anlage):
    c, _ = anlage
    waechter_register.registrieren(id="wd_cron", name="C", kind="cron", token_hash="h")
    assert c.post("/api/watchdog/code-aktualisieren", json={"id": "wd_cron"}).status_code == 400
    assert c.post("/api/watchdog/code-aktualisieren", json={"id": "fehlt"}).status_code == 404


# ── Verwaiste Wächter: abgewiesene Herzschläge sichtbar ───────────────────────

def test_ungueltiger_herzschlag_wird_gemeldet(anlage, caplog):
    import logging
    from webui.routen import waechter
    c, _ = anlage
    waechter._abgewiesen.clear()
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            r = c.post("/api/watchdog/heartbeat", headers={"X-Watchdog-Token": "falsch",
                                                           "X-Watchdog-Id": "wd_alt<x>"})
            assert r.status_code == 401
    warnungen = [m.getMessage() for m in caplog.records if "ungültigem Token" in m.getMessage()]
    assert len(warnungen) == 1                                  # höchstens einmal je Stunde
    assert "wd_altx" in warnungen[0] and "<" not in warnungen[0]
    st = c.get("/api/watchdog/status").json()
    assert st["abgewiesen"][0]["anzahl"] == 3 and st["abgewiesen"][0]["kennung"] == "wd_altx"


def test_ohne_token_keine_meldung(anlage):
    from webui.routen import waechter
    c, _ = anlage
    waechter._abgewiesen.clear()
    assert c.post("/api/watchdog/heartbeat").status_code == 401
    assert waechter._abgewiesen == {}
