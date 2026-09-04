"""Bypass-Wächter Phase 1: Aufteilung der einen Route-Regel in zwei Wege.

Zweck (Vertraulichkeit): Fällt das Gateway aus, darf der Wächter die
**Signatur**-Route abschalten → Post reiner Signatur-Postfächer fließt unsigniert
weiter (kein Geheimnisbruch). Post aus **S/MIME**-Postfächern MUSS in der Queue
warten — sie könnte verschlüsselt gehören, und die darf NIE unverschlüsselt raus.

Warum der Split nach dem Postfach-`smime`-Flag die Vorgabe erfüllt: Verschlüsseln
ist zwar eine Pro-Nachricht-Entscheidung (`#enc`, Auto-Regeln, Antwort auf
Verschlüsseltes), aber `handler.py` erzwingt `wants_encryption = False`, sobald
das Postfach kein S/MIME hat (`_smime_ok`, Z. ~1008/986). Ein Postfach OHNE
`smime` kann also nie verschlüsseln → es ist bypass-sicher.

INERT: dieses Modul definiert nur Namen + Partitionierung. Es legt KEINE Regeln
an und ruft kein EXO. Der Live-Split (zweite DG + zweite Regel) kommt in einem
eigenen, opt-in-gesteuerten Schritt mit Live-Test.

Namensschema (least-disruption): der bestehende Weg bleibt der Signatur-Weg —
Regel `Route via <GatewayName>` + DG `… - Enabled Mailboxes`. Neu kommt nur der
S/MIME-Weg dazu. So bleibt eine nicht aufgeteilte Installation unberührt.
"""
from __future__ import annotations

import settings_store
import waechter_regel


def split_aktiv() -> bool:
    """Opt-in: ist die Regeltrennung (Phase 1) aktiviert? Vorgabe aus (False)."""
    return settings_store.get("WATCHDOG_RULE_SPLIT") is True


def _gw() -> str:
    return settings_store.get("GATEWAY_NAME") or "EXO Signature Gateway"


def signatur_regelname() -> str:
    """Bypass-fähige Signatur-Regel = der bestehende Weg (identisch zu
    waechter_regel.regelname(), damit die Wächter-Statusabfrage konsistent bleibt)."""
    return waechter_regel.regelname()


def smime_regelname() -> str:
    """Die S/MIME-Regel, die der Wächter NIE abschaltet."""
    r = (settings_store.get("EXO_RULE_SMIME") or "").strip()
    return r or f"Route via {_gw()} (S/MIME)"


def signatur_dg_name() -> str:
    """DG des Signatur-Wegs — die bestehende Liste wird wiederverwendet."""
    return f"{_gw()} - Enabled Mailboxes"


def smime_dg_name() -> str:
    """DG des S/MIME-Wegs (neu bei aktivem Split)."""
    return f"{_gw()} - SMIME Mailboxes"


def _adresse(entry: dict) -> str:
    return (entry.get("primary") or entry.get("email") or "").strip().lower()


def partitioniere(mailbox_config: dict | None) -> dict:
    """Teilt die AKTIVEN Postfächer nach Verschlüsselungs-Fähigkeit auf.

    Rückgabe: {"signatur": [addr, …], "smime": [addr, …]} (primäre Adressen,
    sortiert, dedupliziert).

    Kriterium (spiegelt die Verschlüsselungs-Gate in handler.py):
      - `smime` gesetzt  → S/MIME-Weg (Warte-Regel; kann verschlüsseln).
      - sonst nur `sig`  → Signatur-Weg (bypass-fähig; kann NIE verschlüsseln).
      - weder noch       → inaktiv, in keiner Liste.
    """
    signatur: list[str] = []
    smime: list[str] = []
    for entry in (mailbox_config or {}).values():
        if not isinstance(entry, dict):
            continue
        addr = _adresse(entry)
        if not addr:
            continue
        if entry.get("smime"):
            smime.append(addr)
        elif entry.get("sig"):
            signatur.append(addr)
        # weder sig noch smime → nicht aktiv, überspringen
    return {"signatur": sorted(set(signatur)), "smime": sorted(set(smime))}
