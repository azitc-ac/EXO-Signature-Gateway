"""Die Verteilerliste für Benachrichtigungen und externe Absender.

Exchange setzt an einer neuen Verteilerliste RequireSenderAuthenticationEnabled
auf $true. Mail von außerhalb des Tenants wird dann mit 550 5.7.133 abgewiesen —
und zwar lautlos: Der absendende Dienst bekommt eine Annahmebestätigung, die
Liste erhält nichts. Ein vertauschtes Vorzeichen an dieser Stelle wäre deshalb
nicht am Verhalten des Gateways zu bemerken, sondern nur daran, dass irgendwo
eine erwartete Mail ausbleibt. Darum diese Tests.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))


def _skript(monkeypatch, tmp_path, accept_external):
    """Führt run_notification_dg_update aus und gibt das erzeugte PS-Skript zurück."""
    import setup_wizard
    import settings_store

    cert = tmp_path / "auth.pfx"
    cert.write_bytes(b"x")
    monkeypatch.setattr(setup_wizard, "_AUTH_CERT_PATH", cert)
    monkeypatch.setattr(setup_wizard.config, "CLIENT_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a: {"TENANT_DOMAIN": "example.onmicrosoft.com"}.get(k, ""))

    gesehen = {}

    class _Proc:
        returncode = 0
        stdout = '{"ok":true,"email":"dg@example.onmicrosoft.com"}'
        stderr = ""

    def _run(cmd, **kw):
        gesehen["skript"] = cmd[-1]
        return _Proc()

    monkeypatch.setattr(setup_wizard.subprocess, "run", _run)
    ergebnis = setup_wizard.run_notification_dg_update(["a@example.com"], accept_external)
    assert ergebnis["ok"], ergebnis
    return gesehen["skript"]


def test_haken_gesetzt_oeffnet_die_liste(monkeypatch, tmp_path):
    s = _skript(monkeypatch, tmp_path, True)
    assert "-RequireSenderAuthenticationEnabled $false" in s, (
        "Mit gesetztem Haken muss die Liste externe Absender annehmen:\n" + s)


def test_haken_nicht_gesetzt_schliesst_die_liste(monkeypatch, tmp_path):
    s = _skript(monkeypatch, tmp_path, False)
    assert "-RequireSenderAuthenticationEnabled $true" in s, (
        "Ohne Haken muss die Voreinstellung von Exchange wiederhergestellt werden:\n" + s)


def test_vorgabe_ist_geschlossen(monkeypatch, tmp_path):
    """Wer die Funktion ohne das Argument ruft, darf die Liste nicht öffnen."""
    import setup_wizard
    cert = tmp_path / "auth.pfx"
    cert.write_bytes(b"x")
    monkeypatch.setattr(setup_wizard, "_AUTH_CERT_PATH", cert)
    monkeypatch.setattr(setup_wizard.config, "CLIENT_ID", "1")
    import settings_store
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a: {"TENANT_DOMAIN": "example.onmicrosoft.com"}.get(k, ""))
    gesehen = {}

    class _Proc:
        returncode = 0
        stdout = '{"ok":true,"email":"x@y.z"}'
        stderr = ""

    monkeypatch.setattr(setup_wizard.subprocess, "run",
                        lambda cmd, **kw: (gesehen.__setitem__("s", cmd[-1]), _Proc())[1])
    setup_wizard.run_notification_dg_update([])
    assert "-RequireSenderAuthenticationEnabled $true" in gesehen["s"]


def test_gesetzt_wird_bei_jedem_lauf_nicht_nur_beim_anlegen(monkeypatch, tmp_path):
    """Sonst liesse sich ein einmal gesetzter Haken nicht mehr zurücknehmen.

    Set-DistributionGroup muss ausserhalb des `if (-not $dg)`-Zweigs stehen.
    """
    s = _skript(monkeypatch, tmp_path, False)
    vor_dem_anlegen = s.index("if (-not $dg)")
    setzen = s.index("Set-DistributionGroup")
    zweig_ende = s.index("$membersStr")
    assert setzen > vor_dem_anlegen
    assert setzen < zweig_ende
    # Der Aufruf darf nicht innerhalb des Anlege-Zweigs eingerueckt sein
    zeile = [z for z in s.splitlines() if "Set-DistributionGroup" in z][0]
    assert not zeile.startswith(" "), (
        "Set-DistributionGroup steht im Anlege-Zweig — dann wirkt der Haken nur "
        "bei einer neuen Liste:\n" + zeile)


def test_einstellung_hat_eine_vorgabe():
    """Ohne Eintrag in DEFAULTS faellt die Vorlage auf Undefined zurueck und der
    Haken zeigt dauerhaft 'nicht gesetzt', egal was in EXO gilt."""
    import settings_store
    assert settings_store.DEFAULTS["NOTIFICATION_DG_ACCEPT_EXTERNAL"] is False
