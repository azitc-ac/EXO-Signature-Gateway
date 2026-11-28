"""Werte in PowerShell-Skripttext einsetzen — ein Weg, immer als Literal.

ANLASS (v1.10.117)
------------------
Die Skripte für Exchange Online entstehen als Python-f-Strings. Einige setzten
Einstellungswerte (Gateway-Name, Mitgliederliste, Regelnamen) roh ein, teils in
doppelte Anführungszeichen. Dort wertet PowerShell `$(...)` und `$variable`
aus; ein Gateway-Name mit `$(...)` liefe als Befehl, mit den Rechten der
Exchange-Anmeldung. Ein Apostroph in einfachen Anführungszeichen brach das
Skript.

REGEL
-----
* Jeder Wert steht in EINFACHEN Anführungszeichen und geht durch `lit()`
  (bzw. `quoted()` / `array()`, die die Anführungszeichen mitliefern).
  In einfachen Anführungszeichen ist `'` das einzige Sonderzeichen.
* Zeilenumbrüche werden zu Leerzeichen, damit ein Wert keine Zeile einschiebt
  (auch nicht aus einem Kommentar heraus).
* Erzwungen von `tests/test_ps_text.py` über alle Skript-f-Strings.
"""
from __future__ import annotations


def lit(wert) -> str:
    """Inhalt für `'…'` — ohne die Anführungszeichen selbst."""
    return str(wert or "").replace("\r", " ").replace("\n", " ").replace("'", "''")


def quoted(wert) -> str:
    """Vollständiges Single-Quote-Literal: `'…'`."""
    return "'" + lit(wert) + "'"


def kommentar(wert) -> str:
    """Für eine `#`-Kommentarzeile: nur ohne Zeilenumbruch."""
    return str(wert or "").replace("\r", " ").replace("\n", " ")


def array(werte) -> str:
    """PowerShell-Array aus Literalen: `@('a', 'b')`."""
    return "@(" + ", ".join(quoted(w) for w in werte) + ")"


_FARBCODE = __import__("re").compile(r"\x1b\[[0-9;]*[A-Za-z]")


def ausgabe(proc) -> str:
    """stdout + stderr eines PowerShell-Laufs, ohne ANSI-Farbcodes.

    pwsh färbt Warnungen auch bei umgeleiteter Ausgabe; in der Oberfläche
    erschienen sie roh („[33;1mWARNING: The command completed successfully
    but no settings … have been modified“, 28.11.2026). EIN Weg für alle
    Aufrufer — tests/test_ps_ausgabe.py verbietet das Zusammensetzen von Hand."""
    return _FARBCODE.sub("", ((proc.stdout or "") + "\n" + (proc.stderr or ""))).strip()
