"""Einbau der täglichen Direktversand-Probe (Modul: direktversand_probe.py).

Die Probe selbst prüft test_direktversand_probe.py. Hier: dass sie im Betrieb
überhaupt läuft und sichtbar ist — Scheduler (Ankunft in JEDEM Takt geprüft),
Schnittstelle, Tagesbericht (Zeile IMMER bei eingeschaltetem Direktversand),
Aufgabe und Abnahme (dieselbe Bewertung, „gesendet“ ist kein ok).
"""
from __future__ import annotations

from pathlib import Path

import pytest

import abnahme
import aufgaben
import direktversand
import direktversand_probe as dp
import notification
import scheduler

VORLAGE = (Path(__file__).resolve().parent.parent / "app/webui/templates/settings_routing.html")


@pytest.fixture
def zustand(monkeypatch):
    z = {"aktiv": True, "postfach": "probe@firma.at", "stand": {}, "aufrufe": []}
    monkeypatch.setattr(direktversand, "aktiv", lambda: z["aktiv"])
    monkeypatch.setattr(dp, "postfach", lambda: z["postfach"])
    monkeypatch.setattr(dp, "stand", lambda: dict(z["stand"]))
    monkeypatch.setattr(dp, "pruefen", lambda: z["aufrufe"].append("pruefen") or z["stand"])
    monkeypatch.setattr(dp, "senden", lambda: z["aufrufe"].append("senden") or {"zustand": "gesendet"})
    return z


# ── Scheduler ────────────────────────────────────────────────────────────────

def test_scheduler_prueft_in_jedem_takt_und_sendet_nur_wenn_faellig(zustand, monkeypatch):
    monkeypatch.setattr(dp, "faellig", lambda: False)
    scheduler._direktversand_probe()
    scheduler._direktversand_probe()
    assert zustand["aufrufe"] == ["pruefen", "pruefen"], \
        "pruefen muss in jedem Takt laufen — sonst wird die Ankunft erst am Folgetag erkannt"
    monkeypatch.setattr(dp, "faellig", lambda: True)
    scheduler._direktversand_probe()
    assert zustand["aufrufe"][-2:] == ["pruefen", "senden"]


def test_scheduler_ruht_ohne_direktversand_oder_postfach(zustand, monkeypatch):
    monkeypatch.setattr(dp, "faellig", lambda: True)
    zustand["aktiv"] = False
    scheduler._direktversand_probe()
    zustand["aktiv"], zustand["postfach"] = True, ""
    scheduler._direktversand_probe()
    assert zustand["aufrufe"] == []


def test_scheduler_bricht_bei_fehler_nicht_ab(zustand, monkeypatch):
    monkeypatch.setattr(dp, "pruefen", lambda: (_ for _ in ()).throw(OSError("Graph weg")))
    scheduler._direktversand_probe()              # darf nicht werfen


def test_scheduler_schleife_ruft_die_probe():
    import inspect
    quelle = inspect.getsource(scheduler._loop)
    assert "_direktversand_probe()" in quelle and "_PROBE_INTERVAL" in quelle
    assert scheduler._PROBE_INTERVAL <= 15 * 60


# ── Tagesbericht ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("stand, ampel_text", [
    ({}, "noch keine Probe"),
    ({"zustand": "gesendet", "ts": "2999-01-01T00:00:00+00:00"}, "gesendet"),
])
def test_tagesbericht_zeigt_die_zeile_auch_ohne_ergebnis(zustand, stand, ampel_text):
    zustand["stand"] = stand
    zeile = notification._direktversand_probe_row()
    assert "Direktversand-Probe" in zeile and ampel_text in zeile
    assert "#e67e22" in zeile                                       # Warnung, nicht grün


def test_tagesbericht_ohne_postfach_meldet_es(zustand):
    zustand["postfach"] = ""
    assert "kein Probe-Postfach" in notification._direktversand_probe_row()


def test_tagesbericht_schweigt_ohne_direktversand(zustand):
    zustand["aktiv"] = False
    assert notification._direktversand_probe_row() == ""


def test_tagesbericht_baut_die_zeile_ein():
    import inspect
    assert "_direktversand_probe_row()" in inspect.getsource(notification.send_daily_report)


# ── Aufgabe und Abnahme ──────────────────────────────────────────────────────

def test_aufgabe_ist_registriert():
    a = next(a for a in aufgaben.AUFGABEN if a.kennung == "direktversand-probe")
    assert a.pruefung is abnahme._direktversand_probe
    assert a.link.startswith("/settings/routing#") and a.seit
    assert abnahme._direktversand_probe in abnahme.PRUEFUNGEN
    assert 'id="' + a.link.split("#", 1)[1] + '"' in VORLAGE.read_text(encoding="utf-8")


def test_abnahme_offen_ohne_postfach(zustand):
    zustand["postfach"] = ""
    assert abnahme._direktversand_probe()["zustand"] == abnahme.OFFEN


def test_abnahme_ok_ohne_direktversand(zustand):
    zustand["aktiv"] = False
    assert abnahme._direktversand_probe()["zustand"] == abnahme.OK


@pytest.mark.parametrize("ampel, text, erwartet", [
    ("ok", "angekommen", "ok"),
    ("warnung", "gesendet, Ankunft noch offen", "unbekannt"),
    ("warnung", "angekommen … — im Junk-Ordner", "offen"),
    ("fehler", "nicht angekommen", "offen"),
])
def test_abnahme_nach_bewertung(zustand, monkeypatch, ampel, text, erwartet):
    monkeypatch.setattr(dp, "bewertung", lambda st=None: (ampel, text))
    assert abnahme._direktversand_probe()["zustand"] == erwartet


# ── Schnittstelle ────────────────────────────────────────────────────────────

@pytest.fixture
def client(zustand):
    from fastapi.testclient import TestClient
    from webui import deps
    from webui.app import app
    app.dependency_overrides[deps._require_admin] = lambda: "admin"
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(deps._require_admin, None)


def test_probe_jetzt_409_ohne_direktversand(client, zustand):
    zustand["aktiv"] = False
    r = client.post("/api/direkt/probe")
    assert r.status_code == 409 and "senden" not in zustand["aufrufe"]


def test_probe_jetzt_400_ohne_postfach(client, zustand):
    zustand["postfach"] = ""
    r = client.post("/api/direkt/probe")
    assert r.status_code == 400 and "senden" not in zustand["aufrufe"]


def test_probe_jetzt_sendet(client, zustand):
    r = client.post("/api/direkt/probe")
    assert r.status_code == 200 and zustand["aufrufe"] == ["senden"]
    assert r.json()["ampel"] == "warnung"                           # gesendet ist kein ok


def test_stand_prueft_die_ankunft(client, zustand):
    r = client.get("/api/direkt/probe")
    assert r.status_code == 200 and zustand["aufrufe"] == ["pruefen"]


# ── Oberfläche ───────────────────────────────────────────────────────────────

def test_ampel_hat_helle_und_dunkle_regel():
    text = VORLAGE.read_text(encoding="utf-8")
    for wert in ("ok", "warnung", "fehler"):
        assert f'.probe-ampel[data-ampel="{wert}"]' in text
        assert f'[data-theme="dark"] .probe-ampel[data-ampel="{wert}"]' in text
    assert "DIREKTVERSAND_PROBE_POSTFACH" in text
