"""Nach einer Änderung der Anbindung an Exchange lokal (EWS_ONPREM_*) werden
die Hybrid-Nutzer im Hintergrund neu geprüft (v1.12.72).

Anlass: Nach dem Einrichten der Anbindung zeigte der Status weiter
„Abwesenheit: entfällt“ und „Postfach lesen (EWS): entfällt“ — der Stand von
vor der Einrichtung, bis zum nächsten Prüflauf.

Schlägt fehl, wenn
  * der Speicherweg den Nachlauf nicht mehr anstößt (oder bei jeder anderen
    Einstellung — er kostet einen PowerShell-Lauf),
  * ein „OK“ der ALTEN Anbindung stehen bliebe (die Prüfungen nehmen ein
    frisches OK sechs Stunden lang ungeprüft wieder),
  * EXO-Postfächer mitgeprüft würden oder ein zweiter Nachlauf parallel startet.

Testdaten erfunden (firma.at).
"""
from __future__ import annotations

import asyncio

import pytest

import exo_mailboxes
import health_check
import settings_store

HYBRID = "max.mustermann@firma.at"
CLOUD = "erika.musterfrau@firma.at"


@pytest.fixture
def umgebung(monkeypatch):
    store = {
        "EWS_ONPREM_URL": "https://exchange.firma.at/EWS/Exchange.asmx",
        "EWS_ONPREM_KONTO": "FIRMA\\svc-gateway", "EWS_ONPREM_KENNWORT": "alt",
        "MAILBOX_CONFIG": {"g-h": {"sig": True, "hybrid": True, "primary": HYBRID},
                           "g-c": {"sig": True, "primary": CLOUD}},
        "MAILBOX_HEALTH": {
            HYBRID: {"overall": "ok", "checks": {
                "abwesenheit": {"status": "ok", "detail": "EWS-Zugriff: alt"},
                "postfach_lesen": {"status": "skip", "detail": "entfällt"},
                "template": {"status": "ok", "detail": "Vorlage"}}},
            CLOUD: {"overall": "ok", "checks": {
                "abwesenheit": {"status": "ok", "detail": "Graph"}}}},
    }
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(settings_store, "force_update", lambda p: store.update(p))
    monkeypatch.setattr(settings_store, "update", lambda p: store.update(p))
    monkeypatch.setattr(exo_mailboxes, "ist_hybrid", lambda e: e == HYBRID)
    laeufe = []

    async def run_all(emails=None):
        laeufe.append(list(emails))
        await asyncio.sleep(0.05)
        return {}
    monkeypatch.setattr(health_check, "run_all_checks", run_all)
    health_check._ews_lauf.clear()
    yield store, laeufe
    health_check._ews_lauf.clear()


@pytest.mark.parametrize("neu,erwartet", [
    ({"EWS_ONPREM_URL": "https://exchange2.firma.at/EWS/Exchange.asmx"}, True),
    ({"EWS_ONPREM_KENNWORT": "neu"}, True),
    ({"EWS_ONPREM_VERBINDEN": "10.99.0.5"}, True),
    ({"EWS_ONPREM_CA": "-----BEGIN CERTIFICATE-----"}, True),
    ({"EWS_ONPREM_KONTO": "FIRMA\\svc-gateway"}, False),         # unverändert
    ({"LOG_LEVEL": "DEBUG"}, False),
    ({}, False),
])
def test_ews_geaendert(umgebung, neu, erwartet):
    assert health_check.ews_geaendert(neu, settings_store.get) is erwartet


def test_nachlauf_nur_hybrid_und_altes_ok_verworfen(umgebung):
    store, laeufe = umgebung

    async def ablauf():
        assert health_check.nach_ews_aenderung() == "gestartet"
        assert health_check.nach_ews_aenderung() == "laeuft", "gedrosselt: kein zweiter Lauf"
        await asyncio.gather(*list(health_check._ews_lauf))
    asyncio.run(ablauf())
    assert laeufe == [[HYBRID]], "nur Hybrid-Nutzer, genau ein Lauf"
    checks = store["MAILBOX_HEALTH"][HYBRID]["checks"]
    assert "abwesenheit" not in checks and "postfach_lesen" not in checks
    assert checks["template"]["status"] == "ok", "andere Prüfungen bleiben"
    assert store["MAILBOX_HEALTH"][CLOUD]["checks"]["abwesenheit"]["detail"] == "Graph"
    assert not health_check._ews_lauf


def test_ohne_hybrid_kein_lauf(umgebung, monkeypatch):
    _store, laeufe = umgebung
    monkeypatch.setattr(exo_mailboxes, "ist_hybrid", lambda e: False)
    assert health_check.nach_ews_aenderung() == "keine" and laeufe == []


class _Anfrage:
    def __init__(self, daten):
        self._daten = daten

    async def json(self):
        return self._daten


@pytest.mark.parametrize("daten,angestossen", [
    ({"EWS_ONPREM_URL": "https://exchange2.firma.at/EWS/Exchange.asmx"}, True),
    ({"EWS_ONPREM_URL": "https://exchange.firma.at/EWS/Exchange.asmx"}, False),
    ({"LOG_LEVEL": "INFO"}, False),
])
def test_speicherweg_stoesst_nachlauf_an(umgebung, monkeypatch, daten, angestossen):
    from webui.routen import settings as route
    store, _ = umgebung
    aufrufe = []
    monkeypatch.setattr(health_check, "nach_ews_aenderung", lambda: aufrufe.append(
        store.get("EWS_ONPREM_URL")) or "gestartet")
    import log_manager
    monkeypatch.setattr(log_manager, "stufe_anwenden", lambda: None)
    r = asyncio.run(route.settings_save(_Anfrage(daten), user="admin"))
    assert r.status_code == 200
    assert bool(aufrufe) is angestossen
    if angestossen:
        assert aufrufe == [daten["EWS_ONPREM_URL"]], "erst speichern, dann neu prüfen"
