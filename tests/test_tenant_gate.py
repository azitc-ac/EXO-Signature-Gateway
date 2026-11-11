"""Post aus dem EXO-IP-Raum muss aus dem EIGENEN Tenant stammen.

ANLASS: Microsofts Exchange-Online-Adressbereiche (gegen die die Quell-IP-Liste
prüft) teilen sich ALLE M365-Tenants weltweit. `handle_DATA` leitete Post, deren
Absender nicht in MAILBOX_CONFIG steht, unverändert weiter — im Vorgabe-Modus
`smtp` an den Smarthost. Ein fremder Tenant konnte so über einen
Outbound-Connector auf unseren Hostnamen den Gateway als offenes Relay unter der
Reputation des eigenen Tenants missbrauchen. Das Gate prüft
`X-MS-Exchange-CrossTenant-Id` gegen die eigene TENANT_ID.

Der Test schlägt fehl, wenn das Gate entfernt wird (fremde Post käme durch).
"""
import asyncio
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import handler  # noqa: E402

OWN = "11111111-1111-1111-1111-111111111111"
FREMD = "99999999-9999-9999-9999-999999999999"


class _Sitzung:
    def __init__(self, ip):
        self.peer = (ip, 5000)
        self.ssl = None


class _Umschlag:
    def __init__(self, content: bytes, absender="wer@example.com",
                 empfaenger=("ziel@example.com",)):
        self.mail_from = absender
        self.rcpt_tos = list(empfaenger)
        self.content = content


def _nachricht(cross_tenant: str | None) -> bytes:
    kopf = "Subject: Hallo\r\n"
    if cross_tenant is not None:
        kopf += f"X-MS-Exchange-CrossTenant-Id: {cross_tenant}\r\n"
    return (kopf + "\r\nText\r\n").encode()


@pytest.fixture
def anlage(monkeypatch):
    """Verbindung aus dem (stub-)erlaubten EXO-Raum, kein Relay-Gerät, eigener
    Tenant = OWN, Zustellung wird abgefangen."""
    werte = {
        "TENANT_ID": OWN,
        "RELAY_TENANT_CHECK": True,
        "SMTP_RELAY_ENABLED": False,
        "REINJECT_MODE": "smtp",
        "MAILBOX_CONFIG": {},
    }
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))

    import smtp_acl
    monkeypatch.setattr(smtp_acl, "is_allowed", lambda ip: True)          # EXO-Quelle erlaubt
    monkeypatch.setattr(smtp_acl, "record_reject", lambda ip: None)

    zugestellt = []
    import reinject
    monkeypatch.setattr(reinject, "send", lambda *a, **kw: zugestellt.append((a, kw)) or True)

    import mail_audit
    ereignisse = []
    monkeypatch.setattr(mail_audit, "log_event", lambda **kw: ereignisse.append(kw))

    werte["_zugestellt"] = zugestellt
    werte["_ereignisse"] = ereignisse
    return werte


def _lauf(content: bytes, ip="40.92.10.10"):
    h = handler.SignatureHandler()
    return asyncio.run(h.handle_DATA(None, _Sitzung(ip), _Umschlag(content)))


def test_fremder_tenant_wird_abgewiesen(anlage):
    antwort = _lauf(_nachricht(FREMD))
    assert antwort.startswith("554"), antwort
    assert anlage["_zugestellt"] == [], "fremde Post darf nicht zugestellt werden"
    assert any(e.get("action") == "tenant_fremd" for e in anlage["_ereignisse"]), \
        "Abweisung fehlt im Mail-Protokoll"


def test_eigener_tenant_kommt_durch(anlage):
    antwort = _lauf(_nachricht(OWN))
    assert antwort.startswith("250"), antwort
    assert anlage["_zugestellt"], "eigene Post muss zugestellt werden"


def test_grossschreibung_egal(anlage):
    _lauf(_nachricht(OWN.upper()))
    assert anlage["_zugestellt"], "Tenant-Vergleich muss case-insensitiv sein"


def test_ohne_header_wird_angenommen(anlage):
    # Fehlt der Header, kann nicht geprüft werden → annehmen (sichtbar geloggt),
    # nicht abweisen: der konkrete Angriff trägt eine (fremde) Kennung.
    antwort = _lauf(_nachricht(None))
    assert antwort.startswith("250"), antwort
    assert anlage["_zugestellt"]


