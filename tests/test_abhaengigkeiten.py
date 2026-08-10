"""`requirements.txt` und `requirements.lock` dürfen nicht auseinanderlaufen.

ANLASS (09.08.2026)
`requirements.txt` pinnt die 11 direkt benutzten Pakete exakt. Alles, was nur
als deren Abhängigkeit mitkam, blieb frei — und wanderte. Bei identischer
Datei liefen vier verschiedene Starlette-Fassungen:

    Entwicklungsrechner   0.41.3
    Raspi-Container       1.3.1
    Azure-VM (produktiv)  1.4.0
    CI / frische venv     1.6.0

Ab 1.4 hängt `include_router()` einen Stellvertreter ein, statt die Routen
nach `app.routes` zu kopieren. Die Routen-Momentaufnahme wurde dadurch blind;
die CI meldete acht verlorene Adressen, während lokal alles grün war.

DIE GEFAHR, DIE HIER GEPRÜFT WIRD
---------------------------------
Eine Lock-Datei nützt nur, solange sie zum Gewollten passt. Wer die Fassung in
`requirements.txt` ändert und die Lock-Datei nicht neu erzeugt, ändert **gar
nichts** — installiert wird aus der Lock-Datei. Das ist die tückischere
Fehlerform: Die Absicht steht schriftlich da, das Ergebnis widerspricht ihr,
und niemand sieht es. Ein Sicherheits-Update von `cryptography` wäre dann
eingetragen, aber nicht installiert.
"""
import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app"
TXT = APP / "requirements.txt"
LOCK = APP / "requirements.lock"

_ZEILE = re.compile(r"^([A-Za-z0-9._-]+)\s*==\s*([^\s#]+)")


def _pins(pfad: Path) -> dict[str, str]:
    """Paketname (normalisiert) → Fassung. Kommentare und Leerzeilen ignoriert."""
    ergebnis = {}
    for zeile in pfad.read_text(encoding="utf-8").splitlines():
        zeile = zeile.split("#")[0].strip()
        m = _ZEILE.match(zeile)
        if m:
            # PyPI behandelt `-` und `_` gleich und ist bei Gross-/Kleinschreibung
            # unempfindlich: `pydantic_core` und `pydantic-core` sind dasselbe.
            ergebnis[m.group(1).lower().replace("_", "-")] = m.group(2)
    return ergebnis


def test_beide_dateien_existieren():
    assert TXT.is_file(), "requirements.txt fehlt"
    assert LOCK.is_file(), "requirements.lock fehlt — daraus wird installiert"


def test_lock_enthaelt_alles_aus_requirements():
    fehlen = sorted(set(_pins(TXT)) - set(_pins(LOCK)))
    assert not fehlen, (
        f"in requirements.txt gefordert, aber nicht in der Lock-Datei: {fehlen}\n"
        "Installiert wird aus der Lock-Datei — diese Pakete kämen also nicht mit.")


def test_fassungen_stimmen_ueberein():
    """Der eigentliche Punkt: Absicht und Abbild müssen dasselbe sagen."""
    txt, lock = _pins(TXT), _pins(LOCK)
    abweichend = {n: (txt[n], lock[n]) for n in txt if n in lock and txt[n] != lock[n]}
    assert not abweichend, (
        "requirements.txt und requirements.lock nennen verschiedene Fassungen:\n  "
        + "\n  ".join(f"{n}: gefordert {a}, installiert wird {b}"
                      for n, (a, b) in sorted(abweichend.items()))
        + "\n\nNach einer Änderung an requirements.txt die Lock-Datei neu erzeugen:"
          "\n  docker exec exo-signature-service pip freeze > app/requirements.lock")


def test_keine_offenen_fassungen():
    """`>=` in einer der beiden Dateien hebt die Festschreibung auf."""
    for pfad in (TXT, LOCK):
        offen = [z.strip() for z in pfad.read_text(encoding="utf-8").splitlines()
                 if not z.strip().startswith("#") and re.search(r"[><~]=", z)]
        assert not offen, f"{pfad.name} enthält offene Fassungsangaben: {offen}"


