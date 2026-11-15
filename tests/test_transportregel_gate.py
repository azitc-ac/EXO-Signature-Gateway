"""Die Gateway-Transportregel darf NIE ungefiltert Mail fangen.

Eine Regel ohne FromMemberOf (nur `FromScope InOrganization`) matcht JEDEN
internen Absender und leitet den gesamten internen Mailverkehr durchs Gateway.
Kann es nicht zustellen (z.B. noch nicht fertig konfiguriert), staut/verwirft
Exchange die Post — ein Totalausfall. Genau das trat bei einer Erstinstallation
auf: die Regel entstand aktiv und ungefiltert, und der Null-Postfach-Fall leerte
das Gate zusätzlich (`-FromMemberOf $null`), statt es zu schließen.

Zwei Ausfallsicherungen, die das zusammen verhindern:
1. Neu angelegt wird die Regel DEAKTIVIERT (setup_exo_connector.ps1) — dort hat
   sie noch kein FromMemberOf-Gate.
2. Das Gate ist IMMER die DG, wird NIE geleert (update_mailbox_dg.ps1); bei null
   Postfächern zeigt es auf eine leere DG und die Regel wird deaktiviert.
"""
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
SKRIPTE = WURZEL / "app" / "scripts"


def _anweisungen(text: str) -> list[str]:
    """PowerShell-Anweisungen ohne Kommentarzeilen, Backtick-Fortsetzungen
    zusammengeführt. Bis v1.12.19 suchten die Tests im Rohtext — im
    Aktualisieren-Zweig stand `-FromMemberOf @($dgName)` auch in einem
    Kommentar, und der Test blieb grün, wenn die echte Codezeile fehlte."""
    zeilen = [z for z in text.splitlines() if not z.lstrip().startswith("#")]
    anweisungen, teil = [], ""
    for z in zeilen:
        roh = z.rstrip()
        if roh.endswith("`"):
            teil += roh[:-1] + " "
            continue
        anweisungen.append(" ".join((teil + roh).split()))
        teil = ""
    return [a for a in anweisungen if a]


def _aufrufe(anweisungen: list[str], cmdlet: str) -> list[str]:
    return [a for a in anweisungen if a.startswith(cmdlet + " ")]


def _zweig(text: str, anfang: str, ende: str) -> list[str]:
    """Anweisungen zwischen zwei Ankern — Anker im CODE gesucht, nicht im Kommentar."""
    a = _anweisungen(text)
    i = next(n for n, x in enumerate(a) if x.startswith(anfang))
    j = next(n for n, x in enumerate(a) if n > i and x.startswith(ende))
    return a[i:j]


def test_connector_legt_regel_mit_gate_und_deaktiviert_an():
    """New-TransportRule muss das FromMemberOf-Gate als festen Bestandteil tragen
    UND die Regel deaktiviert anlegen (doppelt ausfallsicher)."""
    s = (SKRIPTE / "setup_exo_connector.ps1").read_text()
    anlage = _aufrufe(_anweisungen(s), "New-TransportRule")
    assert len(anlage) == 1, anlage
    assert "-FromMemberOf @($dgName)" in anlage[0], "DL-Gate fehlt bei der Anlage"
    assert "-Enabled $false" in anlage[0], "Regel wird NICHT deaktiviert angelegt"
    assert "-SentTo" not in anlage[0], "empfängerbezogene Bedingung bifurkiert die interne Fork"


def test_connector_legt_die_dg_vor_der_regel_an():
    """Das Gate braucht die DG — sie wird im Connector-Skript sichergestellt."""
    s = (SKRIPTE / "setup_exo_connector.ps1").read_text()
    assert "New-DistributionGroup" in s
    # DG-Anlage muss VOR der Regel-Anlage stehen.
    assert s.index("New-DistributionGroup") < s.index("New-TransportRule")


def test_connector_update_stellt_gate_wieder_her():
    """Der Aktualisieren-Zweig heilt ein abhandengekommenes Gate."""
    s = (SKRIPTE / "setup_exo_connector.ps1").read_text()
    # Set-TransportRule im existingRule-Zweig setzt FromMemberOf — geprüft am
    # AUFRUF, nicht am Block (dort steht derselbe Text im Kommentar).
    zweig = _zweig(s, "if ($existingRule)", "} else {")
    setzen = _aufrufe(zweig, "Set-TransportRule")
    assert setzen, "kein Set-TransportRule im Aktualisieren-Zweig"
    assert any("-FromMemberOf @($dgName)" in a for a in setzen), setzen


def test_split_skript_haelt_dieselben_invarianten():
    """setup_rule_split.ps1 (Phase 1) darf keine Regel aktiv+ungegatet lassen."""
    s = (SKRIPTE / "setup_rule_split.ps1").read_text()
    code = "\n".join(z for z in s.splitlines() if not z.lstrip().startswith("#"))
    # Gate wird nie geleert.
    assert "-FromMemberOf $null" not in code
    # Gate immer auf die DG gesetzt; Aktivierung nach Mitgliederzahl.
    assert any("-FromMemberOf @($dgName)" in a
               for a in _aufrufe(_anweisungen(s), "Set-TransportRule"))   # in Set-RuleGate
    assert "Enable-TransportRule" in code
    assert "Disable-TransportRule" in code
    # Neue S/MIME-Regel: gegated + deaktiviert angelegt, keine Empfaengerbedingung.
    anlage = " ".join(_aufrufe(_anweisungen(s), "New-TransportRule"))
    assert "-FromMemberOf @($smimeDg)" in anlage
    assert "-Enabled $false" in anlage
    assert "-SentToScope" not in code
    # Array-Flattening-Falle: @() ZWINGEND an der Zuweisung (sonst .Count-Fehler
    # unter StrictMode bei Ein-Element-Liste; im Dev-Live-Test aufgetreten).
    assert "@(Split-List $SigMembers)" in code
    assert "@(Split-List $SmimeMembers)" in code


def test_dg_update_leert_das_gate_nie():
    """Das FromMemberOf-Gate darf nie geleert werden; Null-Postfach → deaktivieren."""
    s = (SKRIPTE / "update_mailbox_dg.ps1").read_text()
    # Kommentarzeilen ausblenden — der erklärende Text nennt den alten Fall.
    code = "\n".join(z for z in s.splitlines() if not z.lstrip().startswith("#"))
    # Der fail-dangerous Fall darf im CODE nicht zurückkehren.
    assert "-FromMemberOf $null" not in code, "-FromMemberOf $null öffnet die Regel für ALLE"
    # Das Gate wird immer auf die DG gesetzt — am Aufruf geprüft, nicht im Text.
    a = _anweisungen(s)
    assert any("-FromMemberOf @($dgName)" in x for x in _aufrufe(a, "Set-TransportRule"))
    # Aktivieren nur mit Postfächern, sonst deaktivieren.
    assert _aufrufe(a, "Enable-TransportRule")
    assert _aufrufe(a, "Disable-TransportRule")
