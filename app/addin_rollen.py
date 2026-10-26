"""Benutzerrollen für Outlook-Add-ins im Tenant prüfen (nur lesend).

Fehlen in der Rollenzuweisungsrichtlinie der Nutzer die Rollen *My Custom
Apps*, *My Marketplace Apps* und *My ReadWriteMailbox Apps*, laufen die
Add-ins nur bei Admins — eine Admin-Rolle hebt die Sperre auf (Microsoft Learn,
„Specify the administrators and users who can install and manage add-ins").
Normale Nutzer sehen „Der Zugriff auf Add-Ins wurde von Ihrer Organisation
eingeschränkt", und das Verfassen-Add-in fügt nichts ein. Gemessen 26.10.2026:
Standardrichtlinie leer, Verfassen-Add-in bei erika@ auf iOS nie gestartet;
nach dem Anhaken der drei Rollen sofort „geliefert (iOS)".

Beim Einrichten fällt das nicht auf, weil der Einrichtende Admin ist. Das
Gateway ändert hier nichts — die Richtlinie ist eine Entscheidung im Tenant.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

_SKRIPT = Path("/app/scripts/get_addin_rollen.ps1")

SOLL = ("My Custom Apps", "My Marketplace Apps", "My ReadWriteMailbox Apps")

OK = "ok"
FEHLT = "fehlt"
UNBEKANNT = "unbekannt"


def luecken(daten: dict) -> list[str]:
    """Was den Add-ins fehlt — rein, ohne Tenant. Leere Liste = in Ordnung."""
    fehlt = []
    if daten.get("apps_for_office") is False:
        fehlt.append("AppsForOfficeEnabled ist aus (sperrt Add-ins für alle)")
    vorhanden = {str(r).strip().lower() for r in (daten.get("rollen") or [])}
    fehlt += [r for r in SOLL if r.lower() not in vorhanden]
    return fehlt


def pruefen() -> dict:
    """{"zustand": ok|fehlt|unbekannt, "richtlinie", "fehlt": [...]}"""
    import waechter_regel
    data = waechter_regel.ps_regelskript(_SKRIPT, regeln="-")
    if not data or not data.get("ok"):
        log.info("Add-in-Rollen nicht lesbar: %s", (data or {}).get("error") or "unbekannt")
        return {"zustand": UNBEKANNT, "richtlinie": "", "fehlt": []}
    fehlt = luecken(data)
    return {"zustand": FEHLT if fehlt else OK,
            "richtlinie": str(data.get("richtlinie") or ""), "fehlt": fehlt}
