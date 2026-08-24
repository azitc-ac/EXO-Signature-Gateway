"""Was ein Erklärtext über die Vorgabe sagt, muss die Vorgabe auch sein.

ANLASS (2026-08-24)
-------------------
Stufe 4 des Konsistenz-Verfahrens: Ein Satz kann in kanonischen Begriffen
formuliert und trotzdem falsch sein. `begriffecheck.py` prüft Wörter, nicht
Aussagen — es fand einen erfundenen Modusnamen im Einrichtungsassistenten,
hätte aber „Vorgabe 25" auch dann durchgelassen, wenn die Vorgabe längst 587
wäre.

(Der erfundene Name steht hier bewusst nicht wörtlich: Sonst müsste diese Datei
in die Ausnahmeliste des Begriffsprüfers, und jede weitere Ausnahme verwässert
ihn. Ein Beispiel lässt sich auch beschreiben, statt es zu zitieren.)

Semantik lässt sich nicht allgemein prüfen. **Eine Klasse von Zusagen aber
schon:** Sätze, die einen Vorgabewert nennen. Sie sind zahlreich (neun in der
Oberfläche), sie veralten still, und ihr Bruch ist für den Betreiber
unmittelbar irreführend — er stellt etwas ein, weil er den vermeintlichen
Ausgangszustand kennt.

Deshalb hier eine Tabelle von Hand: Text ↔ Einstellung ↔ erwarteter Wert. Die
Zuordnung maschinell zu erraten wäre unzuverlässig; sie einmal aufzuschreiben
kostet wenig und hält.

WAS DIESER TEST NICHT KANN
--------------------------
Er prüft, ob die genannte Vorgabe stimmt — nicht, ob der Satz drumherum wahr
ist. „Standard: aus. Bereits versorgte Postfächer werden übersprungen" ist zur
Hälfte hier abgedeckt; die zweite Hälfte deckt
`tests/test_auto_enrollment.py` ab. Wer eine Zusage macht, die sich nicht auf
einen Vorgabewert reduzieren lässt, braucht einen eigenen Test dafür.
"""
import re
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import settings_store  # noqa: E402

VORLAGEN = WURZEL / "app" / "webui" / "templates"

# (Vorlage, Einstellung, Textstelle die dort stehen MUSS, erwartete Vorgabe)
#
# Die Textstelle ist bewusst wörtlich: Ändert jemand die Formulierung, fällt
# der Eintrag auf und wird mitgepflegt — statt still auf eine Stelle zu zeigen,
# die es nicht mehr gibt.
ZUSAGEN = [
    ("advanced.html", "EXO_PORT",
     "Vorgabe <code>25</code>", 25),
    ("advanced.html", "SMTP_SUBMIT_HOST",
     "Vorgabe <code>smtp.office365.com</code>", "smtp.office365.com"),
    ("advanced.html", "SMTP_SUBMIT_PORT",
     "Vorgabe <code>587</code>", 587),
    ("advanced.html", "GRAPH_SMTP_FALLBACK",
     "Standardmäßig <strong>aus</strong>", False),
    ("settings_smime.html", "SMIME_SIGNING_ENABLED",
     "Standard: eingeschaltet", True),
    ("settings_smime.html", "SMIME_AUTO_ENROLL",
     "Standard: aus", False),
    ("settings_smime.html", "SECURE_PORTAL_OTP",
     "Standard: ein", True),
    ("settings_smime.html", "SECURE_PORTAL_RETENTION_DAYS",
     "Standard: 14", 14),
    ("settings_smime.html", "SECURE_PORTAL_ENABLED",
     "Standard: aus — dann wird wie bisher ein Unzustellbarkeitsbericht", False),
    # Auswahlfelder: die als „Standard" ausgezeichnete Option muss die sein,
    # die DEFAULTS liefert — sonst zeigt die Oberflaeche beim ersten Aufruf
    # etwas anderes an, als daneben behauptet wird.
    ("settings_smime.html", "SMIME_TAG_POSITION",
     "Vorne (Standard)", "prepend"),
    ("advanced.html", "GRAPH_MIXED_FORK_MODE",
     "Vollständige Empfängerliste (Standard, empfohlen)", "send_to_all"),
    ("setup.html", "REINJECT_MODE",
     "<strong>SMTP Port 25</strong> — Standard", "smtp"),
]


@pytest.mark.parametrize("datei,schluessel,text,erwartet", ZUSAGEN,
                         ids=[f"{z[1]}" for z in ZUSAGEN])
def test_genannte_vorgabe_stimmt(datei, schluessel, text, erwartet):
    """Der Erklärtext nennt eine Vorgabe — und DEFAULTS sagt dasselbe."""
    tatsaechlich = settings_store.DEFAULTS[schluessel]
    assert tatsaechlich == erwartet, (
        f"{datei} sagt zu {schluessel}: »{text}« — die Vorgabe ist aber "
        f"{tatsaechlich!r}, nicht {erwartet!r}.\n"
        "Entweder den Erklärtext berichtigen oder diese Tabelle nachziehen. "
        "Ein Text, der die falsche Vorgabe nennt, ist schlimmer als keiner: "
        "Der Betreiber stellt etwas ein, weil er den Ausgangszustand zu "
        "kennen glaubt.")


@pytest.mark.parametrize("datei,schluessel,text,erwartet", ZUSAGEN,
                         ids=[f"{z[1]}" for z in ZUSAGEN])
def test_die_textstelle_gibt_es(datei, schluessel, text, erwartet):
    """Gegenrichtung: Zeigt die Tabelle auf eine Stelle, die es noch gibt?

    Ohne diese Prüfung könnte jemand den Erklärtext löschen, und der Test oben
    bliebe grün — er würde dann eine Zusage bewachen, die niemand mehr macht.
    """
    inhalt = (VORLAGEN / datei).read_text("utf-8")
    # Zeilenumbrüche und Mehrfach-Leerzeichen sind in Vorlagen beliebig
    norm = re.sub(r"\s+", " ", inhalt)
    assert re.sub(r"\s+", " ", text) in norm, (
        f"In {datei} steht »{text}« nicht mehr. Wurde der Erklärtext "
        f"umformuliert, gehört der Eintrag zu {schluessel} in dieser Tabelle "
        "nachgezogen — sonst bewacht der Test eine Zusage, die es nicht "
        "mehr gibt.")
