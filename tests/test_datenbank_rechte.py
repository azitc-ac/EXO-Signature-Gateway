"""Eine zur Laufzeit entstehende Datenbank ist ab dem ersten Moment 600.

ANLASS (2026-08-25)
-------------------
Beim Bau der Relay-Geräteliste gemessen: SQLite legt seine Datei mit der umask
des Prozesses an — im Container 022, also **644**. `secure_io.harden_tree()`
räumt das auf, läuft aber nur beim Start.

⚠️ Warum das im Bestand niemandem auffiel: Jede vorhandene `.db` hat längst
einen Neustart erlebt. Ein `ls -la` im laufenden Betrieb zeigt überall 600 und
bestätigt scheinbar, dass alles stimmt. Betroffen ist ausschliesslich das
Zeitfenster zwischen dem ERSTEN Schreiben und dem nächsten Start — bei
`portal.db` (Nachrichteninhalte) und `mail_audit.db` (Absender, Empfänger,
Betreffzeilen) ist genau das der Moment nach einer Neuinstallation.

Es ist dieselbe Klasse wie beim atomaren Schreiben von `settings.json`
(CLAUDE.md, Secret-Speicherorte): Die Rechte müssen dort gesetzt werden, wo die
Datei ENTSTEHT, nicht dort, wo man später hinsieht.

Der Test setzt die umask absichtlich auf 022 — mit der laufzeitüblichen umask
des Entwicklungsrechners (oft 002 oder 022) wäre er sonst je nach Umgebung
aussagekräftig oder nicht.
"""
import os
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

# Modul → Attribut, das den Pfad hält.
#
# Angestossen wird über `_conn()` — und zwar bei allen fünf gleich. Das ist
# genau die Stelle, an der die Datei entsteht; über eine fachliche Funktion zu
# gehen (`init_db()`, `merken()`, …) hiesse, für jedes Modul einen anderen Weg
# zu wählen und dabei die Frage zu verschieben, ob der Test überhaupt dorthin
# kommt, wo `harden_file` steht.
DATENBANKEN = [
    ("relay_hosts", "DB_PATH"),
    ("sig_thread", "DB_PATH"),
    ("mail_audit", "DB_PATH"),
    ("portal_store", "_DB_PATH"),
    ("legal_consent", "_DB_PATH"),
]


@pytest.mark.parametrize("modul,attribut",
                         DATENBANKEN, ids=[d[0] for d in DATENBANKEN])
def test_frisch_angelegte_datenbank_ist_600(modul, attribut, tmp_path, monkeypatch):
    m = __import__(modul)
    ziel = tmp_path / f"{modul}.db"
    monkeypatch.setattr(m, attribut, ziel)

    alt = os.umask(0o022)          # die umask des Containers, nicht die lokale
    try:
        # `_conn()` ist mal eine nackte Verbindung, mal ein Kontextmanager
        # (portal_store) — beides wird sauber wieder geschlossen.
        verbindung = m._conn()
        if hasattr(verbindung, "close"):
            verbindung.close()
        else:
            with verbindung:
                pass
    finally:
        os.umask(alt)

    assert ziel.exists(), f"{modul} hat keine Datenbank angelegt — Test wirkungslos"
    rechte = ziel.stat().st_mode & 0o777
    assert rechte == 0o600, (
        f"{modul} legt seine Datenbank mit {oct(rechte)} an. Sie enthält "
        "Betriebsdaten und ist bis zum nächsten Neustart für jeden "
        "Systembenutzer lesbar — `harden_tree()` greift erst beim Start.\n"
        "Abhilfe: `secure_io.harden_file(<pfad>)` direkt nach `sqlite3.connect`.")


def test_harden_file_ist_idempotent_und_leise(tmp_path):
    """Zweimal aufrufen ändert nichts; eine fehlende Datei ist kein Fehler.

    Beides ist nötig, weil der Aufruf in jedem `_conn()` steckt — also bei
    jedem Datenbankzugriff, auch wenn die Datei längst stimmt.
    """
    import secure_io
    datei = tmp_path / "probe.db"
    datei.write_bytes(b"x")
    datei.chmod(0o644)

    secure_io.harden_file(datei)
    assert datei.stat().st_mode & 0o777 == 0o600
    secure_io.harden_file(datei)
    assert datei.stat().st_mode & 0o777 == 0o600

    # Darf nicht werfen — sonst bräche der erste Zugriff, bevor SQLite die
    # Datei überhaupt anlegen konnte.
    secure_io.harden_file(tmp_path / "gibtsnicht.db")
