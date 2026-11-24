"""Eigene Hosts-Einträge: Namen im Gateway auf feste Adressen auflösen.

ANLASS (2026-11-17)
-------------------
Ein On-Prem-Ziel war als `mail.firma.at` eingetragen. Der Name löste auf
exohv nur öffentlich auf; die Probe lief über die öffentliche Adresse und
die Firewall zurück ins eigene Netz. Fällt im Ernstfall der Internet-
anschluss aus, trägt dieser Weg nicht. Eine IP-Adresse als Ziel hätte die
Zertifikatsprüfung gekostet (der Name muss zum Zertifikat passen). Lokal
gesplittetes DNS ist nicht überall zu haben — dafür diese Tabelle.

WIE ES WIRKT
------------
`einschalten()` legt sich beim Start vor `socket.getaddrinfo`: Steht der
gefragte Name in der Tabelle (Einstellung HOSTS_EINTRAEGE, Format der
hosts-Datei), wird stattdessen die eingetragene Adresse aufgelöst. Der Name
bleibt beim Aufrufer — TLS prüft weiter gegen ihn (SNI, Zertifikat). Das
gilt für jede Verbindung des Gateway-Prozesses (Weiterleitungsziele,
On-Prem-Probe, Domänencontroller, Graph …), nicht für den Postfix-
Direktversand (eigener Container) und nicht für PowerShell.
Wirkt sofort nach dem Speichern, ohne Neustart. Jede erste Nutzung eines
Eintrags steht im Protokoll.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
import threading

log = logging.getLogger(__name__)

SCHLUESSEL = "HOSTS_EINTRAEGE"
MAX_ZEILEN = 200
_NAME = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?:\.(?!-)[a-z0-9-]{1,63})*$")

_original = socket.getaddrinfo
_gemeldet: set[tuple[str, str]] = set()
_lock = threading.Lock()


def zerlegen(text: str) -> tuple[dict[str, str], list[str]]:
    """({name: adresse}, [Fehler je Zeile]). Format wie /etc/hosts: Adresse,
    dann ein oder mehrere Namen; `#` leitet einen Kommentar ein. Namen ohne
    Groß-/Kleinschreibung und ohne abschließenden Punkt."""
    eintraege: dict[str, str] = {}
    fehler: list[str] = []
    zeilen = (text or "").replace("\r\n", "\n").split("\n")
    if len(zeilen) > MAX_ZEILEN:
        return {}, [f"Höchstens {MAX_ZEILEN} Zeilen."]
    for nr, zeile in enumerate(zeilen, 1):
        teile = zeile.split("#", 1)[0].split()
        if not teile:
            continue
        try:
            adresse = str(ipaddress.ip_address(teile[0]))
        except ValueError:
            fehler.append(f"Zeile {nr}: „{teile[0][:60]}“ ist keine IP-Adresse.")
            continue
        if len(teile) < 2:
            fehler.append(f"Zeile {nr}: Nach der Adresse fehlt der Name.")
            continue
        for name in teile[1:]:
            n = name.lower().rstrip(".")
            if not _NAME.match(n):
                fehler.append(f"Zeile {nr}: „{name[:60]}“ ist kein gültiger Name.")
            elif n in eintraege and eintraege[n] != adresse:
                fehler.append(f"Zeile {nr}: „{n}“ steht schon mit {eintraege[n]} da.")
            else:
                eintraege[n] = adresse
    return eintraege, fehler


def normalisieren(text: str) -> str:
    """Für das Speichern: Fehler → ValueError mit allen Zeilen, sonst eine
    Zeile je Adresse (Namen kleingeschrieben, Reihenfolge wie eingegeben)."""
    eintraege, fehler = zerlegen(text)
    if fehler:
        raise ValueError(" ".join(fehler))
    je_adresse: dict[str, list[str]] = {}
    for name, adresse in eintraege.items():
        je_adresse.setdefault(adresse, []).append(name)
    return "\n".join(f"{a}  {' '.join(n)}" for a, n in je_adresse.items())


def tabelle() -> dict[str, str]:
    import settings_store
    eintraege, _ = zerlegen(settings_store.get(SCHLUESSEL) or "")
    return eintraege


def _getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if isinstance(host, (str, bytes)):
        name = (host.decode("ascii", "ignore") if isinstance(host, bytes) else host).lower().rstrip(".")
        try:
            adresse = tabelle().get(name)
        except Exception:                                      # noqa: BLE001 — Auflösung nie blockieren
            adresse = None
        if adresse:
            with _lock:
                neu = (name, adresse) not in _gemeldet
                _gemeldet.add((name, adresse))
            if neu:
                log.info("Hosts-Eintrag: %s → %s (statt DNS)", name, adresse)
            return _original(adresse, port, family, type, proto, flags)
    return _original(host, port, family, type, proto, flags)


def einschalten() -> None:
    """Einmal beim Start (main.py). Mehrfacher Aufruf ist unschädlich."""
    if socket.getaddrinfo is not _getaddrinfo:
        socket.getaddrinfo = _getaddrinfo
