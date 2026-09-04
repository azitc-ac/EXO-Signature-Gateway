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


def test_connector_legt_regel_deaktiviert_an():
    """New-TransportRule muss die Regel deaktiviert anlegen (kein Gate vorhanden)."""
    s = (SKRIPTE / "setup_exo_connector.ps1").read_text()
    i = s.index("New-TransportRule")
    j = s.index("Out-Null", i)
    anlege_block = s[i:j]
    assert "-Enabled $false" in anlege_block, "Regel wird NICHT deaktiviert angelegt"


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
