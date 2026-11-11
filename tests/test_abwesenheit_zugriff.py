"""Kein Zugriff auf die Abwesenheit: Ursache und Dauer statt geratener Meldung (v1.11.32).

ANLASS (11.11.2026): Nach dem Aktivieren eines Shared Mailbox meldete die
Übersicht, MailboxSettings.ReadWrite sei nicht per Admin-Consent erteilt. Graph
sagte aber `[RAOP] : Blocked by tenant configured AppOnly AccessPolicy settings`
— das Postfach war korrekt in der Scope-Gruppe, Microsoft hält die Freigabe nur
bis zu 2 Stunden zwischengespeichert. Erst danach ist es ein Problem (Band).
"""
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import abwesenheit  # noqa: E402

RAOP = ('{"error":{"code":"ErrorAccessDenied","message":"Access to OData is disabled: '
        '[RAOP] : Blocked by tenant configured AppOnly AccessPolicy settings."}}')
CONSENT = '{"error":{"code":"ErrorAccessDenied","message":"Access is denied. Check credentials and try again."}}'


@pytest.fixture
def anlage(monkeypatch, tmp_path):
    monkeypatch.setattr(abwesenheit.config, "DATA_DIR", str(tmp_path))
    werte = {"MAILBOX_CONFIG": {}, "GATEWAY_NAME": "Mein Gateway"}
    monkeypatch.setattr(abwesenheit.settings_store, "get", lambda k, d=None: werte.get(k, d))
    monkeypatch.setattr(abwesenheit, "_aktive_postfaecher",
                        lambda cfg: [("rechnungen@firma.at", {}), ("erika@firma.at", {})])
    monkeypatch.setattr(abwesenheit, "zentral_aktiv", lambda: True)
    antworten = {}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            code, text = antworten[url.split("/users/")[1].split("/")[0]]
            return SimpleNamespace(status_code=code, text=text, json=lambda: {"status": "disabled"},
                                   raise_for_status=lambda: None)
    monkeypatch.setattr(abwesenheit.httpx, "AsyncClient", _Client)
    return antworten, tmp_path


def _lesen(upn):
    return asyncio.run(abwesenheit._get_setting(upn, "tok"))[0]


def test_ursache_aus_dem_fehlertext(anlage):
    antworten, _ = anlage
    antworten["rechnungen@firma.at"] = (403, RAOP)
    antworten["erika@firma.at"] = (403, CONSENT)
    assert _lesen("rechnungen@firma.at") == "kein_zugriff"
    assert _lesen("erika@firma.at") == "kein_zugriff"
    stand = {e["upn"]: e["grund"] for e in abwesenheit.zugriff_stand()}
    assert stand == {"rechnungen@firma.at": abwesenheit.RICHTLINIE,
                     "erika@firma.at": abwesenheit.ZUSTIMMUNG}


def test_erfolgreiches_lesen_loescht_den_eintrag(anlage):
    antworten, _ = anlage
    antworten["rechnungen@firma.at"] = (403, RAOP)
    _lesen("rechnungen@firma.at")
    antworten["rechnungen@firma.at"] = (200, "{}")
    assert _lesen("rechnungen@firma.at") == "ok"
    assert abwesenheit.zugriff_stand() == []


def test_in_den_ersten_zwei_stunden_kein_band(anlage):
    antworten, _ = anlage
    antworten["rechnungen@firma.at"] = (403, RAOP)
    _lesen("rechnungen@firma.at")
    _lesen("rechnungen@firma.at")            # wiederholt: „seit“ bleibt stehen
    assert abwesenheit.zugriff_gemeldet() is None


def test_nach_zwei_stunden_band_mit_gruppenname(anlage):
    _, verz = anlage
    seit = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    (verz / "ooo_kein_zugriff.json").write_text(json.dumps({
        "rechnungen@firma.at": {"seit": seit, "grund": abwesenheit.RICHTLINIE},
        "weg@firma.at": {"seit": seit, "grund": abwesenheit.RICHTLINIE}}))
    g = abwesenheit.zugriff_gemeldet()
    assert g["anzahl"] == 1, "nicht mehr aktive Postfächer zählen nicht"
    assert g["postfaecher"] == ["rechnungen@firma.at"] and g["richtlinie"] == 1
    assert g["gruppe"] == "Mein Gateway - App Scope"


def test_band_text(monkeypatch):
    from webui import deps
    import aufgaben
    monkeypatch.setattr(aufgaben, "offene", lambda: [])
    g = deps.templates.env.globals
    for k in ("wartung_gemeldet", "waechter_gemeldet", "bypass_gemeldet", "exo_abgewiesen_gemeldet"):
        monkeypatch.setitem(g, k, lambda: None)
    monkeypatch.setitem(g, "zustellfehler_anzahl", lambda: 0)
    monkeypatch.setitem(g, "lernmodus_rest_min", lambda: 0)
    monkeypatch.setitem(g, "ooo_zugriff_gemeldet", lambda: {
        "anzahl": 1, "postfaecher": ["rechnungen@firma.at"], "gruppe": "Mein Gateway - App Scope",
        "richtlinie": 1, "zustimmung": 0})
    req = SimpleNamespace(state=SimpleNamespace(user_role="admin", angemeldet=True),
                          url=SimpleNamespace(path="/"))
    html = deps.templates.env.get_template("base.html").render(request=req, active="")
    assert 'id="ooo-zugriff-banner"' in html and "rechnungen@firma.at" in html
    assert "Mein Gateway - App Scope" in html and "Admin-Zustimmung" not in html


def test_wiederholte_403_setzt_die_dauer_nicht_zurueck(anlage):
    """Sonst käme das Band nie: Der Abgleich liest alle 10 Minuten erneut."""
    antworten, verz = anlage
    seit = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    (verz / "ooo_kein_zugriff.json").write_text(json.dumps({
        "rechnungen@firma.at": {"seit": seit, "grund": abwesenheit.RICHTLINIE}}))
    antworten["rechnungen@firma.at"] = (403, RAOP)
    _lesen("rechnungen@firma.at")
    assert abwesenheit.zugriff_gemeldet()["anzahl"] == 1


def test_meldung_beim_speichern_nennt_die_ursache(anlage):
    antworten, _ = anlage
    antworten["rechnungen@firma.at"] = (403, RAOP)
    _lesen("rechnungen@firma.at")
    assert "Zugriffsrichtlinie" in abwesenheit.zugriff_meldung("rechnungen@firma.at")
    antworten["erika@firma.at"] = (403, CONSENT)
    _lesen("erika@firma.at")
    assert "Admin-Zustimmung" in abwesenheit.zugriff_meldung("erika@firma.at")
