"""Assistent: Nach dem Umbenennen des Gateways lädt die Seite neu.

Der Name steht serverseitig gerendert in den folgenden Schritten, etwa als
„<Name> Login“ zum Kopieren in Entra. Ohne Neuladen legte man dort Objekte
unter dem alten Namen an.
"""
import re
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent / "app/webui/templates/setup.html"


def test_umbenennen_laedt_die_seite_neu():
    text = SETUP.read_text(encoding="utf-8")
    m = re.search(r"async function saveWizardGatewayName\(\) \{(.*?)\n\}", text, re.S)
    assert m, "saveWizardGatewayName fehlt"
    rumpf = m.group(1)
    assert re.search(r"if \(ok\) setTimeout\(\(\) => location\.reload\(\)", rumpf), rumpf
    assert "const ok = await savePartial({GATEWAY_NAME" in rumpf
