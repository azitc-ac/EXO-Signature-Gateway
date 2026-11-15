"""Soll-Zustand der Signatur-Transportregel — EINE Quelle, ohne Abhängigkeiten.

Benutzt vom laufenden Gateway (waechter_regel → Band auf jeder Seite) UND vom
Offline-Abgleich tools/tenant_soll_check.py. Bis v1.12.19 stand `_gate_leer`
nur im Werkzeug, das nirgends automatisch lief; der Wächter las FromMemberOf
gar nicht. Der Vorfall vom 04.09. (Regel aktiv, Gate leer → jeder interne
Absender läuft durchs Gateway) wäre im Betrieb von nichts gemeldet worden.
"""
from __future__ import annotations


def gate_leer(from_member_of) -> bool:
    """FromMemberOf gilt als leer, wenn keine nicht-leere Kennung enthalten ist.
    (Die PS-Serialisierung liefert für ein leeres Gate [""] oder [], nie None.)"""
    if isinstance(from_member_of, str):
        from_member_of = [from_member_of]
    return not any((str(x) if x is not None else "").strip() for x in (from_member_of or []))


def gate_offen(state: str | None, from_member_of) -> bool:
    """DER gefährliche Zustand: Regel aktiv UND ohne Absender-Gate."""
    return (state or "") == "Enabled" and gate_leer(from_member_of)
