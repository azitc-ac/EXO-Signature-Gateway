"""Betriebszustände als Band auf jeder Seite (v1.10.86).

Wartungsmodus, Zustellfehler und ein funktionsunfähiger Bypass-Wächter standen
bis dahin nur auf der Übersicht. Geprüft:
1. die Bänder kommen aus base.html (Jinja-Globals), nicht mehr aus der Übersicht,
2. nur Admins sehen sie — die Ziele liegen unter „Erweitert“,
3. der Wächter-Zustand wird richtig eingeordnet (still / ohne Exchange / gesund),
   und ein aktiver Bypass erscheint nicht doppelt.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import aufgaben
from webui import deps

VORLAGEN = Path(__file__).resolve().parent.parent / "app" / "webui" / "templates"


def _seite(monkeypatch, rolle="admin", wartung=None, fehler=0, waechter=None):
    g = deps.templates.env.globals
    monkeypatch.setattr(aufgaben, "offene", lambda: [])
    monkeypatch.setitem(g, "wartung_gemeldet", lambda: wartung)
    monkeypatch.setitem(g, "zustellfehler_anzahl", lambda: fehler)
    monkeypatch.setitem(g, "waechter_gemeldet", lambda: waechter)
    req = SimpleNamespace(state=SimpleNamespace(user_role=rolle), url=SimpleNamespace(path="/"))
    return deps.templates.env.get_template("base.html").render(request=req, active="")


def test_ohne_befund_kein_band(monkeypatch):
    html = _seite(monkeypatch)
    for kennung in ("wartung-banner", "zustellfehler-banner", "waechter-banner"):
        assert f'id="{kennung}"' not in html


def test_baender_erscheinen_mit_zahlen_und_ziel(monkeypatch):
    html = _seite(monkeypatch, wartung={"zurueck": 3}, fehler=1,
                  waechter={"art": "still", "zuletzt": "noch nie", "fehler": ""})
    assert 'id="wartung-banner"' in html and "3 Mails zurückgehalten" in html
    assert 'id="zustellfehler-banner"' in html and "1 Mail konnte" in html
    assert 'id="waechter-banner"' in html and "meldet sich nicht" in html
    assert html.count('href="/advanced#maintenance"') >= 2 and 'href="/advanced#watchdog"' in html


def test_farben_zustellfehler_und_waechter_rot_wartung_gelb(monkeypatch):
    html = _seite(monkeypatch, wartung={"zurueck": 0}, fehler=2,
                  waechter={"art": "exo", "zuletzt": "x", "fehler": "403"})

    def stil(kennung):
        i = html.index(f'id="{kennung}"')
        return html[i:html.index(">", i)]
    assert "#fffbeb" in stil("wartung-banner")
    assert "#fef2f2" in stil("zustellfehler-banner") and "#fef2f2" in stil("waechter-banner")


def test_waechter_fehlertext_maskiert(monkeypatch):
    html = _seite(monkeypatch, waechter={"art": "exo", "zuletzt": "x", "fehler": "<img src=x>"})
    assert "<img src=x>" not in html and "&lt;img" in html


def test_bearbeiter_sieht_keine_betriebsbaender(monkeypatch):
    html = _seite(monkeypatch, rolle="editor", wartung={"zurueck": 1}, fehler=1,
                  waechter={"art": "still", "zuletzt": "x", "fehler": ""})
    assert "wartung-banner" not in html and "zustellfehler-banner" not in html \
        and "waechter-banner" not in html


def test_uebersicht_hat_keinen_eigenen_weg_mehr():
    """Ein zweiter Weg neben base.html — genau das wurde abgelöst."""
    s = (VORLAGEN / "dashboard.html").read_text("utf-8")
    for alt in ("maintenance-banner", "delivery-failed-banner", "watchdog-dash-banner",
                "fairuse-banner", "password_change_needed"):
        assert alt not in s, alt
    assert "fairuse-banner" not in (VORLAGEN / "mailboxes.html").read_text("utf-8")


# ── Einordnung des Wächters ──────────────────────────────────────────────────

def _iso(min_her):
    return (datetime.now(timezone.utc) - timedelta(minutes=min_her)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def waechter(monkeypatch):
    import settings_store
    import waechter_register
    import waechter_state
    zustand = {"an": True, "liste": [], "bypass": None}
    monkeypatch.setattr(settings_store, "get",
                        lambda k, d=None: zustand["an"] if k == "WATCHDOG_ENABLED" else d)
    monkeypatch.setattr(waechter_register, "liste", lambda: zustand["liste"])
    monkeypatch.setattr(waechter_state, "lesen", lambda: {})
    monkeypatch.setattr(deps, "_bypass_gemeldet", lambda: zustand["bypass"])
    return zustand


def test_waechter_frisch_gesehen_ist_gesund(waechter):
    waechter["liste"] = [{"last_seen": _iso(3)}]
    assert deps._waechter_gemeldet() is None


def test_waechter_verstummt(waechter):
    waechter["liste"] = [{"last_seen": _iso(deps.WAECHTER_STILL_MIN + 5)}]
    assert deps._waechter_gemeldet()["art"] == "still"


def test_waechter_nie_gesehen(waechter):
    assert deps._waechter_gemeldet() == {"art": "still", "zuletzt": "noch nie", "fehler": ""}


def test_waechter_ohne_exchange(waechter):
    waechter["liste"] = [{"last_seen": _iso(1), "exo_error": "403 Transport Rules"}]
    assert deps._waechter_gemeldet()["art"] == "exo"


def test_aktiver_bypass_nicht_doppelt(waechter):
    waechter["liste"] = [{"last_seen": _iso(60)}]
    waechter["bypass"] = {"seit": "x"}
    assert deps._waechter_gemeldet() is None


def test_waechter_nicht_eingerichtet(waechter):
    waechter["an"] = False
    assert deps._waechter_gemeldet() is None


def test_wartung_zaehlt_zustellfehler_nicht_mit(monkeypatch):
    import held_mails
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: k == "MAINTENANCE_MODE" or d)
    monkeypatch.setattr(held_mails, "count",
                        lambda reason=None: 2 if reason == "delivery_failed" else 7)
    assert deps._wartung_gemeldet() == {"zurueck": 5}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: d)
    assert deps._wartung_gemeldet() is None
