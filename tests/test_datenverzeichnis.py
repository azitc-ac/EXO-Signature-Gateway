"""Kein Modul darf im Testlauf auf das echte Datenverzeichnis zeigen.

ANLASS (09.08.2026)
Der Pfad `/app/data` stand als Literal an 30 Stellen in 18 Modulen. Jeder Test
musste sein Modul einzeln umbiegen; wer eines vergass, schrieb während des
Laufs dorthin, wo CLIENT_SECRET, MAILBOX_CONFIG und die S/MIME-Privatschlüssel
liegen. Am 26.07.2026 ist das beinahe passiert.

Seit `config.DATA_DIR` genügt eine Stelle — `tests/conftest.py` setzt die
Umgebungsvariable, bevor das erste Anwendungsmodul importiert wird.

Diese Prüfung hält das fest. Sie schlägt fehl, sobald jemand wieder ein
Literal einbaut, und zwar BEVOR daraus ein beschädigtes Produktivverzeichnis
wird. Die üblichen Prüfungen sehen das nicht: Ein hartkodierter Pfad ist
syntaktisch einwandfrei, und solange das Verzeichnis auf dem
Entwicklungsrechner fehlt, fällt er auch im Betrieb nicht auf.
"""
import importlib
import re
from pathlib import Path

import pytest

import config

# Module, die einen Pfad unter dem Datenverzeichnis als Modulkonstante führen.
# Wächst die Anwendung, gehört der neue Eintrag hierher — genau das ist der
# Zweck: Die Liste zwingt dazu, die Frage überhaupt zu stellen.
_MODULE_UND_KONSTANTEN = [
    ("settings_store", ["SETTINGS_FILE"]),
    ("smime_store",    ["SMIME_DIR", "RECIPIENT_DIR"]),
    ("mail_audit",     ["DB_PATH"]),
    ("legal_consent",  ["_DB_PATH"]),
    ("portal_store",   ["_DB_PATH", "_BLOB_DIR", "_LOGO_PATH", "_LOGO_TYPE_PATH"]),
    ("held_mails",     ["_HELD_DIR"]),
    ("hub_orders",     ["_DIR"]),
    ("log_manager",    ["LOG_DIR"]),
    ("acme_state",     ["ACME_DIR"]),
    ("stats",          ["_STATS_FILE", "_DAILY_FILE"]),
    ("selfservice",    ["_TOKEN_FILE"]),
    ("smtp_acl",       ["_CACHE_FILE"]),
    ("exo_mailboxes",  ["_AUTH_CERT_PATH"]),
    ("health_check",   ["_AUTH_CERT_PATH"]),
    ("setup_wizard",   ["_AUTH_CERT_PATH"]),
    ("backup_manager", ["DATA_DIR"]),
]


def test_datenverzeichnis_ist_umgebogen():
    """Vorbedingung aller anderen Prüfungen hier."""
    assert config.DATA_DIR != "/app/data", \
        "conftest.py hat DATA_DIR nicht gesetzt — die Tests laufen gegen das echte Verzeichnis"


@pytest.mark.parametrize("modul,konstanten", _MODULE_UND_KONSTANTEN)
def test_kein_modul_zeigt_auf_das_echte_verzeichnis(modul, konstanten):
    m = importlib.import_module(modul)
    for name in konstanten:
        wert = str(getattr(m, name))
        assert not wert.startswith("/app/data"), (
            f"{modul}.{name} = {wert} — hartkodierter Pfad, im Testlauf würde "
            f"in das echte Datenverzeichnis geschrieben")
        assert wert.startswith(config.DATA_DIR), (
            f"{modul}.{name} = {wert} liegt ausserhalb von config.DATA_DIR")


def test_keine_neuen_literale_im_quelltext():
    """Die Gegenrichtung: der Blick in den Quelltext statt auf die Konstanten.

    Fängt auch Stellen, die keinen Modulwert setzen — etwa ein `open()` mitten
    in einer Funktion, das der Prüfung oben entginge.
    """
    erlaubt = {
        # Die Vorgabe selbst und ihre Erläuterung.
        "config.py",
        # Docstring-Beispiel zum Zip-Slip-Schutz, kein Dateizugriff.
        "secure_io.py",
    }
    app = Path(__file__).resolve().parent.parent / "app"
    treffer = []
    for datei in sorted(app.glob("*.py")):
        if datei.name in erlaubt:
            continue
        for nr, zeile in enumerate(datei.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r'["\']/app/data', zeile):
                treffer.append(f"{datei.name}:{nr}: {zeile.strip()}")
    assert not treffer, (
        "hartkodiertes Datenverzeichnis — bitte config.DATA_DIR verwenden:\n  "
        + "\n  ".join(treffer))
