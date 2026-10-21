"""getJSON/postJSON/sendJSON liefern NIE null — immer ein Objekt, im Fehlerfall
{ok:false, error}. Ein `if (!d)` danach greift deshalb nie.

Anlass (2026-10-21): Abnahme und SSO-Prüfung in setup.html fragten `if (!d)`.
Scheiterte der Abruf, lief der Code weiter und zeigte „undefined offen,
undefined ungeklärt“. Gefunden beim Durchsehen der Kopien-Gruppen.
"""
from __future__ import annotations

import re
from pathlib import Path

WEBUI = Path(__file__).parent.parent / "app/webui"
_ZUWEISUNG = re.compile(r"\b(?:const|let|var)\s+(\w+)\s*=\s*await\s+(?:getJSON|postJSON|sendJSON)\(")


def _verstoesse(text: str) -> list[int]:
    zeilen = text.splitlines()
    raus = []
    for i, z in enumerate(zeilen):
        m = _ZUWEISUNG.search(z)
        if not m:
            continue
        name = re.escape(m.group(1))
        for j in range(i + 1, min(i + 6, len(zeilen))):
            # Nur das nackte `if (!d)`. `if (!d || !d.ok)` ist überflüssig, aber
            # richtig — der zweite Teil fängt den Fehler ab.
            if re.search(r"if\s*\(\s*!\s*" + name + r"\s*\)", zeilen[j]):
                raus.append(j + 1)
    return raus


def test_kein_null_vergleich_nach_json_helfer():
    fehler = []
    for p in sorted(WEBUI.rglob("*")):
        if p.suffix in (".html", ".js") and "vendor" not in p.parts:
            fehler += [f"{p.relative_to(WEBUI)}:{n}" for n in _verstoesse(p.read_text(encoding="utf-8"))]
    assert not fehler, "getJSON/postJSON liefern nie null — auf .ok oder die Nutzdaten prüfen:\n" + "\n".join(fehler)


def test_pruefer_erkennt_muster():
    assert _verstoesse("const d = await getJSON('/x');\nknopf.disabled = false;\nif (!d) { return; }") == [3]
    assert _verstoesse("const d = await getJSON('/x');\nif (!d.ok) return;") == []
    assert _verstoesse("const r = await postJSON('/x', {});\nif (!r || !r.ok) return;") == []


def test_relay_identitaet_meldet_sichtbar():
    """showMsg auf das Kästchen selbst zeigte nichts an (textContent eines <input>)."""
    t = (WEBUI / "templates/relay.html").read_text(encoding="utf-8")
    assert "showMsg(cb," not in t
    assert 'id="identitaeten-meldung"' in t
    # beide Schalter (extern/aktiv) laufen seit 2026-10-21 über identitaetSchalter()
    assert t.count("showMsg('identitaeten-meldung'") == 1
    assert t.count("identitaetSchalter(\\'") == 2
