"""Werte in PowerShell-Skripttext nur als Literal (v1.10.117).

Bis v1.10.116 setzten mehrere Skripte Einstellungswerte roh ein, einige in
doppelte Anführungszeichen, in denen PowerShell `$(...)` ausführt. Siehe
`app/ps_text.py`.
"""
from __future__ import annotations

import ast
import subprocess
import shutil
from pathlib import Path

import pytest

import ps_text

APP = Path(__file__).resolve().parent.parent / "app"

# Woran eine Anweisung als PowerShell-Skripttext zu erkennen ist.
MERKMALE = ("Connect-ExchangeOnline", "$ErrorActionPreference", "-Identity",
            "Get-ServicePrincipal", "New-ServicePrincipal", "Get-TransportRule",
            "Get-EXOMailbox", "X509Certificate2", "ConvertTo-Json")

MASKIERER = {"lit", "quoted", "array", "kommentar", "_ps_lit", "_ps_str", "_ps_array",
             "_kommentar"}

# Eingesetzt ohne Maskierer — jeweils mit Grund.
ERLAUBT = {
    "require_auth": "Python-Literal '$true'/'$false', kein Eingabewert",
    "ps_check_imap": "Python-Literal '1'/'0'",
    "server_zeile": "aus _ps_array oder festem Text",
}


def _skript_stellen():
    for datei in sorted(APP.rglob("*.py")):
        quelle = datei.read_text(encoding="utf-8")
        baum = ast.parse(quelle)
        for anweisung in ast.walk(baum):
            if not isinstance(anweisung, ast.stmt) or isinstance(
                    anweisung, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                                ast.If, ast.For, ast.While, ast.Try, ast.With)):
                continue
            text = "".join(k.value for k in ast.walk(anweisung)
                           if isinstance(k, ast.Constant) and isinstance(k.value, str))
            if not any(m in text for m in MERKMALE):
                continue
            for k in ast.walk(anweisung):
                if isinstance(k, ast.FormattedValue):
                    yield datei, k


def test_jeder_eingesetzte_wert_ist_maskiert():
    roh = []
    for datei, k in _skript_stellen():
        w = k.value
        if isinstance(w, ast.Call):
            f = w.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name in MASKIERER:
                continue
        if isinstance(w, ast.Name) and w.id in ERLAUBT:
            continue
        roh.append(f"{datei.relative_to(APP)}:{k.lineno} {{{ast.unparse(w)}}}")
    assert not roh, "PowerShell-Text mit unmaskiertem Wert:\n  " + "\n  ".join(roh)


def test_suche_findet_skripte():
    """Gegenprobe: Die Erkennung erfasst die bekannten Skripte überhaupt."""
    dateien = {d.name for d, _ in _skript_stellen()}
    assert {"setup_wizard.py", "health_check.py", "exo_mailboxes.py"} <= dateien


@pytest.mark.parametrize("wert,erwartet", [
    ("O'Brien GmbH", "O''Brien GmbH"),
    ("a\r\nRemove-Mailbox x", "a  Remove-Mailbox x"),
    ("$(Remove-Item /)", "$(Remove-Item /)"),        # in '…' wirkungslos
    (None, ""),
])
def test_lit(wert, erwartet):
    assert ps_text.lit(wert) == erwartet


def test_array():
    assert ps_text.array(["a", "b'c"]) == "@('a', 'b''c')"


@pytest.mark.skipif(not shutil.which("pwsh"), reason="pwsh nicht installiert")
def test_powershell_liest_den_wert_woertlich():
    boese = "Firma $(Write-Output INJIZIERT) O'Brien`n; \"x\""
    r = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command",
                        f"$x = {ps_text.quoted(boese)}; Write-Output $x"],
                       capture_output=True, text=True, timeout=60)
    assert r.stdout.strip() == boese


def test_verteiler_skript_setzt_name_und_mitglieder_als_literal(monkeypatch, tmp_path):
    """Der gemeldete Fall: Gateway-Name und Mitglieder roh im Skripttext."""
    import setup_wizard
    import settings_store
    cert = tmp_path / "auth.pfx"
    cert.write_bytes(b"x")
    monkeypatch.setattr(setup_wizard, "_AUTH_CERT_PATH", cert)
    monkeypatch.setattr(setup_wizard.config, "CLIENT_ID", "11111111-2222-3333-4444-555555555555")
    werte = {"TENANT_DOMAIN": "example.onmicrosoft.com",
             "GATEWAY_NAME": "Firma $(Remove-Mailbox x) O'Brien"}
    monkeypatch.setattr(settings_store, "get", lambda k, *a: werte.get(k, ""))
    gesehen = {}

    class _Proc:
        returncode = 0
        stdout = '{"ok":true,"email":"dg@example.onmicrosoft.com"}'
        stderr = ""
    monkeypatch.setattr(setup_wizard.subprocess, "run",
                        lambda cmd, **kw: gesehen.update(s=cmd[-1]) or _Proc())
    setup_wizard.run_notification_dg_update(["o'neil@example.com"], False)
    s = gesehen["s"]
    assert "$dgName = 'Firma $(Remove-Mailbox x) O''Brien - Notification recipients'" in s
    assert "$membersStr = 'o''neil@example.com'" in s
    assert '"Firma' not in s, "kein Wert in doppelten Anführungszeichen"
