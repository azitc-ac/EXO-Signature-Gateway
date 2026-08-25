"""SMTP-Relay für Geräte im eigenen Netz — Ersatz für einen Exchange vor Ort.

ANLASS (2026-08-25)
-------------------
Wiederkehrende Anforderung auf Kundenseite: Drucker, Scanner und Anwendungen
liefern seit Jahren anonym per SMTP bei einem lokalen Exchange ab. Wird der
abgelöst, müssten alle Geräte umgestellt werden — was niemand will. Ein Gateway
vor Ort steht ohnehin und ist mit Exchange Online verbunden.

⚠️ DAS GATEWAY RELAYT HEUTE SCHON — unbeabsichtigt
--------------------------------------------------
`handler.py` reicht jede Nachricht unverändert weiter, deren Absender nicht in
`MAILBOX_CONFIG` steht. Der einzige Schutz ist die Quell-IP-Prüfung. Wer dort
ein Netz einträgt, hat ab diesem Moment ein Relay — ohne Absenderprüfung, ohne
Zielbeschränkung, ohne dass irgendwo stünde, dass das geschieht.

Dieses Modul macht daraus eine bewusste Entscheidung mit Grenzen. Es ist
deshalb KEIN Zugewinn an Fähigkeit, sondern einer an Kontrolle.

DIE DREI GRENZEN
----------------
1. **Netz** — nur ausdrücklich eingetragene Quellnetze. Nicht die Liste aus
   `SMTP_ACL_EXTRA_CIDRS`: Die beantwortet die Frage „darf verbinden" (etwa
   für eine Überwachung) und ist nicht dasselbe wie „darf Post einliefern".
2. **Absender** — nur Domänen, die dem Tenant gehören. Ein übernommener
   Drucker soll nicht als fremde Firma versenden können.
3. **Ziel** — Vorgabe: nur Empfänger im eigenen Tenant. Nach aussen erst nach
   ausdrücklicher Freigabe, denn dafür muss auch der Exchange-Verbinder das
   Weiterleiten erlauben (sonst `550 5.7.54`).

⚠️ NUR IM MODUS `smtp`
----------------------
Der Rückweg entscheidet, ob ein Relay überhaupt funktionieren kann. Nur der
Smarthost-Weg reicht eine Nachricht unverändert weiter — mit dem Absender, den
das Gerät gesetzt hat. Die anderen Wege können das nicht:

  `graph`  Graph sendet immer „als" ein Postfach. Ein Drucker hat keines;
           Graph antwortet `ErrorInvalidUser`.
  `imap`   APPEND legt die Nachricht in ein Zielpostfach — für interne Ziele
           denkbar, für externe nicht, und der Weg ist dafür nie erprobt.

Diese Grenze steht deshalb HIER und nicht nur in der Oberfläche: Wer den Modus
später umstellt, bekäme sonst ein Relay, das Post annimmt und dann verwirft.

⚠️ ZUR AUSFALLRICHTUNG
----------------------
`smtp_acl.is_allowed()` lässt bei leerer Adressliste ALLES durch — bewusst, um
den Mailfluss nicht zu unterbrechen. Für ein Relay ist diese Richtung falsch:
Kennt das Gateway seine eigenen Adressen nicht, kann es weder Absender noch
Empfänger beurteilen. Dann wird das Relay verweigert. Der reguläre Mailfluss
bleibt davon unberührt — er läuft über einen anderen Zweig.
"""
from __future__ import annotations

import ipaddress
import logging

log = logging.getLogger(__name__)

# Ergebnis einer Prüfung: (erlaubt, Grund fürs Protokoll, SMTP-Antwort)
ERLAUBT = (True, "", "")


def _netze() -> list:
    import settings_store
    aus = []
    for eintrag in settings_store.get("SMTP_RELAY_NETWORKS") or []:
        text = str(eintrag).strip()
        if not text:
            continue
        try:
            aus.append(ipaddress.ip_network(text, strict=False))
        except ValueError:
            log.warning("SMTP-Relay: %r ist kein gültiges Netz — übergangen", text)
    return aus


