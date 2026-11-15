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


# ── S/MIME-Eingangsregeln ─────────────────────────────────────────────────────
# Kalender-Ausnahme je Regeltyp. Signierte Einladungen externer Absender liefen
# bis v1.12.34 übers Gateway: Es nahm die Signatur ab und stellte neu zu — in
# Graph++ per Ablage, wo Exchange keinen Kalendereintrag anlegt. Mit der
# Ausnahme stellt Exchange sie selbst zu. Verschlüsselte Einladungen sind von
# außen nicht als Kalenderelement erkennbar; dort gibt es keine Ausnahme.
SMIME_KALENDER_AUSNAHME = {"Signed": "Calendaring", "Encrypted": None}

# Stand des Solls der S/MIME-Eingangsregeln. Bei jeder Änderung am Soll
# erhöhen: Installationen mit älterem angewendetem Stand bekommen die Aufgabe,
# die Regeln neu zu schreiben (abnahme._smime_regeln_stand).
SMIME_REGELN_STAND = 2   # 2 = Kalender-Ausnahme an „Signed Inbound“ (v1.12.34)


def smime_kalender_fehlt(typ: str, except_if) -> bool:
    """Fehlt der S/MIME-Eingangsregel `typ` die Kalender-Ausnahme, die sie haben soll?"""
    soll = SMIME_KALENDER_AUSNAHME.get(typ)
    if not soll:
        return False
    werte = except_if if isinstance(except_if, (list, tuple)) else [except_if]
    return soll not in [str(w or "").strip() for w in werte]
