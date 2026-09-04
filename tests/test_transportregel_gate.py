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


def test_connector_legt_regel_mit_gate_und_deaktiviert_an():
    """New-TransportRule muss das FromMemberOf-Gate als festen Bestandteil tragen
    UND die Regel deaktiviert anlegen (doppelt ausfallsicher)."""
    s = (SKRIPTE / "setup_exo_connector.ps1").read_text()
    i = s.index("New-TransportRule")
    j = s.index("Out-Null", i)
    anlege_block = s[i:j]
    assert "-FromMemberOf @($dgName)" in anlege_block, "DL-Gate fehlt bei der Anlage"
    assert "-Enabled $false" in anlege_block, "Regel wird NICHT deaktiviert angelegt"


def test_connector_legt_die_dg_vor_der_regel_an():
    """Das Gate braucht die DG — sie wird im Connector-Skript sichergestellt."""
    s = (SKRIPTE / "setup_exo_connector.ps1").read_text()
    assert "New-DistributionGroup" in s
    # DG-Anlage muss VOR der Regel-Anlage stehen.
    assert s.index("New-DistributionGroup") < s.index("New-TransportRule")


def test_connector_update_stellt_gate_wieder_her():
    """Der Aktualisieren-Zweig heilt ein abhandengekommenes Gate."""
    s = (SKRIPTE / "setup_exo_connector.ps1").read_text()
    # Set-TransportRule im existingRule-Zweig setzt FromMemberOf.
    i = s.index("if ($existingRule)")
    j = s.index("} else {", i)
    update_block = s[i:j]
    assert "-FromMemberOf @($dgName)" in update_block


def test_split_skript_haelt_dieselben_invarianten():
    """setup_rule_split.ps1 (Phase 1) darf keine Regel aktiv+ungegatet lassen."""
    s = (SKRIPTE / "setup_rule_split.ps1").read_text()
    code = "\n".join(z for z in s.splitlines() if not z.lstrip().startswith("#"))
    # Gate wird nie geleert.
    assert "-FromMemberOf $null" not in code
    # Gate immer auf die DG gesetzt; Aktivierung nach Mitgliederzahl.
    assert "-FromMemberOf @($dgName)" in code            # in Set-RuleGate
    assert "Enable-TransportRule" in code
    assert "Disable-TransportRule" in code
    # Neue S/MIME-Regel: gegated + deaktiviert angelegt, keine Empfaengerbedingung.
    i = code.index("New-TransportRule")
    j = code.index("Out-Null", i)
    anlage = code[i:j]
    assert "-FromMemberOf @($smimeDg)" in anlage
    assert "-Enabled $false" in anlage
    assert "-SentToScope" not in code


def test_dg_update_leert_das_gate_nie():
    """Das FromMemberOf-Gate darf nie geleert werden; Null-Postfach → deaktivieren."""
    s = (SKRIPTE / "update_mailbox_dg.ps1").read_text()
    # Kommentarzeilen ausblenden — der erklärende Text nennt den alten Fall.
    code = "\n".join(z for z in s.splitlines() if not z.lstrip().startswith("#"))
    # Der fail-dangerous Fall darf im CODE nicht zurückkehren.
    assert "-FromMemberOf $null" not in code, "-FromMemberOf $null öffnet die Regel für ALLE"
    # Das Gate wird immer auf die DG gesetzt.
    assert "-FromMemberOf @($dgName)" in s
    # Aktivieren nur mit Postfächern, sonst deaktivieren.
    assert "Enable-TransportRule" in s
    assert "Disable-TransportRule" in s
