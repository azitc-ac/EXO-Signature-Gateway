"""Post eines eingetragenen FREMDEN Tenants geht byte-genau an dessen Ziel.

ANLASS: Zwei Gateways teilen sich eine öffentliche Adresse und damit Port 25.
Exchange Online kann für einen Outbound-Connector keinen anderen Port wählen,
also nimmt das erste Gateway die Post beider Tenants an. Die des zweiten muss
unverändert weiter — und zwar nach der Kennung, die Exchange selbst stempelt
(`X-MS-Exchange-CrossTenant-Id`), nicht nach der frei wählbaren Absenderdomäne.

Die Tests schlagen fehl, wenn
- die Weiterleitung fehlt (Post käme mit 554 zurück),
- sie ohne Route greift (Tenant-Gate umgangen),
- sie die Nachricht verändert oder über den eigenen Rückweg schickt,
- ein gescheiterter Hop als zugestellt quittiert würde (Mailverlust),
- eine Route auf den eigenen Tenant angelegt werden könnte.
"""
import asyncio
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import domain_routing  # noqa: E402
import handler  # noqa: E402

OWN = "11111111-1111-1111-1111-111111111111"
FREMD = "ca5a81bc-ba86-4b1f-986a-ccb3a5c6b8ea"
ANDERER = "99999999-9999-9999-9999-999999999999"


class _Sitzung:
    def __init__(self, ip, ident=None):
        self.peer = (ip, 5000)
        self.ssl = None
        self.authenticated = bool(ident)
        self.auth_data = ident


class _Umschlag:
    def __init__(self, content: bytes):
        self.mail_from = "nutzer@azitc.org"
        self.rcpt_tos = ["extern@example.com"]
        self.content = content


def _nachricht(cross_tenant: str | None) -> bytes:
    kopf = "Subject: Hallo\r\nFrom: nutzer@azitc.org\r\n"
    if cross_tenant is not None:
        kopf += f"X-MS-Exchange-CrossTenant-Id: {cross_tenant}\r\n"
    return (kopf + "\r\nText  mit  Leerzeichen\r\n").encode()


@pytest.fixture
def anlage(monkeypatch):
    werte = {
        "TENANT_ID": OWN,
        "RELAY_TENANT_CHECK": True,
        "SMTP_RELAY_ENABLED": False,
        "REINJECT_MODE": "smtp",
        "MAILBOX_CONFIG": {},
        "RELAY_TARGETS": {"org": {"host": "192.168.1.124", "port": 25, "starttls": True}},
        "TENANT_ROUTES": {FREMD: "org"},
    }
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))
    import config
    monkeypatch.setattr(config, "TENANT_ID", "", raising=False)

    import smtp_acl
    monkeypatch.setattr(smtp_acl, "is_allowed", lambda ip: True)
    monkeypatch.setattr(smtp_acl, "record_reject", lambda ip: None)

    import reinject
    eigen, weiter = [], []
    monkeypatch.setattr(reinject, "send", lambda *a, **kw: eigen.append(a) or True)

    def _hop(ziel, absender, empfaenger, inhalt):
        if werte.get("_hop_fehler"):
            raise OSError("Verbindung abgelehnt")
        weiter.append((ziel, absender, list(empfaenger), inhalt))
    monkeypatch.setattr(reinject, "_relay_to_target", _hop)

    import relay_stats
    monkeypatch.setattr(relay_stats, "merke_routing", lambda *a, **kw: None)
    import mail_audit
    ereignisse = []
    monkeypatch.setattr(mail_audit, "log_event", lambda **kw: ereignisse.append(kw))

    werte.update(_eigen=eigen, _weiter=weiter, _ereignisse=ereignisse)
    return werte


def _lauf(content: bytes, ip="40.92.10.10", ident=None):
    h = handler.SignatureHandler()
    return asyncio.run(h.handle_DATA(None, _Sitzung(ip, ident), _Umschlag(content)))


def test_eingetragener_tenant_geht_bytegenau_ans_ziel(anlage):
    roh = _nachricht(FREMD)
    antwort = _lauf(roh)
    assert antwort.startswith("250"), antwort
    assert anlage["_weiter"] == [("org", "nutzer@azitc.org", ["extern@example.com"], roh)]
    assert anlage["_eigen"] == [], "darf nicht über den eigenen Rückweg gehen"
    assert any(e.get("action") == "tenant_weitergeleitet" for e in anlage["_ereignisse"])


def test_grossschreibung_der_kennung_egal(anlage):
    assert _lauf(_nachricht(FREMD.upper())).startswith("250")
    assert anlage["_weiter"]


