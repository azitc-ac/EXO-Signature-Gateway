"""Keine Kennungen der eigenen Betriebsumgebung im öffentlichen Repository.

Hostnamen der eigenen Gateways, Tenant-Namen, öffentliche IP-Adressen und
App-/Tenant-IDs standen als Beispiele in Kommentaren, Doku und Tests. Sie
verraten, wo ein Gateway dieses Projekts erreichbar ist, und tragen als
Beispiel nichts bei, was `contoso.com` nicht auch trüge.

Die verbotenen Werte stehen hier nur als SHA-256-Präfix — sonst stünde genau
das im Repository, was der Test fernhält. Neuen Wert sperren:
    python3 -c "import hashlib;print(hashlib.sha256(b'wert').hexdigest()[:16])"

Bewusst NICHT gesperrt: die Domänen selbst (Kontaktadressen, Rechtstexte,
Hub-Adresse sind öffentlich und gewollt).
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent

GESPERRT = {
    "fbccbe1e6c965257", "628bf1c95e9c1503", "6f6e15dfde06c825",   # Gateway-Hostnamen
    "b649db4adef29eb4",
    "052c5a686ddd1956",                                           # Tenant
    "3626f29f9ccedc1e", "c75f14c715e3cd9e", "69a7169ae1a5bf2e",   # öffentliche IPs
    "f858eadd0707e98a", "7c215983523326b2",
    "0677a3b941e29b9c", "c80f752f6b4b1fa3", "0cf5618b5fe5ed52",   # Tenant-/App-IDs
    "cc91e80c12f383ce", "353e062b0c90ca8d",
}

_KANDIDAT = re.compile(
    r"(?:[a-z0-9-]+\.)+[a-z]{2,}"          # Hostnamen
    r"|\b\d{1,3}(?:\.\d{1,3}){3}\b"        # IPv4
    r"|\b[0-9a-f]{8}(?=-[0-9a-f]{4}-)"     # GUID-Anfang
)


def _hash(wert: str) -> str:
    return hashlib.sha256(wert.encode()).hexdigest()[:16]


def test_keine_eigenen_kennungen():
    dateien = subprocess.run(["git", "ls-files"], cwd=WURZEL, capture_output=True,
                             text=True, check=True).stdout.splitlines()
    treffer = []
    for name in dateien:
        try:
            text = (WURZEL / name).read_text(encoding="utf-8").lower()
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for nr, zeile in enumerate(text.splitlines(), 1):
            for m in _KANDIDAT.finditer(zeile):
                if _hash(m.group(0)) in GESPERRT:
                    treffer.append(f"{name}:{nr}")
    assert not treffer, f"Kennung der eigenen Umgebung im Repository: {treffer}"


def test_suche_findet_gesperrte_werte():
    """Gegenprobe: das Muster erfasst jede Wertart, die gesperrt werden soll."""
    for beispiel in ("gateway.contoso.com", "203.0.113.10",
                     "00000000-0000-4000-8000-000000000001"):
        assert _KANDIDAT.search(beispiel), beispiel
