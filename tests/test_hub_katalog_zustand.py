"""Ein leerer Anbieterkatalog sagt, ob er leer IST oder nur nicht geladen wurde.

ANLASS (24.08.2026)
-------------------
Im Protokoll der Produktions-VM standen drei fehlgeschlagene Katalogabrufe
(ein Zertifikatsfehler, zwei Zeitüberschreitungen). Dass ein Fehlschlag den
letzten bekannten Stand stehen lässt, ist richtig — nach einem Neustart gibt es
diesen Stand aber nicht, und die Anbindungsseite zeigte ihre Anbieterbox dann
gar nicht erst an („Box nur zeigen, wenn es etwas zu zeigen gibt"). Der
Betreiber sah keine Störung, sondern eine Welt ohne Zertifizierungsstellen.

Dieselbe Klasse war hier schon einmal aufgetreten: `ca_backends/registry.py`
hält fest, dass am 19.08.2026 „die Hälfte der Zertifizierungsstellen fehlte,
ohne dass etwas kaputt war". Behoben wurde damals der Abbruch — nicht die
Unsichtbarkeit. Genau das ist CLAUDE.md Regel 8: Wo die Null die Meldung ist,
muss die Zeile erscheinen.
"""
import asyncio
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import hub_catalog  # noqa: E402


@pytest.fixture(autouse=True)
def frischer_katalog(monkeypatch):
    """Jeder Test beginnt mit unbeschriebenem Zwischenspeicher."""
    monkeypatch.setattr(hub_catalog, "_cache",
                        {"ts": 0.0, "providers": [], "currency": "EUR", "vat_percent": 19})
    monkeypatch.setattr(hub_catalog, "_stand",
                        {"letzter_erfolg": 0.0, "fehler": "", "fehler_zeit": 0.0})


def _hub(monkeypatch, *, registriert=True, antwort=None, wirft=None):
    class _Client:
        @staticmethod
        def cert_is_registered():
            return registriert

        @staticmethod
        async def cert_get_catalog():
            if wirft:
                raise wirft
            return antwort

    monkeypatch.setitem(sys.modules, "hub_client", _Client)


def test_nie_geladen_wird_als_solches_gemeldet(monkeypatch):
    _hub(monkeypatch, wirft=RuntimeError("[SSL: CERTIFICATE_VERIFY_FAILED]"))
    asyncio.run(hub_catalog.refresh(force=True))

    z = hub_catalog.zustand()
    assert z["nie_geladen"] is True, (
        "Keine Anbieter und kein früherer Erfolg — das muss als Störung "
        "erkennbar sein, nicht als leeres Angebot.")
    assert "CERTIFICATE_VERIFY_FAILED" in z["fehler"]
    assert z["fehler_zeit"] and z["letzter_erfolg"] is None


def test_nach_erfolg_ist_nie_geladen_falsch(monkeypatch):
    _hub(monkeypatch, antwort={"ok": True, "providers": [{"id": "certum"}]})
    asyncio.run(hub_catalog.refresh(force=True))

    z = hub_catalog.zustand()
    assert z["nie_geladen"] is False
    assert z["anbieter"] == 1
    assert z["fehler"] is None and z["letzter_erfolg"]


def test_stoerung_nach_erfolg_behaelt_den_alten_stand(monkeypatch):
    """Der wichtigere Fall: Es gab Anbieter, der Abruf scheitert — dann ist die
    Liste NICHT leer, und `nie_geladen` wäre eine Falschmeldung."""
    _hub(monkeypatch, antwort={"ok": True, "providers": [{"id": "certum"}]})
    asyncio.run(hub_catalog.refresh(force=True))
    _hub(monkeypatch, wirft=RuntimeError("HTTP 504"))
    asyncio.run(hub_catalog.refresh(force=True))

    z = hub_catalog.zustand()
    assert z["anbieter"] == 1, "der letzte bekannte Stand muss stehen bleiben"
    assert z["nie_geladen"] is False
    assert "504" in z["fehler"], "die Störung wird trotzdem vermerkt"
    assert z["letzter_erfolg"], "und der frühere Erfolg bleibt bekannt"


def test_fachlicher_fehlschlag_zaehlt_wie_ein_ausfall(monkeypatch):
    """`{"ok": False}` ist kein Ausnahmefall im Code, aber einer für den Betrieb.

    Ohne diesen Zweig bliebe `fehler` leer, und die Anzeige meldete eine
    Störung ohne Grund — schlimmer als keine Meldung, weil sie nach einem
    Anzeigefehler aussieht.
    """
    _hub(monkeypatch, antwort={"ok": False, "error": "Gateway nicht freigeschaltet"})
    asyncio.run(hub_catalog.refresh(force=True))

    z = hub_catalog.zustand()
    assert z["fehler"] == "Gateway nicht freigeschaltet"
    assert z["nie_geladen"] is True


def test_erfolg_loescht_die_alte_stoerung(monkeypatch):
    """Sonst bliebe eine behobene Störung für immer angezeigt."""
    _hub(monkeypatch, wirft=RuntimeError("HTTP 504"))
    asyncio.run(hub_catalog.refresh(force=True))
    _hub(monkeypatch, antwort={"ok": True, "providers": [{"id": "certum"}]})
    asyncio.run(hub_catalog.refresh(force=True))

    assert hub_catalog.zustand()["fehler"] is None
