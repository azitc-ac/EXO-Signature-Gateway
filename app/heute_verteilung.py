"""Verteilung der heute verarbeiteten Mails — Grundlage der beiden Diagramme
„Heute gesendet“ / „Heute empfangen“ oben in der Übersicht.

EIN Weg mit der Tabelle: Gezählt wird im Mail-Protokoll (mail_audit), genau wie
die Klicklisten hinter den Zahlen, und ein Klick auf ein Stück öffnet die Liste
derselben Aktionen. Jede Protokollzeile trägt GENAU EINE Aktion je Mail
(handler.py), deshalb sind die Stücke überschneidungsfrei: Eine signierte UND
verschlüsselte Mail ist `smime_signiert_verschluesselt` und zählt nur unter
„verschlüsselt“ (höchste Stufe zuerst). Zusammen ergeben die Stücke beider
Diagramme genau die Gruppe „@verarbeitet“ — geprüft in
tests/test_heute_verteilung.py.

⚠️ Kein Stück „ohne S/MIME“ bei „empfangen“: Eingehende Post erreicht das
Gateway nur über die S/MIME-Regeln (setup_smime_rules.ps1:
FromScope NotInOrganization + MessageTypeMatches Signed/Encrypted). Was ohne
S/MIME ankommt, sieht das Gateway nie — eine Zahl dafür wäre erfunden.
"""
from __future__ import annotations

from datetime import datetime

# (Schlüssel, Beschriftung, Aktionen) — Reihenfolge = höchste Stufe zuerst.
GESENDET: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("verschluesselt", "S/MIME verschlüsselt", ("smime_encrypted", "smime_signiert_verschluesselt")),
    ("smime_signiert", "S/MIME signiert", ("smime_signed",)),
    ("portal", "Über das Portal", ("portal",)),
    ("signatur", "Nur Signatur", ("signed",)),
    ("zustellfehler", "Zustellfehler", ("zustellfehler",)),
)
EMPFANGEN: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("verschluesselt", "S/MIME verschlüsselt", ("smime_decrypted",)),
    ("signiert", "S/MIME signiert", ("smime_signiert_empfangen",)),
)


def heute() -> str:
    """Heutiges Datum in der Anzeige-Zeitzone (wie die Spalte „Heute“)."""
    import settings_store
    return datetime.now(settings_store.anzeige_zone()).strftime("%Y-%m-%d")


def _diagramm(stuecke, datum: str) -> dict:
    import mail_audit
    teile = []
    for schluessel, beschriftung, aktionen in stuecke:
        aktion = ",".join(aktionen)
        teile.append({"schluessel": schluessel, "beschriftung": beschriftung, "aktion": aktion,
                      "anzahl": mail_audit.count_events(date=datum, action=aktion)})
    summe = sum(t["anzahl"] for t in teile)
    # Bögen für den Donut (Umfang 100): Start und Länge je Stück.
    start = 0.0
    for t in teile:
        t["anteil"] = round(100.0 * t["anzahl"] / summe, 3) if summe else 0.0
        t["start"] = round(start, 3)
        start += t["anteil"]
    return {"summe": summe, "teile": teile}


def verteilung(datum: str | None = None) -> dict:
    datum = datum or heute()
    return {"datum": datum, "gesendet": _diagramm(GESENDET, datum),
            "empfangen": _diagramm(EMPFANGEN, datum)}
