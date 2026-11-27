"""Belegung des Datenvolumens — EINE Quelle für die Kachel der Übersicht und
die Warnmail an die Verwaltung.

Bis v1.12.179 färbte nur die Kachel die Zahl (Schwellen 75/90 als Zahlen im
JavaScript der Übersicht); eine aktive Benachrichtigung gab es nicht. Wer die
Übersicht nicht öffnete, bemerkte ein volles Volumen erst, wenn Warteschlange,
Protokoll oder Notbetrieb-Kopie nicht mehr schreiben konnten.

Schwellen bewusst als feste Konstanten, nicht als Einstellung: Eine Einstellung
bräuchte UI-Punkt, Register und Prüfung (Prinzip 6) — für eine Sicherheitsgrenze,
die niemand verstellen muss, wäre das ein Knopf mehr, der falsch stehen kann.

Gemessen wird `DATA_DIR`. Dort liegen auch die Notbetrieb-Kopie
(data/notbetrieb) und die MTA-Daten (data/mta). Die Postfix-Warteschlange liegt
in einem eigenen Docker-Volume (mta-spool) unter /var/lib/docker, das im
Gateway-Container nicht eingehängt ist; auf der Standardinstallation ist das
dasselbe Dateisystem (gemessen auf der Produktiv-VM 27.11.2026: /dev/sda1 für
beide) — ANNAHME für andere Installationen.

Mail: höchstens einmal pro Tag (Ortszeit), solange die Belegung ≥ KRIT_PCT ist;
Entwarnung einmal, wenn sie unter ENTWARNUNG_PCT fällt (Hysterese — zwischen 85
und 90 % kein Hin und Her). Zustand in data/plattenplatz_warnung.json.
"""
from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

import config

log = logging.getLogger(__name__)

WARN_PCT = 75          # Kachel orange
KRIT_PCT = 90          # Kachel rot + Warnmail
ENTWARNUNG_PCT = 85    # Entwarnung erst darunter


def _pfad() -> Path:
    return Path(config.DATA_DIR)


def messen(pfad: Path | None = None) -> dict | None:
    """{total_mb, used_mb, free_mb, pct, pfad} oder None, wenn nicht messbar."""
    pfad = pfad or _pfad()
    try:
        du = shutil.disk_usage(str(pfad))
    except OSError as exc:
        log.warning("plattenplatz: %s nicht messbar: %s", pfad, exc)
        return None
    mb = 1024 * 1024
    return {"total_mb": round(du.total / mb, 1), "used_mb": round(du.used / mb, 1),
            "free_mb": round(du.free / mb, 1),
            "pct": round(du.used / du.total * 100, 1) if du.total else 0.0, "pfad": str(pfad)}


def _zustand_pfad() -> Path:
    return _pfad() / "plattenplatz_warnung.json"


def _zustand() -> dict:
    try:
        return json.loads(_zustand_pfad().read_text())
    except (OSError, ValueError):
        return {}


def _zustand_schreiben(daten: dict) -> None:
    try:
        tmp = _zustand_pfad().with_suffix(".tmp")
        tmp.write_text(json.dumps(daten))
        tmp.chmod(0o600)
        tmp.replace(_zustand_pfad())
    except OSError as exc:
        log.warning("plattenplatz: Zustand nicht gespeichert: %s", exc)


def _heute() -> str:
    import settings_store
    return datetime.now(settings_store.anzeige_zone()).strftime("%Y-%m-%d")


def pruefen(messung: dict | None = None, heute: str | None = None) -> str:
    """Messen und ggf. warnen/entwarnen. Rückgabe: "warnung", "entwarnung" oder ""
    (nichts gesendet). Gemerkt wird nur, was tatsächlich verschickt wurde — eine
    gescheiterte Mail wird beim nächsten Lauf erneut versucht."""
    import notification
    m = messung if messung is not None else messen()
    if not m:
        return ""
    heute = heute or _heute()
    z = _zustand()
    if m["pct"] >= KRIT_PCT:
        if z.get("gemeldet_am") == heute:
            return ""
        if notification.send_plattenplatz_warnung(m):
            _zustand_schreiben({"aktiv": True, "gemeldet_am": heute, "pct": m["pct"]})
            log.warning("plattenplatz: %s zu %.1f %% belegt — Verwaltung benachrichtigt", m["pfad"], m["pct"])
            return "warnung"
        return ""
    if z.get("aktiv") and m["pct"] < ENTWARNUNG_PCT:
        if notification.send_plattenplatz_entwarnung(m):
            _zustand_schreiben({"aktiv": False, "entwarnt_am": heute, "pct": m["pct"]})
            log.info("plattenplatz: %s wieder bei %.1f %% — Entwarnung verschickt", m["pfad"], m["pct"])
            return "entwarnung"
    return ""
