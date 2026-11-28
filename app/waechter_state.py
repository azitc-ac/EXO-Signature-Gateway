"""Gemeinsamer Zustand des Bypass-Wächters (data/watchdog_state.json).

EINE Quelle für: den Heartbeat (Web-Route, minütlich), die unabhängige
EXO-Regelprüfung (Scheduler, täglich) und das Banner (deps). Lesen/Schreiben
unter einem Lock, damit die häufigen Heartbeat-Schreibvorgänge und die
Regelprüfung aus verschiedenen Threads sich nicht gegenseitig überschreiben.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import config

PFAD = Path(config.DATA_DIR) / "watchdog_state.json"
_LOCK = threading.RLock()


def lesen() -> dict:
    with _LOCK:
        try:
            return json.loads(PFAD.read_text("utf-8"))
        except Exception:                                  # noqa: BLE001
            return {}


# Herzschlag-Felder aus der Zeit vor dem Mehrwächter-Register. Seitdem stehen sie
# je Wächter im Register (waechter_register._HB_FELDER); hier schreibt sie
# niemand mehr. Gelesen wurden sie trotzdem weiter als Rückfall — ein alter
# Fehlertext oder ein alter „Bypass aktiv“ wäre so für immer im Band gestanden.
# Nach der Übernahme ins Register werden sie entfernt.
HERZSCHLAG_ALT = ("last_seen", "healthy", "bypass_active", "fails", "oks", "exo_error")


def altfelder_entfernen() -> bool:
    """Die Herzschlag-Altfelder entfernen. True, wenn etwas entfernt wurde."""
    with _LOCK:
        d = lesen()
        if not any(k in d for k in HERZSCHLAG_ALT):
            return False
        for k in HERZSCHLAG_ALT:
            d.pop(k, None)
        tmp = PFAD.with_suffix(".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(PFAD)
        return True


def merge(**felder) -> dict:
    """Felder in den Zustand einmischen und atomar (600) zurückschreiben."""
    with _LOCK:
        d = lesen()
        d.update(felder)
        tmp = PFAD.with_suffix(".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(PFAD)
        return d
