"""Links zum Azure-Marketplace-Angebot: überall dieselben, Vorschau gekennzeichnet.

Bis zum Go live stehen in README und docs/ die Links der Vorschau. Sie
funktionieren nur für die Vorschau-Zielgruppe. Damit beim Tausch keine Stelle
übrig bleibt:

1. Alle Dateien nennen dieselben zwei Links (Portal und Angebotsseite).
2. Ein Vorschau-Link steht genau dort, wo der Kommentar MARKETPLACE-VORSCHAU
   steht, und umgekehrt. Wer die Links tauscht, muss den Kommentar entfernen;
   wer ihn entfernt, muss die Links getauscht haben.
"""
import re
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
DATEIEN = [WURZEL / "README.md", *sorted((WURZEL / "docs").glob("*.md"))]
MUSTER = re.compile(r"https://(?:portal\.azure\.com/#create/azitc\.[\w.-]+"
                    r"|marketplace\.microsoft\.com/[\w/-]+/azitc\.[\w.-]+)")
MARKE = "MARKETPLACE-VORSCHAU"


def _links():
    return {p: set(MUSTER.findall(p.read_text(encoding="utf-8"))) for p in DATEIEN}


def test_ueberall_dieselben_zwei_links():
    mit = {p.name: l for p, l in _links().items() if l}
    assert {"README.md", "installation.md"} <= set(mit), mit
    saetze = {frozenset(l) for l in mit.values()}
    assert len(saetze) == 1, mit
    (satz,) = saetze
    assert len(satz) == 2 and sum("portal.azure.com" in u for u in satz) == 1, satz


def test_vorschau_nur_mit_kennzeichnung():
    for p, links in _links().items():
        text = p.read_text(encoding="utf-8")
        vorschau = any("preview" in u for u in links)
        assert vorschau == (MARKE in text), (
            f"{p.name}: Vorschau-Link={vorschau}, Kommentar {MARKE}={MARKE in text}")


def test_muster_findet_die_links():
    """Ohne Treffer wären beide Prüfungen oben leer."""
    assert MUSTER.search("x https://portal.azure.com/#create/azitc.a-previewb-vm)")
    assert MUSTER.search("https://marketplace.microsoft.com/de-de/product/"
                         "azure-application/azitc.a-preview)")
