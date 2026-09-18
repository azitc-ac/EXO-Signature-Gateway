"""Banner-Kampagnen: ein Banner-Template zeitfenster- und gruppengesteuert zeigen.

Eine Kampagne sagt: „zeige Banner *T* von *Start* bis *Ende*, optional nur der
Gruppe *G*". Sie sitzt VOR dem bestehenden Banner (Richtlinie/Postfach): Ist zum
Sendezeitpunkt eine passende Kampagne aktiv, gewinnt ihr Banner. Ohne aktive
Kampagne bleibt alles wie zuvor.

KEIN Mailfluss-Risiko: rein additiv im Signatur-/Banner-Weg (handler.py), reine
Datumsprüfung — kein Loop-/Header-/Auto-Submitted-Thema.

Zeitkonvention wie im übrigen Gateway: UTC-ISO8601 (`datetime.now(timezone.utc)`,
`fromisoformat`), „Z"-Suffix wird akzeptiert. Leere Grenze = offen (Start leer:
läuft schon; Ende leer: läuft weiter). Leere Gruppe = alle.

Reine Logik (`aktive_kampagne`) und Verwaltung (`liste/speichern/loeschen`) sind
getrennt: Erstere läuft im Mailpfad (auch im Subprozess denkbar) und schreibt
NIE; Letztere nutzt `settings_store.update` und gehört in den Web-Prozess.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone

import settings_store

KEY = "BANNER_CAMPAIGNS"


# ── Zeit ──────────────────────────────────────────────────────────────────────
def parse_zeit(roh) -> datetime | None:
    """ISO8601 → aware UTC-datetime, oder None bei leer/unlesbar. Akzeptiert „Z"."""
    s = (roh or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Naiv angegebene Zeit als UTC deuten (die Oberfläche liefert immer mit Zone).
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _aktiv(camp: dict, now: datetime) -> bool:
    if not camp.get("enabled", True):
        return False
    start = parse_zeit(camp.get("start"))
    ende = parse_zeit(camp.get("end"))
    if start and now < start:
        return False
    if ende and now > ende:
        return False
    return True


def _passt_gruppe(camp: dict, sender_key: str | None, internal_groups: dict) -> bool:
    gruppe = (camp.get("group") or "").strip()
    if not gruppe:
        return True                      # leere Gruppe = alle
    if not sender_key:
        return False
    return sender_key in (internal_groups.get(gruppe) or [])


def aktive_kampagne(campaigns, now: datetime, sender_key: str | None,
                    internal_groups: dict) -> str | None:
    """Banner-Template der ERSTEN aktiven, passenden Kampagne — sonst None.

    Reihenfolge = Listenreihenfolge (erste gewinnt). Rein; kein Seiteneffekt.
    """
    for camp in campaigns or []:
        if not isinstance(camp, dict):
            continue
        banner = (camp.get("banner") or "").strip()
        if not banner:
            continue
        if _aktiv(camp, now) and _passt_gruppe(camp, sender_key, internal_groups):
            return banner
    return None


# ── Verwaltung (nur Web-Prozess) ──────────────────────────────────────────────
_ID_RE = re.compile(r"^kmp_[a-z0-9]{6,}$")


def liste() -> list[dict]:
    out = []
    for c in settings_store.get(KEY) or []:
        if isinstance(c, dict):
            out.append(c)
    return out


def _validiere(name: str, banner: str, start: str, end: str) -> tuple[str, str]:
    name = (name or "").strip()
    banner = (banner or "").strip()
    if not name:
        raise ValueError("Name fehlt.")
    if not banner:
        raise ValueError("Banner-Vorlage fehlt.")
    s = parse_zeit(start)
    e = parse_zeit(end)
    if start and s is None:
        raise ValueError("Startzeit ist unlesbar (erwartet ISO8601).")
    if end and e is None:
        raise ValueError("Endzeit ist unlesbar (erwartet ISO8601).")
    if s and e and e < s:
        raise ValueError("Ende liegt vor Start.")
    return name, banner


def speichern(daten: dict) -> dict:
    """Kampagne anlegen (ohne gültige id) oder ändern (mit id). Gibt den
    gespeicherten Datensatz zurück. NUR aus dem Web-Prozess (settings_store)."""
    name, banner = _validiere(daten.get("name", ""), daten.get("banner", ""),
                              daten.get("start", ""), daten.get("end", ""))
    rec = {
        "id": (daten.get("id") or "").strip(),
        "name": name,
        "banner": banner,
        "start": (daten.get("start") or "").strip(),
        "end": (daten.get("end") or "").strip(),
        "group": (daten.get("group") or "").strip(),
        "enabled": bool(daten.get("enabled", True)),
    }
    campaigns = liste()
    if rec["id"] and _ID_RE.match(rec["id"]):
        ersetzt = False
        for i, c in enumerate(campaigns):
            if c.get("id") == rec["id"]:
                campaigns[i] = rec
                ersetzt = True
                break
        if not ersetzt:
            campaigns.append(rec)
    else:
        rec["id"] = "kmp_" + secrets.token_hex(4)
        campaigns.append(rec)
    settings_store.update({KEY: campaigns})
    return rec


def loeschen(camp_id: str) -> bool:
    camp_id = (camp_id or "").strip()
    campaigns = liste()
    rest = [c for c in campaigns if c.get("id") != camp_id]
    if len(rest) == len(campaigns):
        return False
    settings_store.update({KEY: rest})
    return True


def status(camp: dict, now: datetime | None = None) -> str:
    """Für die Oberfläche: 'aus' | 'aktiv' | 'geplant' | 'abgelaufen'."""
    if not camp.get("enabled", True):
        return "aus"
    now = now or datetime.now(timezone.utc)
    start = parse_zeit(camp.get("start"))
    ende = parse_zeit(camp.get("end"))
    if start and now < start:
        return "geplant"
    if ende and now > ende:
        return "abgelaufen"
    return "aktiv"
