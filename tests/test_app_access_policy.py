"""ApplicationAccessPolicy-Scope: Enumerator + automatische Pflege (Least Privilege)."""
from pathlib import Path

import setup_wizard
import settings_store


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
    s = (Path(__file__).resolve().parent.parent / "app" / "scripts"
         / "setup_app_access_policy.ps1").read_text()
    code = "\n".join(z for z in s.splitlines() if not z.lstrip().startswith("#"))
    assert "-BypassSecurityGroupManagerCheck" in code          # App-only ist nicht Manager
    assert "$desired.Count -eq 0" in code                      # Leer-Ziel → nicht leeren
    assert "$members_now.Count -eq 0" in code                  # leere Gruppe → keine Policy
    assert "New-ApplicationAccessPolicy" in code
    assert "RestrictAccess" in code
