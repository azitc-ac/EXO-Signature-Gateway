"""Warnmail bei knappem Datenvolumen — und dieselben Schwellen wie die Kachel.

Bis v1.12.179 färbte nur die Kachel der Übersicht die Zahl (75/90 im
JavaScript); eine Benachrichtigung gab es nicht. Jetzt plattenplatz.py:
≥ KRIT_PCT → Mail an die Verwaltung, höchstens einmal pro Tag; Entwarnung erst
unter ENTWARNUNG_PCT (Hysterese).
"""
import re
from pathlib import Path

import pytest

import plattenplatz

WURZEL = Path(__file__).resolve().parents[1]


@pytest.fixture
def umgebung(tmp_path, monkeypatch):
    import config
    import notification
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    gesendet = []
    ergebnis = {"ok": True}

    def warnung(m):
        gesendet.append(("warnung", m["pct"]))
        return ergebnis["ok"]

    def entwarnung(m):
        gesendet.append(("entwarnung", m["pct"]))
        return ergebnis["ok"]
    monkeypatch.setattr(notification, "send_plattenplatz_warnung", warnung)
    monkeypatch.setattr(notification, "send_plattenplatz_entwarnung", entwarnung)
    return gesendet, ergebnis


def _m(pct):
    return {"total_mb": 1000.0, "used_mb": pct * 10, "free_mb": 1000 - pct * 10, "pct": pct, "pfad": "/app/data"}


def test_ausloesen_einmal_pro_tag_und_entwarnung_mit_hysterese(umgebung):
    gesendet, _ = umgebung
    assert plattenplatz.pruefen(_m(91.0), heute="2026-01-02") == "warnung"
    assert plattenplatz.pruefen(_m(95.0), heute="2026-01-02") == ""        # selber Tag: nicht wieder
    assert plattenplatz.pruefen(_m(92.0), heute="2026-01-03") == "warnung"  # nächster Tag, noch voll
    assert plattenplatz.pruefen(_m(87.0), heute="2026-01-03") == ""        # Hysterese: noch keine Entwarnung
    assert plattenplatz.pruefen(_m(84.9), heute="2026-01-03") == "entwarnung"
    assert plattenplatz.pruefen(_m(80.0), heute="2026-01-04") == ""        # Entwarnung nur einmal
    assert gesendet == [("warnung", 91.0), ("warnung", 92.0), ("entwarnung", 84.9)]


def test_genau_an_der_schwelle(umgebung):
    gesendet, _ = umgebung
    assert plattenplatz.pruefen(_m(plattenplatz.KRIT_PCT - 0.1), heute="2026-01-02") == ""
    assert plattenplatz.pruefen(_m(float(plattenplatz.KRIT_PCT)), heute="2026-01-02") == "warnung"
    assert plattenplatz.pruefen(_m(float(plattenplatz.ENTWARNUNG_PCT)), heute="2026-01-02") == ""


def test_ohne_vorherige_warnung_keine_entwarnung(umgebung):
    gesendet, _ = umgebung
    assert plattenplatz.pruefen(_m(40.0), heute="2026-01-02") == "" and gesendet == []


def test_gescheiterte_mail_wird_nicht_gemerkt(umgebung):
    gesendet, ergebnis = umgebung
    ergebnis["ok"] = False
    assert plattenplatz.pruefen(_m(93.0), heute="2026-01-02") == ""
    ergebnis["ok"] = True
    assert plattenplatz.pruefen(_m(93.0), heute="2026-01-02") == "warnung"  # noch am selben Tag nachgeholt
    assert len(gesendet) == 2


def test_misst_das_datenverzeichnis(umgebung, tmp_path):
    m = plattenplatz.messen()
    assert m and m["pfad"] == str(tmp_path) and 0 <= m["pct"] <= 100 and m["total_mb"] > 0


def test_kachel_nutzt_dieselben_schwellen():
    """Keine eigenen Zahlen mehr im JavaScript der Übersicht."""
    dash = (WURZEL / "app/webui/templates/dashboard.html").read_text(encoding="utf-8")
    zeile = next(z for z in dash.splitlines() if "const diskClass" in z)
    assert "d.disk_krit_pct" in zeile and "d.disk_warn_pct" in zeile
    assert not re.search(r"disk_pct\s*>=\s*\d", zeile), zeile
    api = (WURZEL / "app/webui/routen/aktualisierung.py").read_text(encoding="utf-8")
    assert '"disk_krit_pct":    plattenplatz.KRIT_PCT' in api and "plattenplatz.messen()" in api
    assert "shutil.disk_usage" not in api


def test_hysterese_liegt_unter_der_schwelle():
    assert plattenplatz.WARN_PCT < plattenplatz.ENTWARNUNG_PCT < plattenplatz.KRIT_PCT


def test_scheduler_ruft_die_pruefung():
    src = (WURZEL / "app/scheduler.py").read_text(encoding="utf-8")
    assert "plattenplatz.pruefen()" in src and "_PLATTENPLATZ_INTERVAL" in src
    schleife = src.split("def _loop() -> None:", 1)[1]
    assert "_plattenplatz()" in schleife


def test_mails_folgen_dem_hauptschalter_und_nennen_die_werte(monkeypatch):
    import notification
    import settings_store
    gesendet = []
    monkeypatch.setattr(notification, "_graph_send", lambda to, betreff, html: gesendet.append((to, betreff, html)) or True)
    monkeypatch.setattr(notification, "_get_notify_to", lambda: "it@firma.at")
    werte = {"NOTIFICATIONS_ENABLED": False}
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))
    assert notification.send_plattenplatz_warnung(_m(93.4)) is False and gesendet == []
    werte["NOTIFICATIONS_ENABLED"] = True
    assert notification.send_plattenplatz_warnung(_m(93.4)) is True
    to, betreff, html = gesendet[-1]
    assert to == "it@firma.at" and "93 %" in betreff and "93.4 %" in html
    assert f"Ab {plattenplatz.KRIT_PCT} %" in html and f"unter {plattenplatz.ENTWARNUNG_PCT} %" in html
    assert notification.send_plattenplatz_entwarnung(_m(70.0)) is True and "Entwarnung" in gesendet[-1][1]
