"""Signierte Einladungen externer Absender laufen NICHT übers Gateway.

Die Regel „<Gateway> - SMIME Signed Inbound“ leitete jede signierte Post von
außen ans Gateway, auch Einladungen. Das Gateway nahm die Signatur ab und
stellte neu zu — in Graph++ per Ablage, bei der Exchange keinen Kalendereintrag
anlegt. Seit v1.12.34 nimmt die Regel Kalenderelemente aus
(`-ExceptIfMessageTypeMatches Calendaring`); Exchange stellt sie selbst zu.
Verschlüsselte Einladungen sind von außen nicht erkennbar — die Encrypted-Regel
bleibt ohne Ausnahme.

Fertig heißt jede Installation: Setzer (Anlage UND Aktualisieren-Zweig), Prüfung
aus EINEM Soll (regel_soll — Werkzeug und Abnahme), Aufgabe für Bestand.
"""
import subprocess
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
SKRIPT = WURZEL / "app" / "scripts" / "setup_smime_rules.ps1"
sys.path.insert(0, str(WURZEL / "tools"))

import abnahme  # noqa: E402
import regel_soll  # noqa: E402
import tenant_soll_check as tsc  # noqa: E402
from test_transportregel_gate import _anweisungen, _aufrufe  # noqa: E402


def _signed_aufrufe(cmdlet):
    return [a for a in _aufrufe(_anweisungen(SKRIPT.read_text("utf-8")), cmdlet)
            if "$signedRuleName" in a]


def _encrypted_aufrufe(cmdlet):
    return [a for a in _aufrufe(_anweisungen(SKRIPT.read_text("utf-8")), cmdlet)
            if "$encRuleName" in a]


def test_soll_kommt_aus_einer_quelle():
    assert regel_soll.SMIME_KALENDER_AUSNAHME == {"Signed": "Calendaring", "Encrypted": None}


@pytest.mark.parametrize("cmdlet", ["New-TransportRule", "Set-TransportRule"])
def test_signed_regel_traegt_die_ausnahme_in_beiden_zweigen(cmdlet):
    aufrufe = _signed_aufrufe(cmdlet)
    assert len(aufrufe) == 1, aufrufe
    soll = regel_soll.SMIME_KALENDER_AUSNAHME["Signed"]
    assert f"-ExceptIfMessageTypeMatches {soll}" in aufrufe[0], (
        f"{cmdlet}: Kalender-Ausnahme fehlt — signierte Einladungen laufen übers Gateway")


@pytest.mark.parametrize("cmdlet", ["New-TransportRule", "Set-TransportRule"])
def test_encrypted_regel_ohne_ausnahme(cmdlet):
    aufrufe = _encrypted_aufrufe(cmdlet)
    assert len(aufrufe) == 1, aufrufe
    assert "-ExceptIfMessageTypeMatches" not in aufrufe[0]


@pytest.mark.parametrize("typ,wert,fehlt", [
    ("Signed", "", True), ("Signed", None, True), ("Signed", "Calendaring", False),
    ("Signed", ["Calendaring"], False), ("Encrypted", "", False),
])
def test_soll_funktion(typ, wert, fehlt):
    assert regel_soll.smime_kalender_fehlt(typ, wert) is fehlt


def test_werkzeug_meldet_fehlende_ausnahme():
    from test_tenant_soll_check import INPUTS, gut
    z = gut()
    name = "EXO Signature Gateway - SMIME Signed Inbound"
    befunde = tsc.pruefe(z, INPUTS)
    assert not any(b["objekt"] == name and "Kalender" in b["text"] for b in befunde)
    next(r for r in z["transportregeln"] if r["Name"] == name)["ExceptIfMessageTypeMatches"] = ""
    w = [b for b in tsc.pruefe(z, INPUTS) if b["schwere"] == tsc.WARNUNG]
    assert any(b["objekt"] == name and "Kalender-Ausnahme" in b["text"] for b in w), w


def _werte(monkeypatch, **w):
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, d=None, *a, **kw: w.get(k, d))


def test_abnahme_entfaellt_ohne_smime_regeln(monkeypatch):
    _werte(monkeypatch, SMIME_RULES_CREATED=False)
    assert abnahme._smime_regeln_stand()["zustand"] == abnahme.ENTFAELLT


@pytest.mark.parametrize("stand", [None, 1])
def test_abnahme_offen_bei_altem_stand(monkeypatch, stand):
    _werte(monkeypatch, SMIME_RULES_CREATED=True, _SMIME_REGELN_STAND=stand)
    p = abnahme._smime_regeln_stand()
    assert p["zustand"] == abnahme.OFFEN
    assert "neu erstellen" in p["zu_tun"]


def test_abnahme_ok_nach_neuem_schreiben(monkeypatch):
    _werte(monkeypatch, SMIME_RULES_CREATED=True, _SMIME_REGELN_STAND=regel_soll.SMIME_REGELN_STAND)
    assert abnahme._smime_regeln_stand()["zustand"] == abnahme.OK


def test_einrichtung_merkt_den_stand(monkeypatch, tmp_path):
    """Mit dem ECHTEN settings_store auf einer Testdatei. Die erste Fassung
    ersetzte update() durch dict.update — dessen Filter auf DEFAULTS kam so nie
    zum Zug, und der Merker ging auf der VM still verloren."""
    import settings_store
    import setup_wizard
    monkeypatch.setattr(settings_store, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings_store, "_data", {})
    settings_store.init()
    cert = tmp_path / "auth.pfx"
    cert.write_bytes(b"x")
    monkeypatch.setattr(setup_wizard, "_AUTH_CERT_PATH", cert)
    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: subprocess.CompletedProcess(a, 0, "ok", ""))
    assert setup_wizard.run_smime_rules_setup("app", "t.onmicrosoft.com")["ok"]
    import json
    gespeichert = json.loads((tmp_path / "settings.json").read_text())
    assert gespeichert.get("_SMIME_REGELN_STAND") == regel_soll.SMIME_REGELN_STAND
    assert gespeichert.get("SMIME_RULES_CREATED") is True


def test_aufgabe_ist_eingetragen():
    import aufgaben
    a = next(x for x in aufgaben.AUFGABEN if x.kennung == "smime-regeln")
    assert a.pruefung is abnahme._smime_regeln_stand and a.link == "/setup#step-smime"
