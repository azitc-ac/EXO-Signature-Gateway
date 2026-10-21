"""Text vom Server landet nie unmaskiert als HTML in einer Meldung.

showAlert() nimmt bewusst HTML (viele Aufrufe setzen <strong>, <code>, Links).
Was dabei vom Server kommt — detail/error/message/output, Ausgaben von
PowerShell oder certbot, JSON-Dumps — muss deshalb an der Aufrufstelle durch
esc(). Bis 2026-10-21 taten das 35 Stellen nicht, dazu drei Ausgaben von
Exchange-PowerShell in der Einrichtung. Geprüft wird das Meldungsargument jedes
showAlert-Aufrufs; der Typ (2. Argument) darf Server-Felder vergleichen.
"""
from __future__ import annotations

import re
from pathlib import Path

VORLAGEN = Path(__file__).parent.parent / "app/webui/templates"
_FELD = re.compile(r"(?<![\w.])((?:d|data|e|err|r|res|resp|_d)\.(?:detail|error|message|output|msg|reason))\b")
_STRINGIFY = re.compile(r"JSON\.stringify\(")


def _meldungsargumente(text: str):
    for m in re.finditer(r"\bshowAlert\(", text):
        i, tiefe, start, args, q = m.end(), 1, m.end(), [], None
        while i < len(text) and tiefe:
            c = text[i]
            if q:
                if c == "\\":
                    i += 2
                    continue
                if c == q:
                    q = None
            elif c in "'\"`":
                q = c
            elif c in "([{":
                tiefe += 1
            elif c in ")]}":
                tiefe -= 1
                if tiefe == 0:
                    args.append(text[start:i])
            elif c == "," and tiefe == 1:
                args.append(text[start:i])
                start = i + 1
            i += 1
        if len(args) >= 3:
            yield text.count("\n", 0, m.start()) + 1, args[2]


def _ungeschuetzt(arg: str) -> bool:
    ohne = re.sub(r"esc(?:Attr)?\((?:[^()]|\([^()]*\))*\)", "", arg)
    return bool(_FELD.search(ohne) or _STRINGIFY.search(ohne))


def test_server_text_in_showalert_maskiert():
    fehler = [f"{p.name}:{z}: {a.strip()[:80]}"
              for p in sorted(VORLAGEN.glob("*.html"))
              for z, a in _meldungsargumente(p.read_text(encoding="utf-8")) if _ungeschuetzt(a)]
    assert not fehler, "Server-Text ohne esc() in showAlert:\n" + "\n".join(fehler)


def test_powershell_ausgabe_maskiert():
    t = (VORLAGEN / "setup.html").read_text(encoding="utf-8")
    assert not re.search(r"=\s*\(\s*\w+\.output\s*\|\|", t)
    # Die drei Einrichtungsschritte (Connector, S/MIME-Regeln, IMAP) zeigen ihre
    # Ausgabe über EINEN Helfer — dort wird maskiert, und keiner baut sie selbst.
    assert t.count("esc(d.output || '').replace(/\\n/g, '<br>')") == 1
    assert t.count(".output") == 1
    for url in ("exo-connector", "smime-rules", "imap-access"):
        assert re.search(r"_skriptSchritt\('/api/setup/" + url + "'", t), url


def test_pruefer_erkennt_muster():
    assert _ungeschuetzt(" 'Fehler: ' + (d.detail || 'x')")
    assert _ungeschuetzt(" 'Fehler: ' + JSON.stringify(d)")
    assert not _ungeschuetzt(" 'Fehler: ' + (esc(d.detail) || 'x')")
    assert not _ungeschuetzt(" 'Fehler: ' + esc(JSON.stringify(d))")


_FELD_HTML = re.compile(r"(?<![\w.])((?:d|data|e|err|r|res|resp|_d)\.(?:detail|error|message|output|msg|reason))\b"
                        r"(?!\s*(?:\?|===|!==|&&))")     # nur Bedingung, nicht eingesetzt


def test_server_text_in_innerhtml_maskiert():
    """Dieselbe Regel für innerHTML-Zuweisungen. e.message zählt mit: Chrome
    schreibt bei kaputtem JSON den Anfang der Server-Antwort hinein."""
    fehler = []
    for p in sorted(VORLAGEN.glob("*.html")):
        t = p.read_text(encoding="utf-8")
        for m in re.finditer(r"\.innerHTML\s*\+?=", t):
            ende = t.find(";\n", m.end())
            satz = t[m.end(): ende if ende > 0 else m.end() + 400]
            ohne = re.sub(r"esc(?:Attr)?\((?:[^()]|\([^()]*\))*\)", "", satz)
            if _FELD_HTML.search(ohne):
                fehler.append(f"{p.name}:{t.count(chr(10), 0, m.start()) + 1}: {satz.strip()[:80]}")
    assert not fehler, "Server-Text ohne esc() in innerHTML:\n" + "\n".join(fehler)
