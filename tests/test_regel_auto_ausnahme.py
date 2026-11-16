"""Auto-Antwort-Ausnahme an den Routing-Regeln (regel_auto_ausnahme.py).

Die Ausnahme kam mit v1.9.88 nur an NEU angelegte Regeln. Ältere
Installationen zogen die Abwesenheitsnotizen ihrer Postfächer (MAIL FROM:<>)
weiter ins Gateway; der Rückweg an externe Empfänger scheiterte
(5.7.64 TenantAttribution), und keine Prüfung im Produkt sah es.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import abnahme               # noqa: E402
import config                # noqa: E402
import regel_auto_ausnahme as raa   # noqa: E402
import settings_store        # noqa: E402
import waechter_regel        # noqa: E402

SKRIPTE = WURZEL / "app" / "scripts"


@pytest.fixture
def store(monkeypatch, tmp_path):
    """Der ECHTE settings_store auf einer Testdatei — der Befund geht über
    force_update; ein Ersatz durch dict.update hätte dessen Weg nie geprüft."""
    monkeypatch.setattr(settings_store, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings_store, "_data", {})
    settings_store.init()
    settings_store.update({"GATEWAY_NAME": "Mein Tor", "EXO_RULE_SIG": "",
                           "EXO_RULE_SMIME": "", "EXO_CONNECTOR_CREATED": True})
    return tmp_path / "settings.json"


def _skript(monkeypatch, antworten: list):
    """ps_regelskript nachspielen: je Aufruf die nächste Antwort, Aufrufe merken."""
    aufrufe = []

    def fake(skript, regeln=None, extra=()):
        aufrufe.append((Path(skript).name, regeln))
        return antworten.pop(0)
    monkeypatch.setattr(waechter_regel, "ps_regelskript", fake)
    return aufrufe


# ── Soll ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("regel, soll", [
    ({"header": "Auto-Submitted", "words": ["auto"]}, True),
    ({"header": "auto-submitted", "words": "AUTO"}, True),      # PS liefert Einzelwert als Text
    ({"header": "Auto-Submitted", "words": ["no", "auto"]}, True),
    ({"header": "Auto-Submitted", "words": ["auto-replied"]}, False),
    ({"header": "", "words": []}, False),                       # die Regel aus dem Feld
    ({"header": "X-Sig-Applied", "words": ["auto"]}, False),
    ({}, False),
])
def test_ist_soll(regel, soll):
    assert raa.ist_soll(regel) is soll


def test_regelnamen_aus_den_quellen_der_einrichtung(store):
    """Ein angepasster Gateway-Name ändert die Regelnamen — nichts fest kodiert."""
    namen = raa.regelnamen()
    assert namen[0] == waechter_regel.regelname() == "Route via Mein Tor (Signatur)"
    assert "Route via Mein Tor (S/MIME)" in namen and "Route via Mein Tor" in namen
    assert not any("SMIME Signed Inbound" in n for n in namen), "Eingangsregeln gehören nicht dazu"


# ── Lesen und merken ──────────────────────────────────────────────────────────

def test_fehlende_ausnahme_wird_gemerkt_und_gemeldet(store, monkeypatch):
    _skript(monkeypatch, [{"ok": True, "regeln": [
        {"name": "Route via Mein Tor (Signatur)", "found": True, "header": "", "words": []},
        {"name": "Route via Mein Tor", "found": False},
        {"name": "Route via Mein Tor (S/MIME)", "found": True,
         "header": "Auto-Submitted", "words": ["auto"]}]}])
    assert raa.pruefen()["zustand"] == raa.FEHLT
    gespeichert = json.loads(store.read_text())[raa.KEY]
    assert gespeichert["zustand"] == raa.FEHLT and gespeichert["fassung"] == config.VERSION
    assert gespeichert["fehlt"] == ["Route via Mein Tor (Signatur)"]
    p = abnahme._regel_auto_ausnahme()
    assert p["zustand"] == abnahme.OFFEN and "Route via Mein Tor (Signatur)" in p["befund"]


def test_vorhandene_ausnahme_ist_ok(store, monkeypatch):
    _skript(monkeypatch, [{"ok": True, "regeln": [
        {"name": "Route via Mein Tor (Signatur)", "found": True,
         "header": "Auto-Submitted", "words": ["auto"]}]}])
    assert raa.pruefen()["zustand"] == raa.OK
    assert abnahme._regel_auto_ausnahme()["zustand"] == abnahme.OK


def test_nicht_lesbar_ueberschreibt_den_befund_nicht(store, monkeypatch):
    """Ein Netzaussetzer darf einen gemerkten „fehlt“-Befund nicht löschen."""
    _skript(monkeypatch, [{"ok": True, "regeln": [
        {"name": "Route via Mein Tor (Signatur)", "found": True, "header": "", "words": []}]},
        None])
    raa.pruefen()
    assert raa.pruefen()["zustand"] == raa.UNBEKANNT
    assert raa.gemerkt()["zustand"] == raa.FEHLT


def test_abnahme_ohne_befund_und_ohne_connector(store, monkeypatch):
    assert abnahme._regel_auto_ausnahme()["zustand"] == abnahme.UNBEKANNT
    settings_store.update({"EXO_CONNECTOR_CREATED": False})
    assert abnahme._regel_auto_ausnahme()["zustand"] == abnahme.ENTFAELLT


# ── Ergänzen: Erfolg ist der neu gelesene Zustand ─────────────────────────────

def test_ergaenzen_liest_nach(store, monkeypatch):
    aufrufe = _skript(monkeypatch, [
        {"ok": True, "regeln": [{"name": "Route via Mein Tor (Signatur)", "found": True,
                                 "vorher": ":", "nachher": "Auto-Submitted:auto"}]},
        {"ok": True, "regeln": [{"name": "Route via Mein Tor (Signatur)", "found": True,
                                 "header": "Auto-Submitted", "words": ["auto"]}]}])
    erg = raa.ergaenzen("test")
    assert erg["ok"] and erg["zustand"] == raa.OK
    assert [a[0] for a in aufrufe] == ["set_rule_auto_ausnahme.ps1", "get_rule_auto_ausnahme.ps1"]
    assert aufrufe[0][1] == ",".join(raa.regelnamen())
    assert abnahme._regel_auto_ausnahme()["zustand"] == abnahme.OK


def test_ergaenzen_ohne_wirkung_ist_kein_erfolg(store, monkeypatch):
    """Skript meldet ok, die Regel hat die Ausnahme danach trotzdem nicht."""
    _skript(monkeypatch, [
        {"ok": True, "regeln": [{"name": "Route via Mein Tor (Signatur)", "found": True}]},
        {"ok": True, "regeln": [{"name": "Route via Mein Tor (Signatur)", "found": True,
                                 "header": "", "words": []}]}])
    assert raa.ergaenzen("test")["ok"] is False


# ── Einmal je Fassung beim Start ──────────────────────────────────────────────

def test_nach_update_prueft_einmal_je_fassung(store, monkeypatch):
    aufrufe = _skript(monkeypatch, [{"ok": True, "regeln": [
        {"name": "Route via Mein Tor (Signatur)", "found": True,
         "header": "Auto-Submitted", "words": ["auto"]}]}])
    raa.nach_update()
    raa.nach_update()                      # gleiche Fassung: kein zweiter Exchange-Login
    assert len(aufrufe) == 1
    settings_store.force_update({raa.KEY: {**raa.gemerkt(), "fassung": "0.0.1"}})
    _skript(monkeypatch, [None])
    raa.nach_update()                      # ältere Fassung: erneut
    assert raa.laeuft() is False


def test_start_stoesst_die_pruefung_an():
    quelle = (WURZEL / "app" / "main.py").read_text("utf-8")
    assert "regel_auto_ausnahme.nach_update()" in quelle


def test_connector_einrichtung_liest_den_befund_nach():
    quelle = (WURZEL / "app" / "setup_wizard.py").read_text("utf-8")
    teil = quelle[quelle.index("def run_exo_connector_setup"):]
    teil = teil[:teil.index("\ndef ", 10)]
    assert "regel_auto_ausnahme.pruefen()" in teil


def test_aufgabe_ist_eingetragen():
    import aufgaben
    a = next(x for x in aufgaben.AUFGABEN if x.kennung == "regel-auto-ausnahme")
    assert a.pruefung is abnahme._regel_auto_ausnahme
    assert a.link == "/setup#exo-auto-ausnahme" and a.laeuft is not None


# ── Skripte: jeder Weg, der eine Routing-Regel schreibt ───────────────────────

def test_jede_neue_routing_regel_hat_die_ausnahme():
    """Jedes New-TransportRule, das AUSGEHENDE Post der Postfächer zum Gateway
    leitet (FromMemberOf + RouteMessageOutboundConnector), trägt die Ausnahme.
    Die eingehenden S/MIME-Regeln (FromScope NotInOrganization) nicht."""
    gefunden = 0
    for datei in SKRIPTE.glob("*.ps1"):
        text = datei.read_text("utf-8")
        for m in re.finditer(r"New-TransportRule\b(.*?)\|\s*Out-Null", text, re.S):
            if ("RouteMessageOutboundConnector" not in m.group(1)
                    or "-FromMemberOf" not in m.group(1)):
                continue
            gefunden += 1
            assert '-ExceptIfHeaderContainsMessageHeader "Auto-Submitted"' in m.group(1), datei.name
            assert '-ExceptIfHeaderContainsWords "auto"' in m.group(1), datei.name
    assert gefunden >= 2


def test_bestehende_smime_regel_bekommt_die_ausnahme():
    """Bis v1.12.38 bekam eine schon vorhandene getrennte S/MIME-Regel die
    Ausnahme auf keinem Weg."""
    text = (SKRIPTE / "setup_rule_split.ps1").read_text("utf-8")
    zweig = text[text.index('Write-OK "S/MIME-Regel angelegt"'):text.index("Set-RuleGate $smimeRule")]
    assert "} else {" in zweig and '-ExceptIfHeaderContainsWords "auto"' in zweig


def test_ergaenzen_skript_setzt_nur_die_ausnahme():
    text = (SKRIPTE / "set_rule_auto_ausnahme.ps1").read_text("utf-8")
    setze = re.search(r"Set-TransportRule\b(.*?)\|\s*Out-Null", text, re.S).group(1)
    parameter = set(re.findall(r"(?<![\w-])-(\w+)", setze))
    assert parameter == {"Identity", "ExceptIfHeaderContainsMessageHeader",
                         "ExceptIfHeaderContainsWords", "ErrorAction"}, parameter
    lesen = (SKRIPTE / "get_rule_auto_ausnahme.ps1").read_text("utf-8")
    assert "Set-" not in lesen, "Das Leseskript schreibt nichts"
