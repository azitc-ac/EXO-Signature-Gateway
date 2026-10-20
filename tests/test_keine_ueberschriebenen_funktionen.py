"""Keine Funktionsdeklaration, die dieselbe Seite danach per Zuweisung ersetzt.

Anlass (2026-10-21): settings_connect.html deklarierte hubConnect() und
certRequestInvoice() und ersetzte beide am Ende per `hubConnect = _gatedHubConnect`.
Die ersten Fassungen waren damit unerreichbar, sahen aber aus wie der gültige
Code — und nur die tote Fassung prüfte „Firma ist Pflicht“. EINE Fassung je Name.
"""
from __future__ import annotations

import re
from pathlib import Path

WEBUI = Path(__file__).parent.parent / "app/webui"
_DEKL = re.compile(r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", re.M)


def _verstoesse(text: str) -> list[str]:
    raus = []
    for name in set(_DEKL.findall(text)):
        if re.search(r"^\s*(?:window\.)?" + re.escape(name) + r"\s*=[^=]", text, re.M):
            raus.append(name)
    return sorted(raus)


def test_keine_ueberschriebene_funktion():
    fehler = []
    for p in sorted(WEBUI.rglob("*")):
        if p.suffix in (".html", ".js") and "vendor" not in p.parts:
            fehler += [f"{p.relative_to(WEBUI)}: {n}" for n in _verstoesse(p.read_text(encoding="utf-8"))]
    assert not fehler, fehler


def test_pruefer_erkennt_muster():
    assert _verstoesse("async function a() {}\nfunction b() {}\na = b;") == ["a"]
    assert _verstoesse("function a() { if (x == 1) return; }") == []


def test_hub_verbinden_prueft_firma():
    t = (WEBUI / "templates/settings_connect.html").read_text(encoding="utf-8")
    rumpf = t[t.index("async function hubConnect()"):]
    rumpf = rumpf[:rumpf.index("\n}\n")]
    assert "hub-company" in rumpf and "Pflicht" in rumpf
