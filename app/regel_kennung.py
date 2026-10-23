"""Loop-Ausnahme an den Gateway-Transportregeln: prüfen und angleichen.

Jede Regel, die Post zum Gateway leitet, nimmt bereits bearbeitete Post über
die Markierung `LOOP_HEADER` aus. Bis v1.10.43 war deren Wert überall `1` —
auch bei jedem ANDEREN Gateway. Post eines fremden Gateways (anderer Kunde,
anderer Tenant) trug sie schon und wurde von den eigenen Regeln ausgenommen:
Die eingehenden S/MIME-Regeln übernahmen kein Absenderzertifikat und
entschlüsselten nichts (gemessen 23.10.2026). Seit v1.10.44 ist der Wert je
Tenant eigen (`loop_detector.marker_wert`), die Regeln prüfen ihn verankert
(`loop_detector.marker_muster`).

Neue Installationen bekommen das Muster vom Einrichtungsweg (setup_wizard
`_loop_muster`). Bestehende Regeln gleicht `angleichen()` an — NUR die
Loop-Ausnahme, nichts sonst. Bis dahin nehmen die alten Regeln (unverankertes
`1`) die neue Markierung weiter aus, weil sie mit `1-` beginnt.

`ist_soll()` ist die EINE Soll-Funktion für Einstellungsseite und Abnahme.
"""
from __future__ import annotations

import logging
from pathlib import Path

import loop_detector
import settings_store
import waechter_regel

log = logging.getLogger(__name__)

_LESEN = Path("/app/scripts/get_rule_loop_marker.ps1")
_ANGLEICHEN = Path("/app/scripts/set_rule_loop_marker.ps1")

OK = "ok"
ABWEICHUNG = "abweichung"
KEINE = "keine"            # keine der Gateway-Regeln gefunden
UNBEKANNT = "unbekannt"


def regelnamen() -> list[str]:
    """Alle Regeln, die Post zu DIESEM Gateway leiten (Namen aus den Quellen
    der Einrichtung). Fehlende werden beim Lesen übersprungen."""
    import regel_split
    gw = settings_store.get("GATEWAY_NAME") or "EXO Signature Gateway"
    namen = [waechter_regel.regelname(), waechter_regel.basis_regelname(),
             regel_split.smime_regelname(),
             f"{gw} - SMIME Signed Inbound", f"{gw} - SMIME Encrypted Inbound"]
    return list(dict.fromkeys(n for n in namen if n))


def ist_soll(regel: dict) -> bool:
    """Header = LOOP_HEADER und genau das eigene, verankerte Muster."""
    return (regel.get("header") == loop_detector.header_name()
            and list(regel.get("patterns") or []) == [loop_detector.marker_muster()])


def pruefen() -> dict:
    """Live in Exchange nachsehen (nur lesend).

    {"zustand": ok|abweichung|keine|unbekannt, "regeln": [...], "soll"}
    """
    soll = loop_detector.marker_muster()
    data = waechter_regel.ps_regelskript(_LESEN, regeln=",".join(regelnamen()))
    if not data or not data.get("ok"):
        log.info("Loop-Ausnahme nicht lesbar: %s", (data or {}).get("error") or "unbekannt")
        return {"zustand": UNBEKANNT, "regeln": [], "soll": soll}
    regeln = [r for r in (data.get("regeln") or []) if r.get("found")]
    if not regeln:
        return {"zustand": KEINE, "regeln": [], "soll": soll}
    for r in regeln:
        r["ok"] = ist_soll(r)
    return {"zustand": OK if all(r["ok"] for r in regeln) else ABWEICHUNG,
            "regeln": regeln, "soll": soll}


def angleichen(von: str) -> dict:
    """Loop-Ausnahme aller vorhandenen Gateway-Regeln auf das eigene Muster
    setzen und nachlesen. {"ok", "regeln": [{name, vorher, nachher}]} oder
    {"ok": False, "error"}."""
    muster = loop_detector.marker_muster()
    data = waechter_regel.ps_regelskript(
        _ANGLEICHEN, regeln=",".join(regelnamen()),
        extra=("-LoopHeader", loop_detector.header_name(), "-LoopPattern", muster))
    if data is None:
        return {"ok": False, "error": "Exchange nicht erreichbar oder Zugangsdaten fehlen."}
    regeln = [r for r in (data.get("regeln") or []) if r.get("found")]
    if data.get("ok"):
        log.warning("Loop-Ausnahme an %d Regel(n) auf %s angeglichen (von %s): %s",
                    len(regeln), muster, von, ", ".join(r.get("name", "") for r in regeln))
    else:
        log.warning("Loop-Ausnahme nicht angeglichen (von %s): %s", von,
                    data.get("error") or regeln)
    return {"ok": bool(data.get("ok")), "regeln": regeln,
            "error": str(data.get("error") or ""), "soll": muster}