def test_lock_ist_deutlich_groesser():
    """Eine Lock-Datei, die nur die direkten Pakete enthält, ist keine.

    Genau das war der Zustand vorher: 11 gepinnte Pakete, während im Container
    34 lagen. Die 23 dazwischen waren die, die wanderten.
    """
    txt, lock = _pins(TXT), _pins(LOCK)
    assert len(lock) > len(txt), (
        f"Lock-Datei enthält {len(lock)} Pakete, requirements.txt {len(txt)} — "
        "die mitgezogenen Abhängigkeiten fehlen offenbar")


@pytest.mark.parametrize("paket", ["starlette", "pydantic", "anyio", "h11"])
def test_die_wanderer_sind_festgeschrieben(paket):
    """Namentlich die Pakete, die den Vorfall ausgelöst haben.

    Sie stehen in keiner requirements.txt und würde niemand von Hand eintragen —
    genau deshalb hier festgehalten. Fällt einer heraus, ist die Lock-Datei
    unvollständig erzeugt worden.
    """
    assert paket in _pins(LOCK), f"{paket} fehlt in der Lock-Datei"


# ── Das Abbild selbst ────────────────────────────────────────────────────────
#
# ANLASS (10.08.2026): Die Frage des Betreibers — bekommt ein Kunde, der
# demnächst frisch installiert, exakt den geprüften Stand? Für die
# Python-Pakete ja, für den Rest nein. Drei Bestandteile wanderten weiter:
# das Basisabbild, die Debian-Pakete und ExchangeOnlineManagement.
#
# Das letzte war der Brocken: `Install-Module` ohne Fassungsangabe holt, was
# der Katalog gerade anbietet — und dieses Modul steuert Verteilerlisten und
# Transportregeln.

DOCKERFILE = APP.parent / "Dockerfile"


def test_basisabbild_haengt_am_digest():
    """Ein Tag bewegt sich. `python:3.11-slim` lieferte über die Monate
    verschiedene Python- und Debian-Stände."""
    froms = [z.strip() for z in _dockerfile_ohne_kommentare().splitlines()
             if z.strip().startswith("FROM ")]
    assert froms, "kein FROM im Dockerfile gefunden"
    for zeile in froms:
        assert "@sha256:" in zeile, (
            f"Basisabbild ohne Digest: {zeile}\n"
            "Neuen Digest holen: docker buildx imagetools inspect python:3.11-slim")


def _dockerfile_ohne_kommentare() -> str:
    """Nur die wirksamen Zeilen.

    Beim ersten Anlauf suchte die Prüfung im Gesamttext und fand
    `Install-Module` in der ERKLÄRUNG darüber statt im Befehl — eine Prüfung,
    die auf einen Kommentar anspricht, ist wertlos. Denselben Fehler hatte
    `test_stilregeln.py` schon einmal (dort mit `max-height` im CSS-Kommentar).
    """
    return "\n".join(z for z in DOCKERFILE.read_text(encoding="utf-8").splitlines()
                     if not z.strip().startswith("#"))


def test_exchange_modul_hat_eine_fassung():
    """Ohne `-RequiredVersion` bekommt jeder Bau eine andere Fassung des
    Moduls, das die gesamte Exchange-Verwaltung steuert."""
    text = _dockerfile_ohne_kommentare()
    assert "Install-Module" in text, "kein Install-Module im Dockerfile — umbenannt?"
    # Der Aufruf laeuft ueber mehrere Zeilen; ab der Fundstelle weitersuchen.
    block = text[text.index("Install-Module"):]
    assert "-RequiredVersion" in block[:300], (
        "Install-Module ohne -RequiredVersion — die Fassung des "
        "Exchange-Moduls waere dem Zufall des Bauzeitpunkts überlassen")


def test_powershell_hat_eine_fassung():
    assert re.search(r'PS_VERSION="\d+\.\d+\.\d+"', _dockerfile_ohne_kommentare()), \
        "PowerShell-Fassung nicht festgelegt"
