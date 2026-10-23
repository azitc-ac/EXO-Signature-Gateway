"""Die Loop-Markierung ist je Installation eigen (loop_detector.marker_wert).

Bis v1.10.43 setzte jedes Gateway `X-Sig-Applied: 1`, und jede Regel nahm `1`
aus. Post eines ANDEREN Gateways (anderer Kunde, anderer Tenant) trug die
Markierung schon — die eingehenden S/MIME-Regeln des Empfängers nahmen sie
aus: kein Absenderzertifikat übernommen, verschlüsselte Post nicht
entschlüsselt (gemessen 23.10.2026, a-az@azitc.org → alexander@zarenko.net).
"""
from __future__ import annotations

import email
import re
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import abnahme          # noqa: E402
import config          # noqa: E402
import loop_detector   # noqa: E402
import regel_kennung   # noqa: E402
import settings_store  # noqa: E402
import waechter_regel  # noqa: E402

SKRIPTE = WURZEL / "app" / "scripts"
T_A = "aaaaaaaa-1111-2222-3333-444444444444"
T_B = "bbbbbbbb-5555-6666-7777-888888888888"


@pytest.fixture
def tenant(monkeypatch):
    werte = {"TENANT_ID": T_A, "LOOP_HEADER": "X-Sig-Applied",
             "GATEWAY_NAME": "GW", "EXO_RULE_SIG": "", "EXO_RULE_SMIME": ""}
    monkeypatch.setattr(config, "TENANT_ID", "")
    monkeypatch.setattr(settings_store, "get", lambda k, d=None, *a, **kw: werte.get(k, d))
    return werte


def _wert_fuer(werte, t):
    werte["TENANT_ID"] = t
    return loop_detector.marker_wert()


# ── Wert und Muster ──────────────────────────────────────────────────────────

def test_wert_je_tenant_eigen(tenant):
    a, b = _wert_fuer(tenant, T_A), _wert_fuer(tenant, T_B)
    assert a != b and a != "1" and b != "1"
    assert _wert_fuer(tenant, T_A.upper()) == a, "Groß-/Kleinschreibung der Tenant-ID"
    assert _wert_fuer(tenant, "") == "1", "ohne Tenant die Altform"


def test_alte_regeln_nehmen_die_neue_kennung_weiter_aus(tenant):
    """Sonst kreiste nach dem Update jede Mail, bis die Regeln angeglichen sind:
    die alten Regeln prüfen das UNVERANKERTE Muster `1`."""
    # Für JEDEN Tenant, nicht nur zufällig: die Hexziffern allein enthalten
    # nicht immer eine 1 — erst das Präfix garantiert den Übergang.
    for t in (T_A, T_B, "00000000-0000-0000-0000-000000000000"):
        wert = _wert_fuer(tenant, t)
        assert wert.startswith("1-") and re.search("1", wert)


def test_muster_ist_verankert_und_trennscharf(tenant):
    b = _wert_fuer(tenant, T_B)
    a = _wert_fuer(tenant, T_A)
    muster = loop_detector.marker_muster()
    assert re.fullmatch(r"\^1-[0-9a-f]{12}\$", muster)
    assert re.search(muster, a)
    assert not re.search(muster, b), "fremde Kennung passt"
    assert not re.search(muster, "1"), "Altform passt"
    assert not re.search(muster, a + "x")


def test_fremde_markierung_zaehlt_nicht(tenant):
    eigen = loop_detector.marker_wert()
    fremd = email.message_from_string("X-Sig-Applied: 1\n\nx")
    assert not loop_detector.is_signed(fremd)
    loop_detector.mark_as_signed(fremd)
    assert fremd.get_all("X-Sig-Applied") == [eigen] and loop_detector.is_signed(fremd)


def test_bytes_markierung_ersetzt_die_fremde(tenant):
    eigen = loop_detector.marker_wert().encode()
    roh = (b"Received: from a\r\n by b\r\nX-Sig-Applied: 1\r\n  fortsetzung\r\n"
           b"From: x@y.de\r\n\r\nX-Sig-Applied: 1 im Rumpf\r\n")
    aus = loop_detector.mark_as_signed_bytes(roh)
    kopf, rumpf = aus.split(b"\r\n\r\n", 1)
    assert kopf.split(b"\r\n")[0] == b"X-Sig-Applied: " + eigen
    assert kopf.count(b"X-Sig-Applied") == 1 and b"fortsetzung" not in kopf
    assert b"Received: from a\r\n by b" in kopf and b"From: x@y.de" in kopf
    assert rumpf == roh.split(b"\r\n\r\n", 1)[1], "Rumpf verändert"
    assert loop_detector.mark_as_signed_bytes(aus) == aus, "nicht idempotent"


# ── Alle Wege setzen und prüfen denselben Wert ───────────────────────────────

def test_kein_regelskript_verdrahtet_das_muster_fest():
    for skript in SKRIPTE.glob("*.ps1"):
        quelle = skript.read_text(encoding="utf-8")
        assert '-ExceptIfHeaderMatchesPatterns "1"' not in quelle, skript.name
        for treffer in re.findall(r"-ExceptIfHeaderMatchesPatterns\s+(\S+)", quelle):
            assert treffer == "$LoopPattern", (skript.name, treffer)


