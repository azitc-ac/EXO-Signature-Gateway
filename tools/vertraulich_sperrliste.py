#!/usr/bin/env python3
"""Erzeugt die Sperrliste vertraulicher Begriffe für die CI — ohne Klartext.

WOFÜR
-----
Kundennamen, Ansprechpartner, Vorlagennamen, Domänen und Netze eines Kunden
dürfen nicht ins öffentliche Repository (CLAUDE.md, „Kundendaten — ULTRA
wichtig“). Der commit-msg-Hook prüft das lokal gegen
~/.config/exo-gateway/vertraulich.txt. Er lässt sich aber umgehen (--no-verify,
Bearbeitung im Browser, Rechner ohne Liste) — die CI prüft deshalb zusätzlich
(tests/test_keine_kundendaten.py).

WARUM MIT SCHLÜSSEL
-------------------
Eine Liste einfacher SHA-256-Werte (wie in test_keine_eigenen_kennungen.py)
genügt hier nicht: Die Begriffe sind kurze Namen und Domänen. Wer einen
Verdacht hat („ist X Kunde?“), rechnet den Hash nach und hat die Bestätigung —
die Liste würde genau das veröffentlichen, was sie schützt. Deshalb ein
geschlüsselter Hash (BLAKE2b mit 32-Byte-Schlüssel, wirkt wie HMAC): Ohne den
Schlüssel lässt sich nichts nachrechnen. Der Schlüssel liegt lokal neben der
Liste und in der CI als Secret VERTRAULICH_SCHLUESSEL.
Dazu kommen Zufallswerte, damit die Länge der Liste nichts über die Zahl der
Begriffe verrät.

NORMALFORM
----------
Kleinbuchstaben, zerlegt in Wörter aus [0-9a-zäöüß]; ein Begriff ist die Folge
seiner Wörter, mit einem Leerzeichen verbunden. „Firma.at“ und „firma at“ sind
also gleich. Der Text im Repository wird genauso zerlegt und in Folgen gleicher
Wortzahl verglichen. Teilwörter („Firmengruppe“ enthält „Firma“) findet die CI
anders als der Hook NICHT — der Hook bleibt die erste Linie.

AUFRUF
------
    python3 tools/vertraulich_sperrliste.py            # Liste neu schreiben
    python3 tools/vertraulich_sperrliste.py --secret   # Schlüssel als CI-Secret setzen (gh)

Nach jedem neuen Begriff in vertraulich.txt: Liste neu schreiben und committen.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
LISTE = Path(os.environ.get("EXO_VERTRAULICH") or Path.home() / ".config/exo-gateway/vertraulich.txt")
SCHLUESSEL = LISTE.with_name("vertraulich.schluessel")
AUSGABE = WURZEL / "tests" / "vertraulich_sperrliste.json"
FUELLER = 32                                # Zufallswerte, damit die Anzahl nichts verrät

_WORT = re.compile(r"[0-9a-zäöüß]+")


def woerter(text: str) -> list[str]:
    return _WORT.findall(text.lower())


def schluessel_hex(text: str) -> bytes:
    k = bytes.fromhex(text.strip())
    if len(k) != 32:
        raise ValueError("Schlüssel muss 32 Byte (64 Hex-Zeichen) lang sein")
    return k


def kennung(schluessel: bytes, folge: str) -> str:
    return hashlib.blake2b(folge.encode(), key=schluessel, digest_size=16).hexdigest()


def begriffe(pfad: Path) -> list[str]:
    out = []
    for z in pfad.read_text(encoding="utf-8").splitlines():
        z = z.strip()
        if not z or z.startswith("#"):
            continue
        w = woerter(z)
        if w:
            out.append(" ".join(w))
    return out


def _schluessel_laden_oder_anlegen() -> bytes:
    if not SCHLUESSEL.exists():
        tmp = SCHLUESSEL.with_suffix(".tmp")
        tmp.write_text(secrets.token_hex(32) + "\n")
        tmp.chmod(0o600)
        tmp.replace(SCHLUESSEL)
        print(f"Neuer Schlüssel angelegt: {SCHLUESSEL} (600). In die CI: --secret")
    return schluessel_hex(SCHLUESSEL.read_text())


def schreiben() -> int:
    if not LISTE.is_file():
        print(f"Keine Liste: {LISTE}", file=sys.stderr)
        return 1
    k = _schluessel_laden_oder_anlegen()
    bs = begriffe(LISTE)
    echte = {kennung(k, b) for b in bs}
    fueller = {secrets.token_hex(16) for _ in range(FUELLER)}
    daten = {
        "verfahren": "blake2b-128 mit Schlüssel (CI-Secret VERTRAULICH_SCHLUESSEL), "
                     "erzeugt von tools/vertraulich_sperrliste.py",
        "wortzahlen": sorted({len(b.split()) for b in bs}),
        "kennungen": sorted(echte | fueller),
    }
    tmp = AUSGABE.with_suffix(".tmp")
    tmp.write_text(json.dumps(daten, indent=1) + "\n", encoding="utf-8")
    tmp.replace(AUSGABE)
    print(f"Geschrieben: {AUSGABE.relative_to(WURZEL)} ({len(daten['kennungen'])} Kennungen)")
    return 0


def secret_setzen() -> int:
    k = _schluessel_laden_oder_anlegen()
    # Über stdin, damit der Schlüssel weder in der Prozessliste noch im Log steht.
    r = subprocess.run(["gh", "secret", "set", "VERTRAULICH_SCHLUESSEL"], input=k.hex(),
                       text=True, cwd=WURZEL)
    return r.returncode


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--secret", action="store_true", help="Schlüssel als CI-Secret setzen (gh)")
    a = ap.parse_args()
    sys.exit(secret_setzen() if a.secret else schreiben())