def test_ohne_route_bleibt_es_bei_der_abweisung(anlage):
    antwort = _lauf(_nachricht(ANDERER))
    assert antwort.startswith("554"), antwort
    assert anlage["_weiter"] == [] and anlage["_eigen"] == []


def test_route_auf_geloeschtes_ziel_weist_ab(anlage):
    anlage["RELAY_TARGETS"] = {}
    assert _lauf(_nachricht(FREMD)).startswith("554")
    assert anlage["_weiter"] == [] and anlage["_eigen"] == []


def test_gescheiterter_hop_ist_vorlaeufig(anlage):
    anlage["_hop_fehler"] = True
    antwort = _lauf(_nachricht(FREMD))
    assert antwort.startswith("451"), antwort
    assert anlage["_eigen"] == [], "kein Rückfall auf den eigenen Smarthost"


def test_eigener_tenant_unberuehrt(anlage):
    anlage["TENANT_ROUTES"] = {OWN: "org"}      # selbst eine Altlast darf nicht greifen
    assert _lauf(_nachricht(OWN)).startswith("250")
    assert anlage["_weiter"] == [] and anlage["_eigen"]


def test_wirkt_auch_bei_abgeschaltetem_gate(anlage):
    anlage["RELAY_TENANT_CHECK"] = False
    assert _lauf(_nachricht(FREMD)).startswith("250")
    assert anlage["_weiter"] and anlage["_eigen"] == []


def test_relay_geraet_wird_nicht_weitergeleitet(anlage, monkeypatch):
    import smtp_relay
    monkeypatch.setattr(smtp_relay, "ist_relay_quelle", lambda ip: True)
    monkeypatch.setattr(smtp_relay, "pruefe", lambda *a: (True, "", ""))
    import relay_hosts, relay_stats
    monkeypatch.setattr(relay_hosts, "merke_zustellung", lambda *a, **kw: None)
    monkeypatch.setattr(relay_stats, "merke", lambda *a, **kw: None)
    _lauf(_nachricht(FREMD), ip="192.168.1.50")
    assert anlage["_weiter"] == [], "lokale Geräte tragen keine vertrauenswürdige Kennung"


def test_authentifizierte_einlieferung_wird_nicht_weitergeleitet(anlage, monkeypatch):
    # Ein Gerät mit Login schreibt seinen Kopf selbst — eine Kennung darin
    # stammt nicht von Exchange und darf die Post nicht umleiten.
    import sende_identitaeten, relay_stats
    monkeypatch.setattr(sende_identitaeten, "pruefe", lambda *a: (True, "", ""))
    monkeypatch.setattr(relay_stats, "merke", lambda *a, **kw: None)
    monkeypatch.setattr(relay_stats, "heute_zaehler", lambda *a: 0)
    _lauf(_nachricht(FREMD), ip="192.168.1.60", ident={"login": "drucker"})
    assert anlage["_weiter"] == []


# ── Verwaltung ──────────────────────────────────────────────────────────────

@pytest.fixture
def speicher(monkeypatch):
    werte = {"TENANT_ID": OWN, "RELAY_TARGETS": {"org": {"host": "h", "port": 25}},
             "TENANT_ROUTES": {}, "DOMAIN_ROUTES": {}, "RELAY_TARGET_PW": {}}
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))
    monkeypatch.setattr(settings_store, "update", lambda d: werte.update(d))
    import config
    monkeypatch.setattr(config, "TENANT_ID", "", raising=False)
    return werte


def test_route_setzen_und_entfernen(speicher):
    domain_routing.setze_tenant_route(FREMD.upper(), "org")
    assert speicher["TENANT_ROUTES"] == {FREMD: "org"}
    domain_routing.setze_tenant_route(FREMD, "")
    assert speicher["TENANT_ROUTES"] == {}


@pytest.mark.parametrize("tid", ["", "kein-guid", FREMD + "x", "@" + FREMD])
def test_nur_guids(speicher, tid):
    with pytest.raises(ValueError):
        domain_routing.setze_tenant_route(tid, "org")


def test_eigener_tenant_nicht_routbar(speicher):
    with pytest.raises(ValueError):
        domain_routing.setze_tenant_route(OWN, "org")


def test_unbekanntes_ziel_abgelehnt(speicher):
    with pytest.raises(ValueError):
        domain_routing.setze_tenant_route(FREMD, "gibtsnicht")


def test_ziel_loeschen_nimmt_tenant_routen_mit(speicher):
    speicher["TENANT_ROUTES"] = {FREMD: "org"}
    assert domain_routing.loesche_ziel("org")
    assert speicher["TENANT_ROUTES"] == {}
