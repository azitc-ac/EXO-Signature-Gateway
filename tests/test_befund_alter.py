"""Befunde aus gemerkten Werten nennen ihr Alter und werden nicht still grün
(Prüfung vom 16.11.2026: veraltete Soll-/Ist-Werte in der Abnahme)."""
from __future__ import annotations

import time

import abnahme
import settings_store
import smtp_acl


def _acl(monkeypatch, anzahl, stand):
    werte = {"SMTP_SOURCE_ACL_ENABLED": True}
    alt = settings_store.get
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte[k] if k in werte else alt(k, *a, **kw))
    monkeypatch.setattr(smtp_acl, "range_count", lambda: anzahl)
    monkeypatch.setattr(smtp_acl, "stand_ts", lambda: stand)


def test_frische_liste_ok_mit_stand(monkeypatch):
    _acl(monkeypatch, 120, time.time() - 3600)
    p = abnahme._quell_ip_liste()
    assert p["zustand"] == abnahme.OK and "Stand" in p["befund"]


def test_alte_liste_ist_offen(monkeypatch):
    """Scheitert der Abruf dauerhaft, blieb bis v1.12.42 jede Liste „OK“."""
    _acl(monkeypatch, 120, time.time() - 10 * 86400)
    p = abnahme._quell_ip_liste()
    assert p["zustand"] == abnahme.OFFEN and "10 Tage" in p["befund"]


def test_unbekanntes_alter_ist_kein_ok(monkeypatch):
    _acl(monkeypatch, 120, 0.0)
    assert abnahme._quell_ip_liste()["zustand"] == abnahme.UNBEKANNT


def test_stand_aus_der_zwischenspeicher_datei(monkeypatch, tmp_path):
    datei = tmp_path / "acl.json"
    datei.write_text("[]")
    monkeypatch.setattr(smtp_acl, "_CACHE_FILE", datei)
    monkeypatch.setattr(smtp_acl, "_last_refresh_ts", 0.0)
    assert abs(smtp_acl.stand_ts() - datei.stat().st_mtime) < 1
    monkeypatch.setattr(smtp_acl, "_last_refresh_ts", 123.0)
    assert smtp_acl.stand_ts() == 123.0


def test_gemerkter_regelbefund_gilt_trotz_zurueckgesetztem_merker(monkeypatch):
    """Ein geänderter Loop-Header setzt EXO_CONNECTOR_CREATED zurück; die Regeln
    gibt es weiter. Der Befund darf dann nicht still „entfällt“ werden."""
    import regel_auto_ausnahme as raa
    werte = {"EXO_CONNECTOR_CREATED": False,
             raa.KEY: {"zustand": raa.FEHLT, "fehlt": ["Route via GW (Signatur)"], "geprueft": "x"}}
    alt = settings_store.get
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte[k] if k in werte else alt(k, *a, **kw))
    assert abnahme._regel_auto_ausnahme()["zustand"] == abnahme.OFFEN