def ist_relay_quelle(ip: str) -> bool:
    """Kommt die Verbindung aus einem für das Relay freigegebenen Netz?"""
    import settings_store
    # `get_bool()` gibt es nur im Hub (settings_schema); im Gateway ist die
    # schlichte Wahrheitsprüfung das übliche Muster für einen Schalter mit
    # Vorgabe AUS — siehe reinject.py bei GRAPH_SMTP_FALLBACK.
    if not settings_store.get("SMTP_RELAY_ENABLED"):
        return False
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return False
    return any(addr in n for n in _netze())


def _eigene_domaenen() -> set[str]:
    """Domänen, die dem Tenant gehören — aus den bekannten Postfachadressen.

    Zusätzlich `TENANT_DOMAIN`, weil die Adressliste Aliasdomänen führt, die
    Startdomäne `…onmicrosoft.com` aber nicht zwingend als Postfachadresse
    auftaucht.
    """
    import exo_mailboxes
    import settings_store
    domaenen = {a.rsplit("@", 1)[-1].lower()
                for a in exo_mailboxes.known_addresses() if "@" in a}
    tenant = (settings_store.get("TENANT_DOMAIN") or "").strip().lower()
    if tenant:
        domaenen.add(tenant)
    return {d for d in domaenen if d}


def pruefe(absender: str, empfaenger: list[str], ip: str) -> tuple[bool, str, str]:
    """Darf diese Nachricht über das Relay? → (erlaubt, Protokollgrund, SMTP-Antwort).

    Wird NUR aufgerufen, wenn `ist_relay_quelle()` bereits zugestimmt hat.
    """
    import exo_mailboxes
    import settings_store

    # ⚠️ Die Ausfallrichtung hängt an den ADRESSEN, nicht an den Domänen.
    #
    # Der erste Entwurf prüfte `_eigene_domaenen()` auf leer — die Menge ist
    # aber nie leer, sobald `TENANT_DOMAIN` gesetzt ist (und das ist sie nach
    # jeder Einrichtung). Die Sicherung wäre damit tot gewesen, und der Test
    # dazu hätte grün gemeldet, was nie greift. Massgeblich ist die
    # Postfachliste: Ohne sie lässt sich kein Ziel beurteilen.
    # ⚠️ Siehe Modulkopf: nur der Smarthost-Weg reicht fremde Absender
    # unveraendert weiter. Die Oberflaeche bietet das Relay deshalb nur im
    # Modus `smtp` an — durchgesetzt wird es hier, weil der Modus danach noch
    # umgestellt werden kann.
    modus = (settings_store.get("REINJECT_MODE") or "smtp").strip()
    if modus != "smtp":
        return (False,
                f"Relay von {ip} abgelehnt — der Rückweg steht auf {modus!r}; "
                "das Relay setzt den SMTP-Smarthost voraus",
                "451 4.3.2 Relay in dieser Betriebsart nicht möglich")

    adressen = exo_mailboxes.known_addresses()
    if not adressen:
        return (False,
                f"Relay von {ip} abgelehnt — die Postfachliste ist (noch) nicht "
                "bekannt, Absender und Ziel lassen sich nicht prüfen",
                "451 4.3.2 Relay temporär nicht verfügbar")

    bekannt = _eigene_domaenen()

    absender = (absender or "").strip().lower()
    domain = absender.rsplit("@", 1)[-1] if "@" in absender else ""
    if domain not in bekannt:
        return (False,
                f"Relay von {ip} abgelehnt — Absenderdomäne {domain or '(leer)'} "
                "gehört nicht zu diesem Tenant",
                "550 5.7.1 Absenderdomäne für das Relay nicht zulässig")

    if settings_store.get("SMTP_RELAY_EXTERNAL"):
        return ERLAUBT

    # Nur interne Ziele: gegen die bekannten ADRESSEN prüfen, nicht gegen die
    # Domänen. Eine Adresse der eigenen Domäne, die es nicht gibt, ist kein
    # internes Ziel — Exchange erzeugte daraus einen Unzustellbarkeitsbericht
    # nach aussen, also doch eine Zustellung nach draussen.
    fremd = [e for e in empfaenger if (e or "").strip().lower() not in adressen]
    if fremd:
        return (False,
                f"Relay von {ip} abgelehnt — Empfänger ausserhalb des Tenants: "
                + ", ".join(fremd[:3]),
                "550 5.7.1 Relay nur an Empfänger im eigenen Tenant")

    return ERLAUBT
