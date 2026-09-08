"""Register der Bypass-Wächter (data/watchdog_watchers.json).

Von „ein Wächter" zu „eine Flotte": mehrere Wächter (Azure Function und/oder cron
auf einem Zweithost) überwachen dasselbe Gateway. Jeder hat eine eigene ID, ein
eigenes Heartbeat-Token und einen eigenen Zustand — das Entfernen des einen stört
den anderen nicht, und beide melden sich unabhängig. Für Azure-Wächter liegen
zusätzlich die Angaben zum Rückbau bei (`azure`), damit das Gateway später GENAU
das löschen kann, was es angelegt hat.

Abgrenzung: Das GLOBALE Faktum „ist die Signatur-Regel gerade abgeschaltet"
(`rule_state`) ist KEIN Wächter-Attribut — es steht weiter in `waechter_state`
(der Scheduler schreibt es). Hier stehen die WÄCHTER.

Rechte: 600, atomar geschrieben (das Token-Hash ist ein Geheimnis).
"""
from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

import config

PFAD = Path(config.DATA_DIR) / "watchdog_watchers.json"
_LOCK = threading.RLock()

# Felder, die ein Wächter je Heartbeat meldet (der Rest ist Stammdaten).
_HB_FELDER = ("last_seen", "healthy", "bypass_active", "fails", "oks", "exo_error")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _laden() -> dict:
    with _LOCK:
        try:
            d = json.loads(PFAD.read_text("utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:                                      # noqa: BLE001
            return {}


def _speichern(d: dict) -> None:
    with _LOCK:
        tmp = PFAD.with_suffix(".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        tmp.chmod(0o600)                                       # enthält Token-Hashes
        tmp.replace(PFAD)


def neue_id() -> str:
    return "wd_" + secrets.token_hex(5)


def registrieren(*, id: str, name: str, kind: str, token_hash: str,
                 azure: dict | None = None) -> dict:
    """Neuen Wächter anlegen. `azure` trägt die Rückbau-Metadaten (nur azure)."""
    with _LOCK:
        d = _laden()
        d[id] = {
            "id": id, "name": name, "kind": kind, "token_hash": token_hash,
            "created_at": _now(), "azure": azure or {},
            "last_seen": "", "healthy": False, "bypass_active": False,
            "fails": 0, "oks": 0, "exo_error": "",
        }
        _speichern(d)
        return _ohne_geheimnis(d[id])


def entfernen(id: str) -> dict | None:
    """Wächter aus dem Register nehmen. Gibt den (vollen) Eintrag zurück — der
    Aufrufer braucht die azure-Metadaten fürs Löschen — oder None."""
    with _LOCK:
        d = _laden()
        eintrag = d.pop(id, None)
        if eintrag is not None:
            _speichern(d)
        return eintrag


def holen(id: str) -> dict | None:
    """Voller Eintrag inkl. token_hash/azure (für interne Nutzung)."""
    return _laden().get(id)


def umbenennen(id: str, name: str) -> bool:
    """Anzeigename eines Wächters setzen. False, wenn unbekannt."""
    name = (name or "").strip()[:80]
    if not name:
        return False
    with _LOCK:
        d = _laden()
        if id not in d:
            return False
        d[id]["name"] = name
        _speichern(d)
        return True


def merke_azure(id: str, **felder) -> bool:
    """Azure-Metadaten eines Wächters nachtragen (z.B. app_id nach der Auflösung,
    grants_done nach der Rollenzuweisung) — damit Objekt-ID UND AppId je Wächter
    fest hinterlegt sind statt in freien Feldern. False, wenn unbekannt."""
    with _LOCK:
        d = _laden()
        if id not in d:
            return False
        az = dict(d[id].get("azure") or {})
        az.update(felder)
        d[id]["azure"] = az
        _speichern(d)
        return True


def _ohne_geheimnis(e: dict) -> dict:
    return {k: v for k, v in e.items() if k != "token_hash"}


def liste() -> list[dict]:
    """Alle Wächter OHNE token_hash, für die Oberfläche. Nach Anlage sortiert."""
    eintraege = sorted(_laden().values(), key=lambda e: e.get("created_at", ""))
    return [_ohne_geheimnis(e) for e in eintraege]


def anzahl() -> int:
    return len(_laden())


def heartbeat_aktualisieren(id: str, felder: dict) -> bool:
    """Heartbeat-Felder eines Wächters fortschreiben. False, wenn unbekannt."""
    with _LOCK:
        d = _laden()
        if id not in d:
            return False
        for f in _HB_FELDER:
            if f in felder:
                d[id][f] = felder[f]
        _speichern(d)
        return True


def zuordnen(token: str, id: str | None, verify) -> str | None:
    """Wächter-ID zu einem Heartbeat finden. `verify(token, hash) -> bool`.

    Mit `id` (neue Wächter senden X-Watchdog-Id): direkte, billige Prüfung.
    Ohne `id` (Legacy-Wächter aus der Einzel-Ära): Token gegen alle Hashes prüfen
    — bei einer Handvoll Wächter unproblematisch.
    """
    if not token:
        return None
    d = _laden()
    if id:
        e = d.get(id)
        return id if e and verify(token, e.get("token_hash", "")) else None
    for wid, e in d.items():
        if verify(token, e.get("token_hash", "")):
            return wid
    return None


def irgendein_bypass_aktiv() -> bool:
    """Meldet mindestens ein Wächter gerade einen aktiven Bypass? (Fürs Banner.)"""
    return any(e.get("bypass_active") for e in _laden().values())


def migrieren_falls_noetig() -> bool:
    """Bestehenden Einzel-Wächter (aus der Zeit vor dem Register) übernehmen —
    OHNE ihn zu stören: sein Token bleibt gültig (er sendet keine Id, der
    Heartbeat findet ihn per Token-Scan). Läuft einmalig beim Start.

    Quelle: WATCHDOG_TOKEN_HASH/WATCHDOG_KIND (settings) + der letzte Zustand aus
    waechter_state. Gibt True zurück, wenn migriert wurde.
    """
    import settings_store
    with _LOCK:
        if _laden():
            return False                                       # schon Register da
        token_hash = settings_store.get("WATCHDOG_TOKEN_HASH") or ""
        if not token_hash:
            return False                                       # kein Alt-Wächter
        kind = settings_store.get("WATCHDOG_KIND") or "azure"
        try:
            import waechter_state
            st = waechter_state.lesen()
        except Exception:                                      # noqa: BLE001
            st = {}
        wid = "wd_legacy"
        d = {wid: {
            "id": wid, "name": "Wächter 1", "kind": kind, "token_hash": token_hash,
            "created_at": _now(), "azure": {},
            "last_seen": st.get("last_seen", ""), "healthy": bool(st.get("healthy")),
            "bypass_active": bool(st.get("bypass_active")),
            "fails": int(st.get("fails") or 0), "oks": int(st.get("oks") or 0),
            "exo_error": st.get("exo_error", ""),
        }}
        _speichern(d)
        return True
