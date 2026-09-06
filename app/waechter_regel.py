"""Unabhängige, NUR LESENDE Prüfung des Signatur-Transportregel-Zustands.

Damit ein aktiver Bypass (Signatur-Regel ist in EXO `Disabled`) auch dann
auffällt, wenn der externe Wächter selbst tot ist. Läuft im Scheduler,
best-effort, read-only — kann den Mailfluss nicht anfassen. Der Zustand landet
in `data/watchdog_state.json` (`rule_state`) und speist das Banner.

Der abzufragende Regelname kommt aus `regelname()` — der EINEN Quelle für alle
Skripte (Vorrang `EXO_RULE_SIG`, sonst `Route via <GatewayName> (Signatur)`).
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import config
import settings_store
import waechter_state

log = logging.getLogger("waechter")

_SCRIPT = Path("/app/scripts/get_transport_rule_state.ps1")
_AUTH_CERT = Path(config.DATA_DIR) / "auth.pfx"


def basis_regelname() -> str:
    """Der ungetrennte Name des Signatur-Wegs — wie ihn das Connector-Setup anlegt.
    Vorrang hat ein ausdrücklich gesetztes EXO_RULE_SIG (der Name kann je System
    völlig anders lauten); sonst aus GATEWAY_NAME abgeleitet. Dient als
    Migrationsquelle beim Umbenennen auf den „(Signatur)"-Namen."""
    r = (settings_store.get("EXO_RULE_SIG") or "").strip()
    if r:
        return r
    gw = settings_store.get("GATEWAY_NAME") or "EXO Signature Gateway"
    return f"Route via {gw}"


def regelname() -> str:
    """Name der Signatur-Transportregel — die EINE Quelle für alle Skripte.

    Nichts wird hart kodiert; der Name variiert je System:
      1. ausdrücklich gesetztes EXO_RULE_SIG hat immer Vorrang (kann je System
         völlig anders lauten),
      2. sonst aus GATEWAY_NAME abgeleitet, mit dem Zusatz „(Signatur)"
         (symmetrisch zur „(S/MIME)"-Regel). So legt der Setup-Assistent sie an;
         bestehende Installationen werden von `basis_regelname()` einmalig
         hierher umbenannt."""
    r = (settings_store.get("EXO_RULE_SIG") or "").strip()
    if r:
        return r
    gw = settings_store.get("GATEWAY_NAME") or "EXO Signature Gateway"
    return f"Route via {gw} (Signatur)"


def pruefe_und_merke() -> str | None:
    """Regelzustand in EXO abfragen und in watchdog_state.json ablegen.

    Rückgabe: "Enabled"/"Disabled" — oder None, wenn nicht ermittelbar
    (fehlende Konfiguration, EXO nicht erreichbar). Wirft nie.
    """
    app_id = config.CLIENT_ID or settings_store.get("CLIENT_ID") or ""
    org = settings_store.get("TENANT_DOMAIN") or ""
    if not app_id or not org or not _AUTH_CERT.exists() or not _SCRIPT.exists():
        return None
    try:
        proc = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(_SCRIPT),
             "-AppId", app_id, "-CertPath", str(_AUTH_CERT),
             "-Organization", org, "-RuleName", regelname()],
            capture_output=True, text=True, timeout=120)
        zeilen = [z for z in (proc.stdout or "").splitlines() if z.strip()]
        data = json.loads(zeilen[-1]) if zeilen else {}
    except Exception as exc:                                # noqa: BLE001
        log.warning("Regelzustand nicht ermittelbar: %s", exc)
        return None
    if not data.get("ok"):
        log.info("Regelzustand-Abfrage: %s", data.get("error") or "unbekannt")
        return None
    state = str(data.get("state") or "")
    waechter_state.merge(
        rule_state=state,
        rule_checked=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    if state == "Disabled":
        log.warning("Signatur-Transportregel %r ist DISABLED — Bypass aktiv, "
                    "ausgehende Post geht ohne Signatur.", regelname())
    return state
