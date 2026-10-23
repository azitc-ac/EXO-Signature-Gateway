"""Empfängerbedingung an der Signatur-Transportregel erkennen und gezielt
entfernen (regel_bedingung.py).

Regeln aus Installationen vor v1.8.64 tragen SentToScope=NotInOrganization:
interne Post erreicht das Gateway dann nie, „Rein interne Mails auch signieren“
wirkt nicht, und bei gemischten Empfängern läuft der interne Teil unsigniert
vorbei. Der einzige Heilweg war „Connector neu einrichten“ — der setzt auch die
Connectoren neu. Diese Tests halten fest: erkannt wird aus EINER Soll-Funktion,
entfernt wird NUR SentToScope, und nie an einer Regel ohne Gate.
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import abnahme            # noqa: E402
import regel_bedingung    # noqa: E402
import waechter_regel     # noqa: E402
import waechter_state     # noqa: E402

SKRIPTE = WURZEL / "app" / "scripts"


@pytest.fixture
def zustand(monkeypatch):
    gemerkt = {}
    monkeypatch.setattr(waechter_state, "merge", lambda **f: gemerkt.update(f) or gemerkt)
    monkeypatch.setattr(waechter_regel, "regelname", lambda: "Route via GW (Signatur)")
    return gemerkt


def _ps_liefert(monkeypatch, antwort):
    monkeypatch.setattr(waechter_regel, "ps_regelskript", lambda skript: antwort)


# ── Lesen ────────────────────────────────────────────────────────────────────

def test_abfrage_liefert_sent_to_scope_mit():
    """Ohne das Feld in der Ausgabe meldete die Prüfung jede Regel als in Ordnung."""
    quelle = (SKRIPTE / "get_transport_rule_state.ps1").read_text(encoding="utf-8")
    assert re.search(r"sent_to_scope\s*=\s*\[string\]\$r\.SentToScope", quelle)


@pytest.mark.parametrize("scope, erwartet", [
    ("NotInOrganization", regel_bedingung.BEDINGUNG),
    ("InOrganization", regel_bedingung.BEDINGUNG),
    ("", regel_bedingung.OK),
    ("  ", regel_bedingung.OK),
])
def test_pruefen(monkeypatch, zustand, scope, erwartet):
    _ps_liefert(monkeypatch, {"ok": True, "state": "Enabled", "sent_to_scope": scope})
    p = regel_bedingung.pruefen()
    assert p["zustand"] == erwartet and p["regel"] == "Route via GW (Signatur)"
    assert zustand["rule_sent_to_scope"] == scope.strip() and zustand["rule_checked"]


@pytest.mark.parametrize("antwort", [None, {"ok": False, "error": "connect: x"}])
def test_nicht_lesbar_ist_unbekannt_nicht_ok(monkeypatch, zustand, antwort):
    _ps_liefert(monkeypatch, antwort)
    assert regel_bedingung.pruefen()["zustand"] == regel_bedingung.UNBEKANNT


def test_taeglicher_lauf_warnt(monkeypatch, zustand, caplog):
    _ps_liefert(monkeypatch, {"ok": True, "state": "Enabled", "sent_to_scope": "NotInOrganization"})
    with caplog.at_level(logging.WARNING, logger="waechter"):
        assert waechter_regel.pruefe_und_merke() == "Enabled"
    assert any("Empfängerbedingung" in r.getMessage() for r in caplog.records)


# ── Abnahme ──────────────────────────────────────────────────────────────────

def test_abnahme_meldet_die_bedingung_offen_mit_weg(monkeypatch, zustand):
    assert abnahme._regel_empfaenger in abnahme.PRUEFUNGEN
    _ps_liefert(monkeypatch, {"ok": True, "state": "Enabled", "sent_to_scope": "NotInOrganization"})
    p = abnahme._regel_empfaenger()
    assert p["zustand"] == abnahme.OFFEN and "Empfängerbedingung entfernen" in p["zu_tun"]
    _ps_liefert(monkeypatch, {"ok": True, "state": "Enabled", "sent_to_scope": ""})
    assert abnahme._regel_empfaenger()["zustand"] == abnahme.OK
    _ps_liefert(monkeypatch, None)
    assert abnahme._regel_empfaenger()["zustand"] == abnahme.UNBEKANNT


# ── Entfernen ────────────────────────────────────────────────────────────────

def _entfern_skript() -> str:
    return (SKRIPTE / "remove_rule_recipient_scope.ps1").read_text(encoding="utf-8")


def _code(quelle: str) -> str:
    return "\n".join(z for z in quelle.splitlines() if not z.lstrip().startswith("#"))


def test_skript_fasst_nur_sent_to_scope_an():
    """Kein Connector, keine andere Bedingung — sonst wäre es wieder der grobe
    Weg, der hinter einem Proxy die TLS-Domäne zurücksetzt."""
    code = _code(_entfern_skript())
    setzer = re.findall(r"\bSet-\w+|\bNew-\w+|\bRemove-\w+|\bDisable-\w+|\bEnable-\w+", code)
    assert setzer == ["Set-TransportRule"], setzer
    zeile = next(z for z in code.splitlines() if "Set-TransportRule" in z)
    parameter = set(re.findall(r"\s-(\w+)", zeile))
    assert parameter == {"Identity", "SentToScope", "ErrorAction"}, parameter
    assert re.search(r"-SentToScope\s+\$null", zeile)


def test_skript_aendert_nichts_ohne_gate():
    """Ohne FromMemberOf matcht die Regel jeden internen Absender; ihr auch die
    Empfängerbedingung zu nehmen, schickte ALLE Post durchs Gateway."""
    code = _code(_entfern_skript())
    gate = code.index("FromMemberOf")
    setzen = code.index("Set-TransportRule")
    assert gate < setzen
    zweig = code[gate:setzen]
    assert "ok = $false" in zweig and "} else {" in zweig


def test_entfernen_merkt_den_neuen_stand(monkeypatch, zustand):
    _ps_liefert(monkeypatch, {"ok": True, "geaendert": True, "vorher": "NotInOrganization", "nachher": ""})
    r = regel_bedingung.entfernen("admin")
    assert r["ok"] and r["geaendert"] and r["vorher"] == "NotInOrganization"
    assert zustand["rule_sent_to_scope"] == "" and zustand["rule_checked"]


def test_entfernen_scheitert_ehrlich(monkeypatch, zustand):
    _ps_liefert(monkeypatch, {"ok": False, "vorher": "NotInOrganization", "error": "kein Gate"})
    r = regel_bedingung.entfernen("admin")
    assert r["ok"] is False and r["error"] == "kein Gate"
    assert "rule_sent_to_scope" not in zustand, "gescheitert, aber als geheilt gemerkt"
    _ps_liefert(monkeypatch, None)
    assert regel_bedingung.entfernen("admin")["ok"] is False


# ── Oberfläche ───────────────────────────────────────────────────────────────

pytest.importorskip("starlette.testclient", reason="httpx wird für TestClient benötigt")
from test_seiten import client  # noqa: E402,F401  (Fixture)


def test_seite_zeigt_die_bedingung_und_den_knopf(client, monkeypatch):
    monkeypatch.setattr(waechter_state, "lesen", lambda: {
        "rule_sent_to_scope": "NotInOrganization", "rule_checked": "2026-10-23T08:00:00Z"})
    html = client.get("/settings/signature").text
    knopf = re.search(r'<button[^>]*id="regel-bedingung-entfernen"[^>]*>', html).group(0)
    assert " hidden" not in knopf
    assert "<code>NotInOrganization</code>" in html

    monkeypatch.setattr(waechter_state, "lesen", lambda: {
        "rule_sent_to_scope": "", "rule_checked": "2026-10-23T08:00:00Z"})
    html = client.get("/settings/signature").text
    knopf = re.search(r'<button[^>]*id="regel-bedingung-entfernen"[^>]*>', html).group(0)
    assert " hidden" in knopf
    assert "ohne Empfängerbedingung" in html


def test_routen_verlangen_die_verwaltungsrolle():
    import inspect
    from webui.routen import setup
    for fn in (setup.api_regel_empfaengerbedingung, setup.api_regel_empfaengerbedingung_entfernen):
        assert "_require_admin" in inspect.getsource(fn)
