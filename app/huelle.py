"""Kopfzeilen, die eine S/MIME-Hülle (signiert oder verschlüsselt) von der
inneren Nachricht übernimmt — EINE Liste für alle Hüllen.

openssl/CMS legen die Kopfzeilen nur in den signierten bzw. verschlüsselten
Inhalt. Was die äußere Hülle nicht trägt, sieht weder Exchange noch der
Empfänger vor dem Entschlüsseln.

Bis v1.11.55 stand die Liste dreimal im Code (smime_signer, cms_sign,
handler._restore_envelope_headers), jeweils ohne `Sender`. Gemessen 12.11.2026
im DEV-Tenant: Bei „Senden im Auftrag von“ kam die signierte Mail ohne
Sender-Kopf an — der Empfänger sah nur das geteilte Postfach, nicht die Person.
`Thread-Topic`/`Thread-Index` fehlten ebenso; Outlook ordnet Unterhaltungen
danach.
"""
from __future__ import annotations

import email.message

KOPFZEILEN = ("From", "Sender", "To", "Cc", "Subject", "Date", "Message-ID",
              "Reply-To", "In-Reply-To", "References", "Thread-Topic", "Thread-Index")


def uebernehmen(ziel: email.message.Message, quelle: email.message.Message) -> None:
    """Fehlende Hüllen-Kopfzeilen aus `quelle` in `ziel` setzen (vorhandene bleiben)."""
    for kopf in KOPFZEILEN:
        if not ziel.get(kopf) and quelle.get(kopf):
            ziel[kopf] = quelle[kopf]
