"""Azure Instance Metadata Service (IMDS) — best effort, nie blockierend.

ANLASS
---------------------------
Mehrere Stellen wollen wissen, WO die VM läuft (Region, Ressourcengruppe) und
unter welcher öffentlichen IP sie erreichbar ist:

  - Der Key-Vault-Schritt soll Region und Ressourcengruppe der VM vorschlagen
    statt einer hartkodierten Region.
  - Die Abnahme will prüfen, ob der öffentliche Name auf diese VM zeigt.

IMDS beantwortet das lokal, ohne Zugangsdaten, über eine Link-Local-Adresse.
Es gibt IMDS aber nur auf Azure — überall sonst schlägt der Aufruf fehl. Jede
Funktion liefert deshalb einen Rückfallwert (leer/None) und hängt NICHT: die
Aufrufer laufen teils synchron in einer Web-Anfrage.
"""
from __future__ import annotations

import json
import logging
import urllib.request

log = logging.getLogger(__name__)

_BASE = "http://169.254.169.254/metadata/instance"
_TIMEOUT = 1.5

# Einmal je Prozess ermittelt — die Antwort ändert sich zur Laufzeit nicht, und
# ein Fehlschlag (nicht-Azure) soll nicht bei jeder Anfrage erneut warten.
_cache: dict | None = None


def _abruf(pfad: str, *, text: bool = False) -> str | dict | None:
    url = f"{_BASE}{pfad}?api-version=2021-02-01" + ("&format=text" if text else "")
    try:
        req = urllib.request.Request(url, headers={"Metadata": "true"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:   # noqa: S310
            rohdaten = resp.read().decode()
        return rohdaten.strip() if text else json.loads(rohdaten)
    except Exception:                                                  # noqa: BLE001
        return None


def compute() -> dict:
    """`compute`-Abschnitt der Instanz-Metadaten (`{}` außerhalb Azure)."""
    global _cache
    if _cache is None:
        daten = _abruf("/compute")
        _cache = daten if isinstance(daten, dict) else {}
    return _cache


def location() -> str:
    """Azure-Region der VM, z. B. `northeurope` — `""` außerhalb Azure."""
    return (compute().get("location") or "").strip()


def resource_group() -> str:
    """Ressourcengruppe der VM — `""` außerhalb Azure."""
    return (compute().get("resourceGroupName") or "").strip()


def public_ip() -> str | None:
    """Öffentliche IP dieser VM — `None`, wenn nicht ermittelbar."""
    ip = _abruf(
        "/network/interface/0/ipv4/ipAddress/0/publicIpAddress", text=True)
    return ip or None if isinstance(ip, str) else None
