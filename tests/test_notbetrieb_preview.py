"""Notbetrieb als PREVIEW: Schalter NOTBETRIEB_AKTIV aus (Vorgabe) → nichts davon
ist aktiv. Keine Route antwortet, kein Abgleich läuft, der Mailfluss bleibt
unberührt.

Die Routen werden nicht einzeln aufgezählt, sondern aus den beiden Routern
gelesen: Eine künftige Route ohne eigene Prüfung fällt hier auf, solange sie
im selben Router steht. Gegenprobe: Ohne die Router-Sperren
(`_nur_wenn_an`/`_nur_wenn_sichtbar`) antworten mehrere Routen nicht mehr mit 404.
"""
from __future__ import annotations

import re

import pytest

import config
import settings_store


@pytest.fixture
def aus(monkeypatch, tmp_path):
    werte = {"NOTBETRIEB_AKTIV": False, "SSO_SESSION_SECRET": "s" * 64,
             "NOTBETRIEB_AUSGERUFEN": True,     # liegengebliebener Schalter darf nichts bewirken
             "MAILBOX_CONFIG": {"a@firma.at": {"sig": True}}}
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))   # keine Kopie vorhanden
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: werte.get(k, d))
    return werte


def _admin(monkeypatch):
    from starlette.testclient import TestClient
    import sso
    from webui import app as wa
    monkeypatch.setattr(sso, "_get_secret", lambda: "testgeheimnis-preview")
    monkeypatch.setattr(sso, "_noch_gueltig", lambda p: True)
    c = TestClient(wa.app, base_url="https://testserver", follow_redirects=False)
    c.cookies.set(sso.SESSION_COOKIE, sso.create_session_cookie("x@example.org", role=sso.ROLE_ADMIN))
    return c


def _routen():
    from webui.routen import notbetrieb as r_nb, notfall as r_nf
    for router in (r_nf.router, r_nb.router):
        for route in router.routes:
            pfad = re.sub(r"\{[^}]+\}", "x", route.path)
            for methode in sorted(route.methods - {"HEAD"}):
                yield methode, pfad


def test_es_gibt_routen_zu_pruefen():
    liste = list(_routen())
    assert len(liste) > 40, liste           # sonst prüft der Test nichts
    assert ("GET", "/notfall") in liste and ("POST", "/api/notbetrieb/loeschen") in liste


def test_aus_antwortet_keine_route(aus, monkeypatch):
    c = _admin(monkeypatch)
    offen = []
    for methode, pfad in _routen():
        r = c.request(methode, pfad, json={})
        erwartet = {303} if (methode, pfad) == ("GET", "/notbetrieb") else {404}
        if r.status_code not in erwartet:
            offen.append(f"{methode} {pfad} → {r.status_code}")
    assert not offen, "Bei Preview aus erreichbar:\n" + "\n".join(offen)


def test_aus_kein_menuepunkt(aus, monkeypatch):
    html = _admin(monkeypatch).get("/settings").text
    assert 'href="/notbetrieb"' not in html


def test_an_mit_menuepunkt_und_preview_kennzeichnung(aus, monkeypatch):
    """Gegenstück: Die Kennzeichnung erscheint, sobald der Schalter an ist."""
    aus["NOTBETRIEB_AKTIV"] = True
    html = _admin(monkeypatch).get("/settings").text
    i = html.index('href="/notbetrieb"')
    assert "PREVIEW" in html[i:i + 600]


def test_aus_kein_abgleich_im_scheduler(aus, monkeypatch):
    import notbetrieb
    import notbetrieb_rechte
    import scheduler

    def verboten(*a, **k):
        raise AssertionError("Notbetrieb-Abgleich trotz Preview aus")
    monkeypatch.setattr(notbetrieb, "abgleich_alle", verboten)
    monkeypatch.setattr(notbetrieb, "profile_sichern", verboten)
    monkeypatch.setattr(notbetrieb_rechte, "abgleichen", verboten)
    warnungen = []
    monkeypatch.setattr(scheduler.log, "warning", lambda *a, **k: warnungen.append(a))
    scheduler._sync_notbetrieb()
    assert not warnungen, warnungen


def test_aus_mailfluss_unberuehrt(aus):
    """Liegengebliebenes AUSGERUFEN ohne Schalter: keine Umleitung, keine
    Eingangsprüfung, keine Teams-App."""
    import notbetrieb
    import notbetrieb_eingang
    import reinject
    import teams_katalog
    assert notbetrieb.ausgerufen() is False
    assert reinject.umgeleitet() is False
    assert notbetrieb_eingang.quelle("203.0.113.5", object()) in (None, "", False)
    assert teams_katalog.aktiv("notfall") is False
    import abnahme
    assert abnahme._notbetrieb_lokal()["zustand"] == abnahme.ENTFAELLT
    assert teams_katalog.pruefung("notfall")["zustand"] == abnahme.ENTFAELLT


def test_schalter_ist_als_preview_gekennzeichnet():
    from pathlib import Path
    setup = (Path(__file__).resolve().parent.parent / "app/webui/templates/setup.html").read_text(encoding="utf-8")
    i = setup.index("EXO-Ausfallschutz vorbereiten")
    assert "PREVIEW" in setup[i:i + 400]
    assert settings_store.DEFAULTS["NOTBETRIEB_AKTIV"] is False
