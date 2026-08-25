"""Der Relay-Zweig umgeht die Quell-IP-Prüfung — und darf sie nicht aushebeln.

ANLASS (2026-08-25)
-------------------
`smtp_relay` selbst ist an anderer Stelle geprüft. Hier geht es um die vier
Zeilen Verdrahtung in `handler.handle_DATA`, und die tragen die gefährlichere
Invariante: Bis heute schützt eine einzige Bedingung den Listener auf Port 25 —
`smtp_acl.is_allowed(peer_ip)`. Der Relay-Zweig hängt sich davor.

⚠️ Warum das getestet gehört, obwohl es nur eine Verzweigung ist: Ein Fehler in
der Richtung „Drucker wird abgewiesen" fällt sofort auf — jemand ruft an. Ein
Fehler in der Gegenrichtung ist still. Wäre `ist_relay_quelle()` versehentlich
für jede Adresse wahr, liefe der Listener ohne Zugangsschutz weiter, und nichts
im Betrieb würde sich ändern, bis er missbraucht wird.

Geprüft wird deshalb vor allem, dass die Umgehung eng bleibt: nur bei
eingeschaltetem Relay, nur aus dem eingetragenen Netz.
"""
import asyncio
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import handler  # noqa: E402
import smtp_relay  # noqa: E402


class _Sitzung:
    def __init__(self, ip):
        self.peer = (ip, 5000)


class _Umschlag:
    def __init__(self, absender, empfaenger):
        self.mail_from = absender
        self.rcpt_tos = list(empfaenger)
        self.content = b"Subject: Scan\r\n\r\nAnbei.\r\n"


@pytest.fixture
def anlage(monkeypatch):
    """Relay an für 10.1.5.0/24; die Quell-IP-Liste weist alles ab."""
    werte = {
        "SMTP_RELAY_ENABLED": True,
        "SMTP_RELAY_NETWORKS": ["10.1.5.0/24"],
        "SMTP_RELAY_EXTERNAL": False,
        "REINJECT_MODE": "smtp",
        "TENANT_DOMAIN": "firma.onmicrosoft.com",
    }
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))

    import exo_mailboxes
    monkeypatch.setattr(exo_mailboxes, "known_addresses",
                        lambda: {"chefin@firma.de"})

    # Die Quell-IP-Liste sagt zu ALLEM nein. Wer trotzdem durchkommt, kam über
    # den Relay-Zweig — das macht den Test trennscharf.
    import smtp_acl
    monkeypatch.setattr(smtp_acl, "is_allowed", lambda ip: False)
    monkeypatch.setattr(smtp_acl, "record_reject", lambda ip: None)

    # Der Anschlag sitzt an der Zustellung, nicht am Anfang der Verarbeitung:
    # So misst der Test, dass die Nachricht den ganzen Weg bis dorthin kommt —
    # und nicht bloss, dass sie eine frühe Verzweigung passiert hat.
    gesehen = {}

    import reinject
    def _halt(*a, **kw):
        gesehen["zugestellt"] = (a, kw)
        return True
    monkeypatch.setattr(reinject, "send", _halt)

    import mail_audit
    ereignisse = []
    monkeypatch.setattr(mail_audit, "log_event",
                        lambda **kw: ereignisse.append(kw))
    werte["_ereignisse"] = ereignisse
    werte["_gesehen"] = gesehen
    return werte


def _lauf(ip, absender="drucker@firma.de", empfaenger=("chefin@firma.de",)):
    h = handler.SignatureHandler()
    return asyncio.run(h.handle_DATA(None, _Sitzung(ip), _Umschlag(absender, empfaenger)))


def test_relay_netz_umgeht_die_quell_ip_liste(anlage):
    """Der Drucker kommt durch, obwohl die Liste alles abweist."""
    antwort = _lauf("10.1.5.30")
    assert antwort.startswith("250"), antwort
    assert anlage["_gesehen"], "die Nachricht kam nicht bis zur Zustellung"


def test_fremdes_netz_wird_weiter_abgewiesen(anlage):
    """Die Umgehung gilt nur für das eingetragene Netz — sonst nichts."""
    antwort = _lauf("203.0.113.9")
    assert antwort == "554 5.7.1 Access denied", antwort


def test_abgeschaltetes_relay_schuetzt_wieder_vollstaendig(anlage):
    """⚠️ Die eigentliche Sorge: dass die Umgehung stehen bleibt.

    Ohne den Schalter muss auch die eingetragene Adresse wieder an der
    Quell-IP-Liste scheitern.
    """
    anlage["SMTP_RELAY_ENABLED"] = False
    assert _lauf("10.1.5.30") == "554 5.7.1 Access denied"


def test_fremde_absenderdomaene_wird_abgewiesen_und_protokolliert(anlage):
    """Abgelehnt — und im Mail-Protokoll auffindbar, nicht nur im Logbuch."""
    antwort = _lauf("10.1.5.30", absender="werbung@fremd.example")
    assert antwort.startswith("550"), antwort
    assert not anlage["_gesehen"], "abgelehnte Post darf nicht zugestellt werden"

    ereignisse = anlage["_ereignisse"]
    assert len(ereignisse) == 1, ereignisse
    assert ereignisse[0]["action"] == "relay_abgelehnt"
    assert "fremd.example" in (ereignisse[0]["error"] or "")


def test_externes_ziel_ohne_freigabe_wird_abgewiesen(anlage):
    antwort = _lauf("10.1.5.30", empfaenger=("kunde@extern.example",))
    assert antwort.startswith("550"), antwort
    assert not anlage["_gesehen"]
