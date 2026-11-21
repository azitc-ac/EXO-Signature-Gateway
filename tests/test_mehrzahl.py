"""Mehrzahl über EINEN Helfer, nie als Klammerform.

„Regel(n)“, „Gerät(e)“, „Secret(s)“ standen in Oberfläche, Mails und Protokoll,
daneben neun handgeschriebene `n === 1 ? … : …`-Weichen und drei Kopien eines
Helfers (abnahme._zahl, mailboxes._mbZahl, dmarc_berichte._n). Eine Weiche war
falsch: Der Betreff der Lizenz-Erinnerung lautete bei einem Tag „läuft in
1 Tagen ab“. Einzige Quellen jetzt: texte.zahl/wort (Python) und anzahlText()
in base.html (JavaScript).
"""
import re
from pathlib import Path

WURZEL = Path(__file__).resolve().parents[1]
APP = WURZEL / "app"

# Großgeschriebenes Wort direkt vor (n)/(en)/(s)/(e)/(er) — die deutsche bzw.
# englische Klammer-Mehrzahl. `\b` davor: „escAttr(n)“ ist ein Aufruf, kein Nomen.
KLAMMER = re.compile(r"\b[A-ZÄÖÜ][a-zäöüß]+\((?:n|en|s|e|er|nen)\)(?![\w(])")
# Eigene Zahlweiche in JS: `x === 1 ? '…' : '…s'` — auch mit leerem Einzahlzweig.
WEICHE_JS = re.compile(r"(?:=== ?1|!== ?1|\+ \([\w.]+ > ?1) \? ['\"`][^'\"`]*['\"`] : ['\"`][^'\"`]*[A-Za-zÄÖÜäöüß]")
# … und in Python/Jinja: `'…' if n == 1 else '…'`, auch `Mail{{ '' if n == 1 else 's' }}`.
WEICHE_PY = re.compile(r"""['"][^'"]*['"] if [^'"{}]+? == 1 else ['"][^'"]*[A-Za-zÄÖÜäöüß]""")

# JS-Konstruktoren/-Aufrufe mit Großbuchstaben, keine Nomen: String(e), Error(e) …
JS_AUFRUFE = {"String", "Number", "Boolean", "Error", "Date", "Array", "Object", "Promise"}
_LOG = {"debug", "info", "warning", "error", "exception", "critical"}


def _quellen():
    for p in sorted(APP.rglob("*")):
        if p.suffix in (".py", ".html", ".js") and p.is_file() and "vendor" not in p.parts:
            yield p


def _ist_kommentar(zeile: str) -> bool:
    z = zeile.strip()
    return z.startswith(("#", "//", "*", "/*"))


def _python_texte(p: Path):
    """(Zeile, Text) aller String-Konstanten, die Menschen zu sehen bekommen.

    Ausgenommen: Docstrings und Protokollaufrufe (log.info("%d Regel(n) …")) —
    das Protokoll liest, wer den Betrieb untersucht; dort bleibt die knappe
    Klammerform erlaubt. Oberfläche, Mails, Meldungen und Rückgaben nicht."""
    import ast
    baum = ast.parse(p.read_text(encoding="utf-8"))
    aus = set()
    for k in ast.walk(baum):
        if isinstance(k, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if k.body and isinstance(k.body[0], ast.Expr) and isinstance(k.body[0].value, ast.Constant):
                aus.add(id(k.body[0].value))
        if (isinstance(k, ast.Call) and isinstance(k.func, ast.Attribute)
                and k.func.attr in _LOG):
            for arg in k.args:
                for u in ast.walk(arg):
                    aus.add(id(u))
    for k in ast.walk(baum):
        if isinstance(k, ast.Constant) and isinstance(k.value, str) and id(k) not in aus:
            yield k.lineno, k.value


def _klammer_treffer(text: str):
    for m in KLAMMER.finditer(text):
        if m.group(0).split("(")[0] not in JS_AUFRUFE:
            yield m.group(0)


def test_keine_klammer_mehrzahl():
    treffer = []
    for p in _quellen():
        rel = str(p.relative_to(WURZEL))
        if p.suffix == ".py":
            for i, t in _python_texte(p):
                treffer += [f"{rel}:{i}: {m}" for m in _klammer_treffer(t)]
            continue
        for i, z in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if not _ist_kommentar(z):
                treffer += [f"{rel}:{i}: {m}" for m in _klammer_treffer(z)]
    assert not treffer, ("Klammer-Mehrzahl — texte.zahl()/anzahlText() verwenden:\n  "
                         + "\n  ".join(treffer))


def test_keine_eigenen_zahlweichen():
    treffer = []
    for p in _quellen():
        rel = str(p.relative_to(WURZEL))
        if rel in ("app/texte.py",):
            continue
        for i, z in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if _ist_kommentar(z) or "function anzahlText" in z:
                continue
            if WEICHE_JS.search(z) or WEICHE_PY.search(z):
                treffer.append(f"{rel}:{i}: {z.strip()[:100]}")
    assert not treffer, ("Eigene Einzahl/Mehrzahl-Weiche — texte.wort()/anzahlText() "
                         "verwenden:\n  " + "\n  ".join(treffer))


def test_helfer():
    import sys
    sys.path.insert(0, str(APP))
    import texte
    assert texte.zahl(1, "Tag", "Tagen") == "1 Tag"
    assert texte.zahl(0, "Tag", "Tagen") == "0 Tagen"
    assert texte.zahl(3, "Regel", "Regeln") == "3 Regeln"
    assert texte.wort(1, "läuft", "laufen") == "läuft"
