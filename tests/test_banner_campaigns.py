"""Banner-Kampagnen: Zeitfenster, Gruppen-Match, Vorrang, Verwaltung.

Kernverträge:
  * Nur eine aktive (im Fenster, aktiviert) UND passende (Gruppe) Kampagne liefert
    ihr Banner; sonst None → der Handler nimmt das übliche Banner.
  * Offene Grenzen (Start/Ende leer) bedeuten „läuft schon"/„läuft weiter".
  * Erste passende Kampagne in Listenreihenfolge gewinnt.
  * Verwaltung validiert VOR dem Schreiben und schreibt via settings_store.
"""
from datetime import datetime, timedelta, timezone

import pytest

import banner_campaigns as bc


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)   # fester Bezug NUR im Test


def _kmp(**kw):
    base = {"id": "kmp_aaaaaa", "name": "K", "banner": "sommer",
            "start": "", "end": "", "group": "", "enabled": True}
    base.update(kw)
    return base


# ── aktive_kampagne ──────────────────────────────────────────────────────────
def test_offenes_fenster_ist_aktiv():
    assert bc.aktive_kampagne([_kmp()], NOW, None, {}) == "sommer"


def test_innerhalb_des_fensters_aktiv():
    c = _kmp(start=_iso(NOW - timedelta(days=1)), end=_iso(NOW + timedelta(days=1)))
    assert bc.aktive_kampagne([c], NOW, None, {}) == "sommer"


def test_vor_start_nicht_aktiv():
    c = _kmp(start=_iso(NOW + timedelta(hours=1)))
    assert bc.aktive_kampagne([c], NOW, None, {}) is None


def test_nach_ende_nicht_aktiv():
    c = _kmp(end=_iso(NOW - timedelta(hours=1)))
    assert bc.aktive_kampagne([c], NOW, None, {}) is None


def test_deaktiviert_nicht_aktiv():
    assert bc.aktive_kampagne([_kmp(enabled=False)], NOW, None, {}) is None


def test_leeres_banner_wird_uebersprungen():
    assert bc.aktive_kampagne([_kmp(banner="  ")], NOW, None, {}) is None


def test_gruppe_leer_gilt_fuer_alle():
    assert bc.aktive_kampagne([_kmp(group="")], NOW, "guid-1", {}) == "sommer"


def test_gruppe_gesetzt_nur_fuer_mitglieder():
    groups = {"Vertrieb": ["guid-1", "guid-2"]}
    c = _kmp(group="Vertrieb")
    assert bc.aktive_kampagne([c], NOW, "guid-1", groups) == "sommer"
    assert bc.aktive_kampagne([c], NOW, "guid-9", groups) is None
    assert bc.aktive_kampagne([c], NOW, None, groups) is None      # kein Key → kein Match


def test_erste_passende_gewinnt():
    a = _kmp(id="kmp_a1a1a1", banner="banner-a")
    b = _kmp(id="kmp_b2b2b2", banner="banner-b")
    assert bc.aktive_kampagne([a, b], NOW, None, {}) == "banner-a"


def test_inaktive_wird_uebersprungen_naechste_greift():
    a = _kmp(id="kmp_a1a1a1", banner="banner-a", end=_iso(NOW - timedelta(days=1)))
    b = _kmp(id="kmp_b2b2b2", banner="banner-b")
    assert bc.aktive_kampagne([a, b], NOW, None, {}) == "banner-b"


def test_leere_liste_none():
    assert bc.aktive_kampagne([], NOW, None, {}) is None
    assert bc.aktive_kampagne(None, NOW, None, {}) is None


# ── parse_zeit ───────────────────────────────────────────────────────────────
def test_parse_zeit_akzeptiert_z_und_offset():
    z = bc.parse_zeit("2026-07-15T12:00:00Z")
    off = bc.parse_zeit("2026-07-15T12:00:00+00:00")
    assert z == off == NOW


def test_parse_zeit_naiv_wird_utc():
    dt = bc.parse_zeit("2026-07-15T12:00:00")
    assert dt.tzinfo is not None and dt == NOW


def test_parse_zeit_leer_und_muell_none():
    assert bc.parse_zeit("") is None
    assert bc.parse_zeit(None) is None
    assert bc.parse_zeit("übermorgen") is None


# ── status ───────────────────────────────────────────────────────────────────
def test_status_stufen():
    assert bc.status(_kmp(enabled=False), NOW) == "aus"
    assert bc.status(_kmp(), NOW) == "aktiv"
    assert bc.status(_kmp(start=_iso(NOW + timedelta(days=1))), NOW) == "geplant"
    assert bc.status(_kmp(end=_iso(NOW - timedelta(days=1))), NOW) == "abgelaufen"


# ── Verwaltung ───────────────────────────────────────────────────────────────
@pytest.fixture
def store(monkeypatch):
    daten = {}
    monkeypatch.setattr(bc.settings_store, "get", lambda k, *a, **kw: daten.get(k))
    monkeypatch.setattr(bc.settings_store, "update", lambda d: daten.update(d))
    return daten


def test_speichern_legt_an_mit_id(store):
    rec = bc.speichern({"name": "Sommer", "banner": "sommer-banner",
                        "start": _iso(NOW), "end": _iso(NOW + timedelta(days=30))})
    assert rec["id"].startswith("kmp_")
    assert store["BANNER_CAMPAIGNS"][0]["name"] == "Sommer"


def test_speichern_aendert_bestehende(store):
    r1 = bc.speichern({"name": "A", "banner": "b"})
    bc.speichern({"id": r1["id"], "name": "A2", "banner": "b2"})
    campaigns = store["BANNER_CAMPAIGNS"]
    assert len(campaigns) == 1 and campaigns[0]["name"] == "A2"


def test_speichern_lehnt_muell_ab(store):
    with pytest.raises(ValueError):
        bc.speichern({"name": "", "banner": "b"})
    with pytest.raises(ValueError):
        bc.speichern({"name": "n", "banner": ""})
    with pytest.raises(ValueError):
        bc.speichern({"name": "n", "banner": "b", "start": "kaputt"})
    with pytest.raises(ValueError):
        bc.speichern({"name": "n", "banner": "b",
                      "start": _iso(NOW), "end": _iso(NOW - timedelta(days=1))})
    assert "BANNER_CAMPAIGNS" not in store          # nichts geschrieben


def test_loeschen(store):
    r = bc.speichern({"name": "A", "banner": "b"})
    assert bc.loeschen(r["id"]) is True
    assert store["BANNER_CAMPAIGNS"] == []
    assert bc.loeschen(r["id"]) is False            # zweites Mal: weg
