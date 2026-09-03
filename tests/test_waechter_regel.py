"""Unabhängige EXO-Regelzustand-Prüfung (waechter_regel) — nur die Logik.

Der echte `Get-TransportRule`-Aufruf läuft gegen EXO und ist nicht CI-fähig;
hier wird die pwsh-Ausführung gestellt und geprüft, dass die Antwort korrekt
zerlegt und der Zustand abgelegt wird. Read-only: nichts an EXO wird geändert.
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import waechter_regel as wr  # noqa: E402
import waechter_state  # noqa: E402


def _stub_pwsh(monkeypatch, tmp_path, stdout: str):
    monkeypatch.setattr(waechter_state, "PFAD", tmp_path / "state.json")
    monkeypatch.setattr(wr, "_AUTH_CERT", tmp_path / "auth.pfx")
    (tmp_path / "auth.pfx").write_bytes(b"x")
    monkeypatch.setattr(wr, "_SCRIPT", tmp_path / "s.ps1")
    (tmp_path / "s.ps1").write_text("x")

    def _run(*a, **kw):
        return types.SimpleNamespace(stdout=stdout, stderr="", returncode=0)
    monkeypatch.setattr(wr.subprocess, "run", _run)
    monkeypatch.setattr(wr.settings_store, "get",
                        lambda k, *a, **kw: {"CLIENT_ID": "app", "TENANT_DOMAIN": "t.onmicrosoft.com"}.get(k))
    monkeypatch.setattr(wr.config, "CLIENT_ID", "app")


def test_regelname_faellt_auf_standard_zurueck(monkeypatch):
    monkeypatch.setattr(wr.settings_store, "get",
                        lambda k, *a, **kw: {"GATEWAY_NAME": "EXO Signature Gateway"}.get(k))
    assert wr.regelname() == "Route via EXO Signature Gateway"


def test_regelname_nimmt_gesetzten_wert(monkeypatch):
    monkeypatch.setattr(wr.settings_store, "get",
                        lambda k, *a, **kw: {"EXO_RULE_SIG": "EXO Signature Gateway - Signatures"}.get(k))
    assert wr.regelname() == "EXO Signature Gateway - Signatures"


def test_enabled_wird_gemerkt(monkeypatch, tmp_path):
    _stub_pwsh(monkeypatch, tmp_path, '{"ok":true,"state":"Enabled"}')
    assert wr.pruefe_und_merke() == "Enabled"
    assert waechter_state.lesen()["rule_state"] == "Enabled"


def test_disabled_wird_gemerkt(monkeypatch, tmp_path):
    _stub_pwsh(monkeypatch, tmp_path, 'noise\n{"ok":true,"state":"Disabled"}\n')
    assert wr.pruefe_und_merke() == "Disabled"
    assert waechter_state.lesen()["rule_state"] == "Disabled"


def test_fehler_liefert_none_und_merkt_nichts(monkeypatch, tmp_path):
    _stub_pwsh(monkeypatch, tmp_path, '{"ok":false,"error":"connect: nope"}')
    assert wr.pruefe_und_merke() is None
    assert "rule_state" not in waechter_state.lesen()


def test_banner_bei_disabled_regel(monkeypatch, tmp_path):
    """Das Banner (deps._bypass_gemeldet) greift auch aus der unabhängigen
    Regelprüfung heraus — nicht nur aus dem Heartbeat."""
    from webui import deps
    monkeypatch.setattr(waechter_state, "PFAD", tmp_path / "s.json")
    waechter_state.merge(rule_state="Disabled", rule_checked="2026-09-03T00:00:00Z")
    b = deps._bypass_gemeldet()
    assert b and b["seit"], "Disabled-Regel muss das Banner auslösen"
    waechter_state.merge(rule_state="Enabled", bypass_active=False)
    assert deps._bypass_gemeldet() is None, "bei aktiver Regel kein Banner"


def test_ohne_konfiguration_kein_aufruf(monkeypatch, tmp_path):
    monkeypatch.setattr(waechter_state, "PFAD", tmp_path / "state.json")
    monkeypatch.setattr(wr.config, "CLIENT_ID", "")
    monkeypatch.setattr(wr.settings_store, "get", lambda k, *a, **kw: None)
    # subprocess.run darf gar nicht erst laufen
    def _boom(*a, **kw):
        raise AssertionError("pwsh darf ohne Konfiguration nicht aufgerufen werden")
    monkeypatch.setattr(wr.subprocess, "run", _boom)
    assert wr.pruefe_und_merke() is None