def test_notnagel_aus_laesst_fremde_post_durch(anlage):
    anlage["RELAY_TENANT_CHECK"] = False
    antwort = _lauf(_nachricht(FREMD))
    assert antwort.startswith("250"), antwort
    assert anlage["_zugestellt"], "bei abgeschaltetem Gate muss Post durchgehen"


# ── Strenge Prüfung und Zähler (v1.11.4) ─────────────────────────────────────

@pytest.fixture
def zaehler(monkeypatch):
    import stats
    gezaehlt = []
    monkeypatch.setattr(stats, "increment", lambda k: gezaehlt.append(k))
    return gezaehlt


def test_streng_weist_post_ohne_kennung_ab(anlage, zaehler):
    anlage["RELAY_TENANT_STRENG"] = True
    antwort = _lauf(_nachricht(None))
    assert antwort.startswith("554"), antwort
    assert anlage["_zugestellt"] == []
    assert any(e.get("action") == "tenant_fremd" for e in anlage["_ereignisse"])


def test_streng_laesst_eigene_post_durch(anlage, zaehler):
    anlage["RELAY_TENANT_STRENG"] = True
    assert _lauf(_nachricht(OWN)).startswith("250")


def test_zaehler_mit_bezugsgroesse(anlage, zaehler):
    _lauf(_nachricht(OWN))
    _lauf(_nachricht(None))
    assert zaehler.count("tenant_geprueft") == 1
    assert zaehler.count("tenant_ohne_kopf") == 1


def test_relay_geraete_zaehlen_nicht(anlage, zaehler, monkeypatch):
    """Lokale Geräte tragen die Kennung nie — sie gehören nicht in die Zahl
    und werden auch streng nicht abgewiesen."""
    import smtp_relay
    monkeypatch.setattr(smtp_relay, "ist_relay_quelle", lambda ip: True)
    anlage["RELAY_TENANT_STRENG"] = True
    anlage["SMTP_RELAY_ENABLED"] = True
    _lauf(_nachricht(None), ip="192.168.1.50")
    assert "tenant_ohne_kopf" not in zaehler
    assert not any(e.get("action") == "tenant_fremd" for e in anlage["_ereignisse"])


@pytest.mark.parametrize("geprueft,ohne,streng,erwartet", [
    (0, 0, False, "ok"),          # Beobachtung läuft
    (500, 0, False, "offen"),     # Empfehlung: einschalten
    (500, 3, False, "offen"),     # erst klären
    (500, 3, True, "ok"),
])
def test_abnahme_empfiehlt_nach_beobachtung(monkeypatch, geprueft, ohne, streng, erwartet):
    import abnahme
    import settings_store
    import stats
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a, **kw: {"RELAY_TENANT_STRENG": streng}.get(k))
    monkeypatch.setattr(stats, "get", lambda: {"tenant_geprueft": geprueft,
                                               "tenant_ohne_kopf": ohne})
    p = abnahme._tenant_herkunft()
    assert p["zustand"] == erwartet, p
    if ohne and not streng:
        assert f"{ohne} von {geprueft + ohne}" in p["befund"]


def test_angemeldete_einlieferung_587_ist_ausgenommen(anlage, zaehler, monkeypatch):
    """Sende-Identitäten liefern ohne Tenant-Kennung ein — der strenge Modus
    darf sie nicht treffen."""
    import relay_stats
    import sende_identitaeten
    monkeypatch.setattr(sende_identitaeten, "pruefe", lambda *a: (True, "", ""))
    monkeypatch.setattr(relay_stats, "merke", lambda *a, **kw: None)
    anlage["RELAY_TENANT_STRENG"] = True
    sitzung = _Sitzung("203.0.113.7")
    sitzung.authenticated = True
    sitzung.auth_data = {"login": "drucker", "name": "Drucker"}
    antwort = asyncio.run(handler.SignatureHandler().handle_DATA(
        None, sitzung, _Umschlag(_nachricht(None))))
    assert not antwort.startswith("554"), antwort
    assert "tenant_ohne_kopf" not in zaehler
