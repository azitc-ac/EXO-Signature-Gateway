"""Der laufende Wächter meldet „Regel aktiv, Gate leer“ — nicht nur das Werkzeug.

Vorfall 04.09.: Die Signatur-Regel war aktiv, FromMemberOf leer — jeder interne
Absender lief durchs Gateway. tools/tenant_soll_check.py erkennt das, lief aber
nirgends automatisch, und die Regelabfrage des Schedulers
(get_transport_rule_state.ps1 → waechter_regel.regel_lesen) las nur State und
SentToScope. Im Betrieb hätte nichts gemeldet.

Jetzt: Das Skript liefert from_member_of, regel_lesen merkt `rule_gate_offen`
aus regel_soll.gate_offen (dieselbe Funktion wie im Werkzeug), und ein Band auf
jeder Seite zeigt den Zustand.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))
sys.path.insert(0, str(WURZEL / "app" / "webui"))

import regel_soll  # noqa: E402
import waechter_regel  # noqa: E402
import waechter_state  # noqa: E402

from test_transportregel_gate import _anweisungen  # noqa: E402


@pytest.fixture
def zustand(monkeypatch, tmp_path):
    monkeypatch.setattr(waechter_state, "PFAD", tmp_path / "watchdog_state.json")
    return tmp_path


def _lesen(monkeypatch, antwort):
    monkeypatch.setattr(waechter_regel, "ps_regelskript", lambda *a, **k: antwort)
    return waechter_regel.regel_lesen()


def _band():
    from webui import deps
    return deps._gate_offen_gemeldet()


@pytest.mark.parametrize("gate", [[""], [], None, "  "])
def test_aktiv_ohne_gate_wird_gemeldet(zustand, monkeypatch, gate):
    erg = _lesen(monkeypatch, {"ok": True, "state": "Enabled", "sent_to_scope": "",
                               "from_member_of": gate})
    assert erg["gate_offen"] is True
    assert waechter_state.lesen().get("rule_gate_offen") is True
    assert _band(), "Band „Transportregel ohne Absender-Gate“ fehlt"


@pytest.mark.parametrize("state,gate", [
    ("Enabled", ["EXO Signature Gateway - Enabled Mailboxes"]),   # Soll
    ("Disabled", [""]),        # deaktiviert: kein Durchlauf, das meldet der Bypass
])
def test_kein_band_ohne_gefahr(zustand, monkeypatch, state, gate):
    erg = _lesen(monkeypatch, {"ok": True, "state": state, "sent_to_scope": "",
                               "from_member_of": gate})
    assert erg["gate_offen"] is False
    assert _band() is None


def test_alte_skriptfassung_ist_unbekannt_nicht_leer(zustand, monkeypatch):
    """Ohne from_member_of in der Antwort (Zwischenstand beim Update) kein
    Fehlalarm: unbekannt ist nicht leer."""
    erg = _lesen(monkeypatch, {"ok": True, "state": "Enabled", "sent_to_scope": ""})
    assert erg["gate_offen"] is None
    assert _band() is None


def test_skript_liefert_das_gate():
    """Ausgabevertrag: die JSON-Zeile trägt from_member_of aus $r.FromMemberOf."""
    s = (WURZEL / "app" / "scripts" / "get_transport_rule_state.ps1").read_text()
    code = " ".join(_anweisungen(s))
    assert "$r.FromMemberOf" in code
    ausgabe = [a for a in _anweisungen(s) if a.startswith("Write-Output") and "ok = $true" in a]
    assert len(ausgabe) == 1 and "from_member_of =" in ausgabe[0], ausgabe


def test_werkzeug_und_waechter_teilen_die_soll_funktion():
    """Keine zweite Definition: das Werkzeug benutzt regel_soll.gate_leer."""
    pfad = WURZEL / "tools" / "tenant_soll_check.py"
    spec = importlib.util.spec_from_file_location("tenant_soll_check_gate", pfad)
    tsc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tsc)
    assert tsc._gate_leer is regel_soll.gate_leer
    assert regel_soll.gate_offen("Enabled", [""]) and not regel_soll.gate_offen("Enabled", ["DG"])
