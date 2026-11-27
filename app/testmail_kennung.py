"""Einmal-Kennung der Testmail aus der Oberfläche (`/api/test-mail`).

Die Testmail wird per SMTP an den eigenen Listener auf 127.0.0.1 eingeliefert,
damit sie denselben Weg nimmt wie echte Post. Sie kommt damit aber nicht aus
Exchange und trägt keine `X-MS-Exchange-CrossTenant-Id` — mit der strengen
Herkunftsprüfung (RELAY_TENANT_STRENG) wurde sie bis v1.12.184 abgewiesen
(„554 5.7.1 Access denied: message carries no tenant identifier“, Prod
27.11.2026), ohne sie zählte jede Testmail als `tenant_ohne_kopf` und
verfälschte die Zahl, aus der die Abnahme die strenge Prüfung empfiehlt.

Ausgenommen wird deshalb nicht „Loopback“ pauschal, sondern genau die eine
Testmail: Kopf `X-Gateway-Testmail` mit einer Kennung, die nur dieser Prozess
kennt, die einmal gilt, nach fünf Minuten verfällt und nur von 127.0.0.1/::1
angenommen wird. Gleiches Muster wie die Testmail des EXO SMTP Relays.
"""
from __future__ import annotations

import secrets
import time

KOPF = "X-Gateway-Testmail"
GUELTIG_S = 300
_LOOPBACK = frozenset({"127.0.0.1", "::1"})
_offen: dict[str, float] = {}


def _aufraeumen() -> None:
    jetzt = time.monotonic()
    for k in [k for k, bis in _offen.items() if bis < jetzt]:
        del _offen[k]


def neu() -> str:
    _aufraeumen()
    kennung = secrets.token_urlsafe(24)
    _offen[kennung] = time.monotonic() + GUELTIG_S
    return kennung


def einloesen(msg, peer_ip: str) -> bool:
    """True genau dann, wenn `msg` die gerade aus der Oberfläche verschickte Testmail
    ist — von Loopback, mit gültiger, unverbrauchter Kennung. Verbraucht die Kennung."""
    if peer_ip not in _LOOPBACK:
        return False
    try:
        kennung = (msg.get(KOPF) or "").strip()
    except Exception:                                         # noqa: BLE001
        return False
    if not kennung:
        return False
    _aufraeumen()
    return _offen.pop(kennung, None) is not None
