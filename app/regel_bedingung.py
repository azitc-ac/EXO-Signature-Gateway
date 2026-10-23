"""Empfängerbedingung an der Signatur-Transportregel: erkennen und entfernen.

Bis v1.8.63 legte die Einrichtung die Signatur-Regel mit
`SentToScope NotInOrganization` an („The recipient is located
'NotInOrganization'“). Folgen:

* rein interne Post erreicht das Gateway nie — „Rein interne Mails auch
  signieren“ (SIGN_INTERNAL_ONLY_MAIL) wirkt dann nicht;
* bei gemischten Empfängern spaltet Exchange die Mail auf, und der interne
  Teil läuft unsigniert am Gateway vorbei (Bifurkation, siehe CLAUDE.md).

Seit v1.8.64 entsteht die Regel ohne Empfängerbedingung, und „Connector neu
einrichten“ entfernt sie an bestehenden Regeln. Dieser Weg setzt aber auch die
Connectoren neu; deshalb hier ein gezielter Weg, der NUR SentToScope anfasst.

Soll: SentToScope leer. `ist_soll()` ist die EINE Soll-Funktion für
Einstellungsseite, Abnahme und Regelabfrage des Schedulers.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import waechter_regel
import waechter_state

log = logging.getLogger(__name__)

_ENTFERNEN = Path("/app/scripts/remove_rule_recipient_scope.ps1")

OK = "ok"
BEDINGUNG = "bedingung"
UNBEKANNT = "unbekannt"


def ist_soll(sent_to_scope: str | None) -> bool:
    """Keine Empfängerbedingung an der Regel."""
    return not (sent_to_scope or "").strip()


def pruefen() -> dict:
    """Live in Exchange nachsehen (nur lesend).

    {"zustand": ok|bedingung|unbekannt, "sent_to_scope", "regel"}
    """
    regel = waechter_regel.regelname()
    erg = waechter_regel.regel_lesen()
    if erg is None:
        return {"zustand": UNBEKANNT, "sent_to_scope": "", "regel": regel}
    scope = erg["sent_to_scope"]
    return {"zustand": OK if ist_soll(scope) else BEDINGUNG,
            "sent_to_scope": scope, "regel": regel}


def entfernen(von: str) -> dict:
    """SentToScope an der Signatur-Regel entfernen und nachlesen.

    {"ok", "geaendert", "vorher", "nachher"} oder {"ok": False, "error"}.
    """
    regel = waechter_regel.regelname()
    data = waechter_regel.ps_regelskript(_ENTFERNEN)
    if data is None:
        return {"ok": False, "error": "Exchange nicht erreichbar oder Zugangsdaten fehlen."}
    if data.get("ok"):
        waechter_state.merge(
            rule_sent_to_scope=str(data.get("nachher") or ""),
            rule_checked=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        if data.get("geaendert"):
            log.warning("Empfängerbedingung %r an Regel %r entfernt (von %s) — "
                        "interne Post erreicht das Gateway ab jetzt (nach 5–15 Min.)",
                        data.get("vorher"), regel, von)
    else:
        log.warning("Empfängerbedingung an %r nicht entfernt (von %s): %s",
                    regel, von, data.get("error"))
    return {"ok": bool(data.get("ok")), "geaendert": bool(data.get("geaendert")),
            "vorher": str(data.get("vorher") or ""), "nachher": str(data.get("nachher") or ""),
            "error": str(data.get("error") or ""), "regel": regel}
