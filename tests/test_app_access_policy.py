"""ApplicationAccessPolicy-Scope: Enumerator + automatische Pflege (Least Privilege)."""
import re
from pathlib import Path

import setup_wizard
import settings_store

SKRIPTE = Path(__file__).resolve().parent.parent / "app" / "scripts"


def _code(name: str) -> str:
    """Skripttext ohne Kommentare — auch ohne `<# … #>`-Blöcke, sonst bestünde
    eine Prüfung schon über die Beschreibung im Kopf des Skripts."""
    text = re.sub(r"<#.*?#>", "", (SKRIPTE / name).read_text("utf-8"), flags=re.S)
    return "\n".join(z for z in text.splitlines() if not z.lstrip().startswith("#"))


def test_app_scope_adressen_aktiv_plus_notification(monkeypatch):
    """Scope = aktive Postfächer (sig ODER smime) ∪ Notification; inaktive raus.
    ACME/CA fließt NICHT gesondert ein (aktive Postfächer decken das ab)."""
    data = {
        "MAILBOX_CONFIG": {
            "g1": {"sig": True,  "smime": False, "primary": "a@x.de"},
            "g2": {"sig": False, "smime": True,  "primary": "b@x.de"},
            "g3": {"sig": False, "smime": False, "primary": "off@x.de"},  # inaktiv
        },
        "CA_USER_CONFIG": {"acme@x.de": {}},   # darf NICHT auftauchen
        "NOTIFICATION_MAILBOX": "notif@x.de",
    }
    monkeypatch.setattr(settings_store, "get", lambda k, *a: data.get(k))
    import sende_identitaeten
    monkeypatch.setattr(sende_identitaeten, "liste", lambda: [])   # hermetisch
    assert setup_wizard._app_scope_adressen() == ["a@x.de", "b@x.de", "notif@x.de"]


def test_app_scope_enthaelt_sende_identitaeten(monkeypatch):
    """Die Shared-Mailbox-Adressen der Sende-Identitäten gehören in den Scope —
    sonst scheitert Graph `sendMail` „als" sie an der ApplicationAccessPolicy."""
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a: {"NOTIFICATION_MAILBOX": "notif@x.de"}.get(k))
    import sende_identitaeten
    monkeypatch.setattr(sende_identitaeten, "liste", lambda: [
        {"adresse": "Drucker.EG@Firma.de"},    # wird kleingeschrieben aufgenommen
        {"adresse": ""},                        # ohne Adresse (keine Domäne) → ignoriert
    ])
    assert setup_wizard._app_scope_adressen() == ["drucker.eg@firma.de", "notif@x.de"]


def test_sync_noop_wenn_nicht_aktiviert(monkeypatch):
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a: False if k == "APP_ACCESS_POLICY_ENABLED" else None)
    import subprocess
    called = {"sub": False}
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.update(sub=True))
    r = setup_wizard.run_app_access_policy_sync("app", "t.onmicrosoft.com")
    assert r["ok"] is True
    assert called["sub"] is False  # kein pwsh-Aufruf, solange nicht aktiviert


def test_ps_invarianten_ausfallsicher():
    code = _code("setup_app_access_policy.ps1")
    assert "-UmgehtManagerPruefung" in code                     # App-only ist nicht Manager
    assert "$desired.Count -eq 0" in code                      # Leer-Ziel → nicht leeren
    assert "$members_now.Count -eq 0" in code                  # leere Gruppe → keine Policy
    assert "New-ApplicationAccessPolicy" in code
    assert "RestrictAccess" in code


def test_scope_gruppe_nutzt_gemeinsamen_abgleich_nur_bei_ziel_liste():
    """Hunderte Mitglieder: kein eigener Einzelweg mehr, sondern Sync-DgMitglieder
    (Sammelaufruf ab vielen Änderungen) — mit Bypass, und NUR im Zweig mit
    nicht-leerer Ziel-Liste (der Abgleich würde sonst einzeln leeren = Deny-all)."""
    code = _code("setup_app_access_policy.ps1")
    assert ". (Join-Path $PSScriptRoot 'dg_mitglieder.ps1')" in code
    assert "Add-DistributionGroupMember" not in code, "eigener Einzelweg"
    assert "Remove-DistributionGroupMember" not in code, "eigener Einzelweg"
    aufruf = [z for z in code.splitlines() if "Sync-DgMitglieder" in z]
    assert len(aufruf) == 1, aufruf
    assert "-Identity $GroupName" in aufruf[0] and "-Soll $desired" in aufruf[0]
    assert "-UmgehtManagerPruefung" in aufruf[0]
    leer = code.index("if ($desired.Count -eq 0) {")
    sonst = code.index("} else {", leer)
    assert leer < sonst < code.index(aufruf[0]) < code.index("Get-ApplicationAccessPolicy")
    # Kein weiterer Aufruf zwischen Leer-Prüfung und else: der Leer-Zweig ruft nichts.
    assert "Sync-DgMitglieder" not in code[leer:sonst]


def test_abgleich_reicht_bypass_an_alle_drei_cmdlets():
    """-UmgehtManagerPruefung muss Update-, Add- UND Remove- erreichen — sonst
    scheitert genau der Rückfall auf den Einzelweg an der Manager-Prüfung."""
    code = _code("dg_mitglieder.ps1")
    assert "[switch]$UmgehtManagerPruefung" in code
    assert "if ($UmgehtManagerPruefung) { $umgehen['BypassSecurityGroupManagerCheck'] = $true }" in code
    for cmdlet in ("Update-DistributionGroupMember", "Add-DistributionGroupMember",
                   "Remove-DistributionGroupMember"):
        zeilen = [z for z in code.splitlines() if cmdlet + " -Identity" in z]
        assert len(zeilen) == 1, (cmdlet, zeilen)
        assert "@umgehen" in zeilen[0], (cmdlet, zeilen[0])


def test_scope_sync_zeitgrenze_waechst_mit_den_mitgliedern(monkeypatch, tmp_path):
    import subprocess
    grenzen = []

    def run(cmd, **kw):
        grenzen.append(kw.get("timeout"))

        class P:
            returncode, stdout, stderr = 0, "", ""
        return P()
    monkeypatch.setattr(subprocess, "run", run)
    pfx = tmp_path / "auth.pfx"
    pfx.write_bytes(b"x")
    monkeypatch.setattr(setup_wizard, "_AUTH_CERT_PATH", pfx)
    monkeypatch.setattr(setup_wizard, "Path", lambda p: tmp_path / Path(p).name)
    (tmp_path / "setup_app_access_policy.ps1").write_text("#")
    mitglieder = [f"p{i}@firma.at" for i in range(400)]
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a: True if k == "APP_ACCESS_POLICY_ENABLED" else None)
    monkeypatch.setattr(setup_wizard, "_app_scope_adressen", lambda: list(mitglieder))
    assert setup_wizard.run_app_access_policy_sync("app", "firma.onmicrosoft.com")["ok"]
    assert grenzen == [setup_wizard.dg_zeitlimit(180, 400)]
    assert grenzen[0] > 180
