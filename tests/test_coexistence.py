"""On-prem-Koexistenz-Connector — Skripterzeugung und gespeicherte Parameter.

Kernverträge, die im Skript NIE brechen dürfen (sonst Mailstopp bzw. offenes
Relay on-prem):
  * `AuthMechanism` enthält `Tls` — sonst kein STARTTLS → Gateway QUIT → Mailstopp.
  * `PermissionGroups ExchangeServers` + `ExternalAuthoritative` — sonst kein
    `AuthAs: Internal`.
  * Die Quell-IP steht im Skript; Müll-Eingaben werden abgelehnt, nicht verbaut.
  * Der Connector heißt „Inbound from <Gateway> (Coexistence)".
"""
import pytest

import coexistence


def test_skript_grundgeruest():
    s = coexistence.baue_skript("EXO-Gateway", "203.0.113.5", ["EX10", "EX11"])
    assert s["name"] == "Inbound from EXO-Gateway (Coexistence)"
    for teil in (s["build"], s["teardown"]):
        assert "Inbound from EXO-Gateway (Coexistence)" in teil
    # Aufbau trägt die tragenden Optionen
    assert "New-ReceiveConnector" in s["build"]
    assert "-AuthMechanism Tls,ExternalAuthoritative" in s["build"]
    assert "-PermissionGroups ExchangeServers" in s["build"]
    assert "203.0.113.5" in s["build"]
    # Abriss entfernt gezielt diesen Connector
    assert "Remove-ReceiveConnector" in s["teardown"]
    assert "203.0.113.5" not in s["teardown"]      # Abriss braucht die IP nicht


def test_tls_bleibt_im_authmechanism():
    # Regressionswache: 'Tls' darf nicht aus AuthMechanism verschwinden.
    s = coexistence.baue_skript("gw", "10.0.0.9", [])
    assert "Tls,ExternalAuthoritative" in s["build"]
    assert "ExternalAuthoritative" in s["build"]


def test_server_liste_aus_string_wird_normalisiert():
    s = coexistence.baue_skript("gw", "10.0.0.9", "EX10, EX10  EX11")
    assert "@('EX10', 'EX11')" in s["build"]        # dedupe + split
    assert "@('EX10', 'EX11')" in s["teardown"]


def test_ohne_server_lokaler_rechner():
    s = coexistence.baue_skript("gw", "10.0.0.9", [])
    assert "$env:COMPUTERNAME" in s["build"]
    assert "$env:COMPUTERNAME" in s["teardown"]


def test_cidr_erlaubt():
    s = coexistence.baue_skript("gw", "192.168.1.0/24", [])
    assert "192.168.1.0/24" in s["build"]


def test_ungueltige_ip_wird_abgelehnt():
    with pytest.raises(ValueError):
        coexistence.baue_skript("gw", "kein-ip", [])
    with pytest.raises(ValueError):
        coexistence.baue_skript("gw", "", [])


def test_leerer_gateway_name_wird_abgelehnt():
    with pytest.raises(ValueError):
        coexistence.baue_skript("", "10.0.0.9", [])


def test_boeser_servername_wird_abgelehnt():
    with pytest.raises(ValueError):
        coexistence.baue_skript("gw", "10.0.0.9", ["EX10; rm -rf"])


def test_single_quote_wird_escaped():
    # Ein Apostroph im Namen darf das PS-Single-Quote-Literal nicht sprengen.
    s = coexistence.baue_skript("O'Brien-GW", "10.0.0.9", [])
    assert "'Inbound from O''Brien-GW (Coexistence)'" in s["build"]


# ── gespeicherte Parameter (konfig/ansicht/speichern) ────────────────────────
@pytest.fixture
def store(monkeypatch):
    daten = {}
    monkeypatch.setattr(coexistence.settings_store, "get",
                        lambda k, *a, **kw: daten.get(k))
    monkeypatch.setattr(coexistence.settings_store, "update",
                        lambda d: daten.update(d))
    return daten


def test_speichern_und_konfig_roundtrip(store):
    coex = coexistence.speichern("EXO-Gateway", "203.0.113.5", "EX10, EX11")
    assert coex["bereit"] is True
    assert store["COEX_CONFIG"]["source_ip"] == "203.0.113.5"
    assert store["COEX_CONFIG"]["servers"] == ["EX10", "EX11"]
    c = coexistence.konfig()
    assert c["gateway_name"] == "EXO-Gateway"


def test_ansicht_ohne_ip_nicht_bereit(store):
    a = coexistence.ansicht("Fallback-GW")
    assert a["bereit"] is False
    assert a["build"] == ""
    assert a["gateway_name"] == "Fallback-GW"      # Fallback greift ohne Konfig


def test_ansicht_mit_konfig_ist_bereit(store):
    coexistence.speichern("EXO-Gateway", "10.9.9.9", [])
    a = coexistence.ansicht("Fallback-GW")
    assert a["bereit"] is True
    assert "10.9.9.9" in a["build"]
    assert a["gateway_name"] == "EXO-Gateway"      # gespeicherter Name gewinnt


def test_speichern_lehnt_muell_ab_ohne_zu_schreiben(store):
    with pytest.raises(ValueError):
        coexistence.speichern("gw", "kaputt", [])
    assert "COEX_CONFIG" not in store              # Validierung VOR dem Schreiben
