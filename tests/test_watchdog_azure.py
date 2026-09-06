"""Sicherheits-Invarianten des Azure-Bypass-Wächters (statisch geprüft).

Der Wächter läuft extern mit eigenen (minimalen) Rechten und schaltet im Ausfall
Transportregeln. Diese Prüfungen halten seinen Wirkungskreis eng:
  - schaltet NUR (Enable/Disable), löscht NIE eine Regel,
  - fasst NUR die Signatur-Regel an, nie die S/MIME-Regel,
  - authentifiziert per Managed Identity (kein Secret/Zertifikat im Wächter),
  - Rolle ist minimal ("Transport Rules"), kein Admin/Postfach-Zugriff.
"""
from pathlib import Path

_AZ = Path(__file__).resolve().parent.parent / "azure" / "watchdog"
_CRON = Path(__file__).resolve().parent.parent / "contrib" / "watchdog"


def test_run_schaltet_nur_und_loescht_nie():
    s = (_AZ / "Watchdog" / "run.ps1").read_text()
    assert "Disable-TransportRule" in s
    assert "Enable-TransportRule" in s
    assert "Remove-TransportRule" not in s          # nie löschen
    assert "New-TransportRule" not in s             # nie anlegen


def test_run_nutzt_managed_identity_kein_secret():
    s = (_AZ / "Watchdog" / "run.ps1").read_text()
    assert "-ManagedIdentity" in s
    assert "ClientSecret" not in s
    assert "CertificateThumbprint" not in s and "-Certificate " not in s


def test_run_fasst_nur_die_signatur_regel_an():
    """Regelname kommt aus SIG_RULE_NAME; keine feste S/MIME-Regel wird berührt."""
    s = (_AZ / "Watchdog" / "run.ps1").read_text()
    assert "SIG_RULE_NAME" in s
    assert "S/MIME" not in s.split("param(")[-1]   # im Code (nicht Kommentar) keine S/MIME-Regel


def test_rolle_ist_minimal():
    s = (_AZ / "setup_watchdog_role.ps1").read_text()
    assert '"Transport Rules"' in s or "'Transport Rules'" in s
    # keine breiten Rollen versehentlich vergeben
    for weit in ("Organization Management", "Mail Recipients", "Role Management"):
        assert weit not in s


# ── cron-Variante (contrib/watchdog/) — dieselben Invarianten, Cert statt MI ──

def test_cron_schaltet_nur_und_loescht_nie():
    s = (_CRON / "watchdog.ps1").read_text()
    assert "Disable-TransportRule" in s
    assert "Enable-TransportRule" in s
    assert "Remove-TransportRule" not in s          # nie löschen
    assert "New-TransportRule" not in s             # nie anlegen


def test_cron_fasst_nur_die_signatur_regel_an():
    s = (_CRON / "watchdog.ps1").read_text()
    assert "SIG_RULE_NAME" in s
    # im ausführenden Teil (nach dem Kommentarkopf) keine S/MIME-Regel
    body = s.split("Set-StrictMode", 1)[-1]
    assert "S/MIME" not in body


def test_cron_timer_und_service_verweisen_aufeinander():
    """Der Timer aktiviert timers.target; der Service ruft genau watchdog.ps1."""
    timer = (_CRON / "exo-watchdog.timer").read_text()
    service = (_CRON / "exo-watchdog.service").read_text()
    assert "WantedBy=timers.target" in timer
    assert "OnUnitActiveSec=5min" in timer
    assert "watchdog.ps1" in service
    assert "Type=oneshot" in service