def test_einrichtung_gibt_jedem_regelskript_das_muster():
    import inspect
    import setup_wizard
    quelle = inspect.getsource(setup_wizard)
    assert quelle.count('"-LoopHeader"') == quelle.count('"-LoopPattern", _loop_muster()') == 3


def test_graph_rueckweg_setzt_den_eigenen_wert():
    import inspect
    import graph_reinject
    quelle = inspect.getsource(graph_reinject)
    assert '"value": loop_detector.marker_wert()' in quelle
    assert re.search(r'header_name\(\), "value": "1"', quelle) is None


# ── Prüfen und Angleichen ────────────────────────────────────────────────────

def _ps(monkeypatch, antwort, aufrufe=None):
    def _lauf(skript, regeln=None, extra=()):
        if aufrufe is not None:
            aufrufe.append((skript.name, regeln, extra))
        return antwort
    monkeypatch.setattr(waechter_regel, "ps_regelskript", _lauf)


def test_regelnamen_umfassen_alle_wege(tenant):
    namen = regel_kennung.regelnamen()
    assert "Route via GW (Signatur)" in namen and "Route via GW (S/MIME)" in namen
    assert "GW - SMIME Signed Inbound" in namen and "GW - SMIME Encrypted Inbound" in namen


def test_pruefen(tenant, monkeypatch):
    soll = loop_detector.marker_muster()
    _ps(monkeypatch, {"ok": True, "regeln": [
        {"name": "A", "found": True, "header": "X-Sig-Applied", "patterns": [soll]},
        {"name": "B", "found": True, "header": "X-Sig-Applied", "patterns": ["1"]},
        {"name": "C", "found": False}]})
    p = regel_kennung.pruefen()
    assert p["zustand"] == regel_kennung.ABWEICHUNG
    assert [(r["name"], r["ok"]) for r in p["regeln"]] == [("A", True), ("B", False)]
    _ps(monkeypatch, {"ok": True, "regeln": [
        {"name": "A", "found": True, "header": "X-Anders", "patterns": [soll]}]})
    assert regel_kennung.pruefen()["zustand"] == regel_kennung.ABWEICHUNG, "Header-Name"
    _ps(monkeypatch, {"ok": True, "regeln": [
        {"name": "A", "found": True, "header": "X-Sig-Applied", "patterns": [soll]}]})
    assert regel_kennung.pruefen()["zustand"] == regel_kennung.OK
    _ps(monkeypatch, {"ok": True, "regeln": [{"name": "A", "found": False}]})
    assert regel_kennung.pruefen()["zustand"] == regel_kennung.KEINE
    _ps(monkeypatch, None)
    assert regel_kennung.pruefen()["zustand"] == regel_kennung.UNBEKANNT


def test_angleichen_gibt_header_und_muster_mit(tenant, monkeypatch):
    aufrufe = []
    _ps(monkeypatch, {"ok": True, "regeln": [{"name": "A", "found": True, "vorher": "1",
                                               "nachher": loop_detector.marker_muster()}]}, aufrufe)
    r = regel_kennung.angleichen("admin")
    assert r["ok"] and r["regeln"][0]["vorher"] == "1"
    (skript, regeln, extra), = aufrufe
    assert skript == "set_rule_loop_marker.ps1" and "Route via GW (Signatur)" in regeln
    assert extra == ("-LoopHeader", "X-Sig-Applied", "-LoopPattern", loop_detector.marker_muster())
    _ps(monkeypatch, None)
    assert regel_kennung.angleichen("admin")["ok"] is False


def test_angleich_skript_fasst_nur_die_ausnahme_an():
    code = "\n".join(z for z in (SKRIPTE / "set_rule_loop_marker.ps1").read_text(encoding="utf-8").splitlines()
                     if not z.lstrip().startswith("#"))
    assert re.findall(r"\b(?:Set|New|Remove|Disable|Enable)-\w+", code) == ["Set-TransportRule"]
    i = code.index("Set-TransportRule")
    aufruf = code[i:code.index("| Out-Null", i)]
    assert set(re.findall(r"\s-(\w+)", aufruf)) == {
        "Identity", "ExceptIfHeaderMatchesMessageHeader", "ExceptIfHeaderMatchesPatterns", "ErrorAction"}


def test_abnahme(tenant, monkeypatch):
    assert abnahme._regel_kennung in abnahme.PRUEFUNGEN
    _ps(monkeypatch, {"ok": True, "regeln": [
        {"name": "B", "found": True, "header": "X-Sig-Applied", "patterns": ["1"]}]})
    p = abnahme._regel_kennung()
    assert p["zustand"] == abnahme.OFFEN and "Regeln angleichen" in p["zu_tun"]
    _ps(monkeypatch, {"ok": True, "regeln": [{"name": "A", "found": False}]})
    assert abnahme._regel_kennung()["zustand"] == abnahme.ENTFAELLT
    _ps(monkeypatch, None)
    assert abnahme._regel_kennung()["zustand"] == abnahme.UNBEKANNT


def test_routen_verlangen_die_verwaltungsrolle():
    import inspect
    from webui.routen import setup
    for fn in (setup.api_regel_schleifenkennung, setup.api_regel_schleifenkennung_angleichen):
        assert "_require_admin" in inspect.getsource(fn)
