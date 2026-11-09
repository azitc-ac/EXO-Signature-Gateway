"""Die Abnahme meldet Befunde, keine Dauerfloskeln (09.11.2026).

„Nicht geprüft: … zeigt erst der Betrieb“ stand an sechs Punkten, dazu drei
Lücken, die längst geschlossen waren (Secrets in Entra, Schlüsselpasswort im
Backup). Für den Betreiber las sich das wie eine Mängelliste ohne Aussicht auf
Besserung — und eine Liste, die immer da ist, liest niemand. `deckt_nicht`
bleibt für den Einzelfall, in dem eine Prüfung wirklich nicht möglich war.
"""
import re
from pathlib import Path

QUELLE = Path(__file__).resolve().parent.parent / "app" / "abnahme.py"
FLOSKELN = re.compile(r"zeigt erst|erst im Betrieb|erst der Betrieb|erst ein Versuch",
                      re.IGNORECASE)


def test_keine_dauerfloskeln_in_der_abnahme():
    code = [z for z in QUELLE.read_text("utf-8").splitlines()
            if not z.lstrip().startswith("#")]
    treffer = [z.strip() for z in code if FLOSKELN.search(z)]
    assert not treffer, f"Floskel statt Befund: {treffer}"


def test_ohne_ausnahme_hoechstens_wenige_dauerhinweise():
    """Ein `deckt_nicht` mit festem Text erscheint IMMER, wenn der Punkt grün
    ist. Mehr als eine Handvoll davon ist wieder die Mängelliste."""
    text = QUELLE.read_text("utf-8")
    fest = re.findall(r'deckt_nicht="', text)
    assert len(fest) <= 3, f"{len(fest)} feste Hinweise — jeden einzeln rechtfertigen"
