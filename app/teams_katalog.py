"""Teams-Apps im Katalog des Tenants: Fassung lesen, merken, als Aufgabe melden.

ANLASS (2026-11-17)
-------------------
Lag im Teams-Katalog eine ältere Fassung als das Paket des Gateways, sah das
nur, wer die Einrichtung bzw. die Notbetrieb-Seite öffnete — eine Aufgabe im
Sammelpunkt gab es nicht (anders als beim Verfassen-Add-in, `addin-fassung`).
Auf exohv stand die Notfall-App so unbemerkt auf einer älteren Fassung.

EINE Stelle für beide Apps (Self-Service „signatur“, Notfall-Mail „notfall“):
`stand()` liest live (Seitenaufbau, täglicher Takt im Scheduler) und MERKT das
Ergebnis; `pruefung()` liest nur das Gemerkte — sie läuft als Aufgabe bei jedem
Seitenaufbau und darf nicht ins Netz (aufgaben.py). Nach „Im Teams-Katalog
aktualisieren“ merkt `merken()` die neue Fassung sofort.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import settings_store

log = logging.getLogger(__name__)

MERK = "_TEAMS_KATALOG"          # {app: {"zustand", "version", "ts"}}
NAME = {"signatur": "Teams-App „Signatur & Abwesenheit“", "notfall": "Teams-App „Notfall-Mail“"}
STAND = {"signatur": "_TEAMS_APP_STAND", "notfall": "_TEAMS_NOTFALL_STAND"}   # Paketfassung (addin._manifest_version)
LINK = {"signatur": "/setup#teams-app-schalter", "notfall": "/notbetrieb#nb-teams-app"}


def aktiv(app: str) -> bool:
    """Wird diese App bereitgestellt? Self-Service: Schalter in der Einrichtung.
    Notfall-Mail: Notbetrieb eingeschaltet (die App ist optional — ob sie im
    Katalog steht, entscheidet `pruefung`)."""
    if app == "signatur":
        return bool(settings_store.get("ADDIN_ENABLED") and settings_store.get("TEAMS_APP_AKTIV"))
    import notbetrieb
    return notbetrieb.funktion_aktiv()


def _manifest(app: str, base: str) -> dict:
    from webui.routen.addin import _notfall_teams_manifest, _teams_manifest
    return (_teams_manifest if app == "signatur" else _notfall_teams_manifest)(base)


def _versionstupel(v) -> tuple[int, ...]:
    try:
        return tuple(int(t) for t in str(v).split("."))
    except ValueError:
        return (0,)


def merken(app: str, katalog: dict) -> None:
    """Zustand und Fassung im Katalog festhalten — geschrieben nur bei Änderung."""
    alt = settings_store.get(MERK) or {}
    eintrag = {"zustand": katalog.get("zustand") or "fehler", "version": katalog.get("version")}
    if {k: (alt.get(app) or {}).get(k) for k in eintrag} == eintrag:
        return
    settings_store.force_update({MERK: {**alt, app: {**eintrag, "ts": datetime.now(timezone.utc).isoformat()}}})
    import aufgaben
    aufgaben.vergessen()


async def stand(app: str, base: str) -> dict:
    """Paketfassung und — live gelesen — Fassung im Katalog; das Ergebnis wird
    gemerkt. Für Einrichtung und Notbetrieb-Seite (EINE Rechnung von `aktuell`)."""
    import graph_client
    m = _manifest(app, base)
    katalog = await graph_client.teams_katalog(m["id"])
    v = katalog.get("version")
    katalog["aktuell"] = bool(v) and _versionstupel(v) >= _versionstupel(m["version"])
    merken(app, katalog)
    return {"version": m["version"], "seit": (settings_store.get(STAND[app]) or {}).get("seit", ""),
            "katalog": katalog}


async def auffrischen() -> None:
    """Täglicher Takt (scheduler): beide Apps lesen, damit die Aufgabe auch
    ohne Seitenaufruf entsteht."""
    import aussenadresse
    base = aussenadresse.basis()
    if not base:
        return
    for app in NAME:
        if aktiv(app):
            try:
                await stand(app, base)
            except Exception as exc:                          # noqa: BLE001
                log.warning("Teams-Katalog (%s) nicht gelesen: %s", app, exc)


def pruefung(app: str) -> dict:
    """Abnahme-Punkt aus dem GEMERKTEN Stand — ohne Netz."""
    import abnahme
    name = f"{NAME[app]} im Teams-Katalog aktuell"
    if not aktiv(app):
        return abnahme._punkt(name, abnahme.ENTFAELLT, "Nicht bereitgestellt.")
    k = (settings_store.get(MERK) or {}).get(app) or {}
    paket = (settings_store.get(STAND[app]) or {}).get("version") or ""
    zustand, version = k.get("zustand"), k.get("version")
    if not k:
        return abnahme._punkt(name, abnahme.UNBEKANNT, "Fassung im Teams-Katalog noch nicht gelesen.",
                              "Seite öffnen — sie liest den Katalog; sonst folgt der tägliche Abgleich.")
    if zustand == "recht_fehlt":
        return abnahme._punkt(name, abnahme.UNBEKANNT, "Fassung im Teams-Katalog nicht lesbar: "
                              "Der Gateway-App fehlt AppCatalog.Read.All.",
                              "Einrichtung → Berechtigungen erteilen (AppCatalog.Read.All).")
    if zustand == "nicht_gefunden":
        if app == "notfall":
            return abnahme._punkt(name, abnahme.ENTFAELLT, "Nicht im Teams-Katalog (optional, "
                                  "Alternative zur Edge-App).")
        return abnahme._punkt(name, abnahme.OFFEN, "Bereitgestellt, aber nicht im Teams-Katalog.",
                              "Paket herunterladen und im Teams Admin Center hochladen.")
    if zustand != "ok" or not paket:
        return abnahme._punkt(name, abnahme.UNBEKANNT, f"Fassung im Teams-Katalog unklar ({zustand}).")
    if _versionstupel(version) >= _versionstupel(paket):
        return abnahme._punkt(name, abnahme.OK, f"Im Katalog Fassung {version}, aktuell.")
    return abnahme._punkt(name, abnahme.OFFEN, f"Im Teams-Katalog steht Fassung {version}, "
                          f"das Paket des Gateways hat {paket}.",
                          "„Im Teams-Katalog aktualisieren“ (Konto mit Rolle Teams-Administrator).")
