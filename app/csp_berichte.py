"""Berichte über Verstöße gegen die geplante Content-Security-Policy.

ANLASS (v1.11.7)
----------------
Die CSP steht seit Langem nur berichtend im Kopf (`Content-Security-Policy-
Report-Only`, webui/app.py) — aber ohne `report-uri`. Kein Browser hat je
einen Verstoß gemeldet, und „die Berichte sind leer" war damit keine Aussage:
Es gab keine. Bevor die CSP scharf geschaltet werden kann, muss sichtbar sein,
was sie brechen würde (Inline-Skripte, fremde Quellen wie Office.js).

Gesammelt wird je Art (Direktive, blockierte Quelle, Seite) mit Zähler und
erstem/letztem Zeitpunkt, begrenzt und auf Platte, damit eine Beobachtung über
Tage einen Neustart übersteht. Der Endpunkt ist ohne Anmeldung erreichbar
(Browser senden die Berichte ohne Sitzung) — daher die harten Grenzen: höchstens
`MAX_ARTEN` Einträge, kurze Felder, Schreiben auf Platte höchstens alle
`SCHREIB_ABSTAND_S` Sekunden.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import config

log = logging.getLogger(__name__)

MAX_ARTEN = 100
SCHREIB_ABSTAND_S = 60
_lock = threading.Lock()
_arten: dict[str, dict] | None = None
_geschrieben = 0.0


def _datei() -> Path:
    return Path(config.DATA_DIR) / "csp_berichte.json"


def _laden() -> dict[str, dict]:
    global _arten
    if _arten is None:
        try:
            _arten = json.loads(_datei().read_text(encoding="utf-8")).get("arten", {})
        except (OSError, ValueError):
            _arten = {}
    return _arten


def _kurz(wert, n=120) -> str:
    return str(wert or "")[:n]


def _quelle(uri: str) -> str:
    """Nur Schema+Host einer blockierten Adresse — Pfade können Tokens tragen."""
    uri = _kurz(uri, 500)
    if uri in ("inline", "eval", "data", "blob", "self", ""):
        return uri or "?"
    p = urlparse(uri)
    return f"{p.scheme}://{p.hostname}" if p.hostname else _kurz(uri, 40)


def merken(bericht: dict) -> None:
    """Einen Bericht (Rumpf von `application/csp-report`) aufnehmen."""
    global _geschrieben
    r = bericht.get("csp-report") if isinstance(bericht, dict) else None
    if not isinstance(r, dict):
        return
    direktive = _kurz(r.get("effective-directive") or r.get("violated-directive"), 60)
    quelle = _quelle(r.get("blocked-uri") or "")
    seite = _kurz(urlparse(_kurz(r.get("document-uri"), 500)).path, 80)
    schluessel = f"{direktive}|{quelle}|{seite}"
    jetzt = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _lock:
        arten = _laden()
        e = arten.get(schluessel)
        if e is None:
            if len(arten) >= MAX_ARTEN:
                return
            e = arten[schluessel] = {"direktive": direktive, "quelle": quelle,
                                     "seite": seite, "anzahl": 0, "erstmals": jetzt}
            log.info("CSP-Verstoß (neu): %s %s auf %s", direktive, quelle, seite)
        e["anzahl"] += 1
        e["zuletzt"] = jetzt
        if time.monotonic() - _geschrieben >= SCHREIB_ABSTAND_S:
            _schreiben(arten)
            _geschrieben = time.monotonic()


def _schreiben(arten: dict) -> None:
    ziel = _datei()
    try:
        ziel.parent.mkdir(parents=True, exist_ok=True)
        tmp = ziel.with_suffix(".tmp")
        tmp.write_text(json.dumps({"arten": arten}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(ziel)
    except OSError as exc:
        log.warning("CSP-Berichte nicht gespeichert: %s", exc)


def liste() -> list[dict]:
    with _lock:
        return sorted(_laden().values(), key=lambda e: -e["anzahl"])
