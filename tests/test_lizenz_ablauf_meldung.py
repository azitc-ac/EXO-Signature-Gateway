"""Lizenz-Ablauf-Meldungen: vorher erinnern, danach schweigen.

Eine ablaufende Lizenz wird rechtzeitig gemeldet (damit man verlängern/neu kaufen
kann). Ist sie ABGELAUFEN, wird gar nicht mehr benachrichtigt — ein Fair-Use-
Produkt nörgelt einen Dauerzustand nicht täglich per Mail an, unabhängig von der
Freigrenze.

Der Test schlägt fehl, wenn man die Abgelaufen-Meldung wieder einführt.
"""
from __future__ import annotations

import license
import notification
import scheduler
import settings_store


def _patch(monkeypatch, store, gesendet, status):
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(settings_store, "force_update", lambda patch: store.update(patch))
    monkeypatch.setattr(license, "status", lambda: status)
    monkeypatch.setattr(notification, "send_license_expiry_warning",
                        lambda st, days: gesendet.append(days) or True)
    scheduler._cert_alerts_sent.clear()


def test_abgelaufen_meldet_nicht(monkeypatch):
    """status() liefert für eine abgelaufene Lizenz kein `expires` — es darf KEINE
    Mail rausgehen, auch nicht nach einem Neustart."""
    store = {"LICENSE_KEY": "KEY"}
    gesendet: list = []
    _patch(monkeypatch, store, gesendet,
           {"licensed": False, "reason": "Lizenz am 2026-09-17 abgelaufen"})

    scheduler._check_license_expiry()
    scheduler._cert_alerts_sent.clear()   # Neustart simulieren
    scheduler._check_license_expiry()
    assert gesendet == [], "abgelaufene Lizenz darf nicht (wiederholt) gemeldet werden"


def test_keine_lizenz_meldet_nicht(monkeypatch):
    store = {}
    gesendet: list = []
    _patch(monkeypatch, store, gesendet, {"licensed": False, "reason": ""})
    scheduler._check_license_expiry()
    assert gesendet == []


def test_bald_ablaufend_erinnert(monkeypatch):
    """Gültige Lizenz, die in Kürze abläuft → genau eine Erinnerung an der
    Schwelle; nach Neustart maximal eine Wiederholung (in-memory-Dedup)."""
    from datetime import date, timedelta
    in5 = (date.today() + timedelta(days=5)).isoformat()
    store = {"LICENSE_KEY": "KEY"}
    gesendet: list = []
    _patch(monkeypatch, store, gesendet,
           {"licensed": True, "expires": in5, "lic_id": "L1"})

    scheduler._check_license_expiry()
    scheduler._check_license_expiry()   # gleicher Prozess → Dedup greift
    assert len(gesendet) == 1, "bald ablaufende Lizenz sollte (einmal) erinnern"
    assert gesendet[0] == 5


def test_gueltig_und_fern_meldet_nicht(monkeypatch):
    from datetime import date, timedelta
    fern = (date.today() + timedelta(days=200)).isoformat()
    store = {"LICENSE_KEY": "KEY"}
    gesendet: list = []
    _patch(monkeypatch, store, gesendet,
           {"licensed": True, "expires": fern, "lic_id": "L1"})
    scheduler._check_license_expiry()
    assert gesendet == []
