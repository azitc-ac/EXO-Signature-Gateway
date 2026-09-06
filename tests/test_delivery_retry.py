"""Auto-Retry des Zustell-Fallnetzes, Kadenz an Exchanges Queue-Retry angelehnt.

Exchange-Standardwerte (Transport service auf Mailbox-Servern):
  Glitch    4× 1 min,  Transient 6× 5 min,  danach 15 min,  Aufgabe nach 2 Tagen.
Unterschied: bei „Aufgabe" wird NICHT gelöscht — die Mail bleibt zur manuellen
Zustellung liegen (Fallnetz gegen Verlust entschlüsselter Post).
"""
from datetime import datetime, timedelta, timezone

import held_mails


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(held_mails, "_HELD_DIR", tmp_path)


def _jetzt():
    # Bezugszeitpunkt berechnen, nicht verdrahten (siehe test_keine_zeitbomben);
    # Mittag, damit ein Lauf nahe Mitternacht beim Formatieren nicht danebenlandet.
    return datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)


def test_kadenz_entspricht_exchange():
    # Glitch: Versuche 0..3 → 1 min
    for v in range(0, 4):
        assert held_mails._abstand_fuer(v) == 60
    # Transient: Versuche 4..9 → 5 min
    for v in range(4, 10):
        assert held_mails._abstand_fuer(v) == 300
    # danach: 15 min
    for v in (10, 11, 50):
        assert held_mails._abstand_fuer(v) == 900


def test_delivery_failed_bekommt_retry_plan_maintenance_nicht(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    df = held_mails.hold("a@x.de", ["b@x.de"], b"From: a@x.de\r\n\r\nx", reason="delivery_failed")
    mt = held_mails.hold("c@x.de", ["d@x.de"], b"From: c@x.de\r\n\r\ny")  # maintenance

    import json
    d_df = json.loads((tmp_path / f"{df}.json").read_text())
    d_mt = json.loads((tmp_path / f"{mt}.json").read_text())
    assert d_df["attempts"] == 0 and d_df["retry_exhausted"] is False
    assert "next_retry" in d_df and "first_failed" in d_df
    assert "next_retry" not in d_mt  # Wartungsmodus wird bewusst manuell freigegeben


def test_due_for_retry_beachtet_zeitpunkt_grund_und_aufgabe(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    now = _jetzt()
    # next_retry = now+60s bei Anlage → jetzt noch nicht fällig
    monkeypatch.setattr(held_mails, "datetime", _FixedNow(now))
    mid = held_mails.hold("a@x.de", ["b@x.de"], b"From: a@x.de\r\n\r\nx", reason="delivery_failed")
    held_mails.hold("c@x.de", ["d@x.de"], b"From: c@x.de\r\n\r\ny")  # maintenance – nie fällig

    assert held_mails.due_for_retry(now=now) == []                       # +0s: zu früh
    assert held_mails.due_for_retry(now=now + timedelta(seconds=61)) == [mid]  # fällig
    # nur delivery_failed:
    assert all(i == mid for i in held_mails.due_for_retry(now=now + timedelta(hours=1)))


def test_mark_retry_failed_plant_nach_und_gibt_nach_2_tagen_auf(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    start = _jetzt()
    monkeypatch.setattr(held_mails, "datetime", _FixedNow(start))
    mid = held_mails.hold("a@x.de", ["b@x.de"], b"From: a@x.de\r\n\r\nx", reason="delivery_failed")

    # 1. Fehlschlag kurz nach Anlage → attempts=1, noch nicht aufgegeben
    st = held_mails.mark_retry_failed(mid, now=start + timedelta(minutes=1))
    assert st == {"attempts": 1, "retry_exhausted": False}

    # Nach >2 Tagen: aufgegeben, aber NICHT gelöscht
    st = held_mails.mark_retry_failed(mid, now=start + timedelta(days=2, seconds=1))
    assert st["retry_exhausted"] is True
    assert (tmp_path / f"{mid}.json").exists()                 # Fallnetz: bleibt liegen
    assert held_mails.due_for_retry(now=start + timedelta(days=3)) == []  # kein Auto-Retry mehr


def test_scheduler_retry_erfolg_loescht_fehlschlag_plant_nach(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    import json

    import reinject
    import scheduler

    # Erfolg → aus der Warteschlange entfernt
    mid = held_mails.hold("a@x.de", ["b@x.de"],
                          b"From: a@x.de\r\nTo: b@x.de\r\n\r\nx", reason="delivery_failed")
    monkeypatch.setattr(reinject, "send", lambda *a, **k: None)
    monkeypatch.setattr(held_mails, "due_for_retry", lambda now=None: [mid])
    scheduler._retry_held_deliveries()
    assert not (tmp_path / f"{mid}.json").exists()

    # Fehlschlag → bleibt liegen, Versuch hochgezählt
    mid2 = held_mails.hold("a@x.de", ["b@x.de"],
                           b"From: a@x.de\r\nTo: b@x.de\r\n\r\nx", reason="delivery_failed")

    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(reinject, "send", boom)
    monkeypatch.setattr(held_mails, "due_for_retry", lambda now=None: [mid2])
    scheduler._retry_held_deliveries()
    d = json.loads((tmp_path / f"{mid2}.json").read_text())
    assert d["attempts"] == 1
    assert (tmp_path / f"{mid2}.json").exists()


class _FixedNow:
    """Ersatz für das datetime-Modul in held_mails, dessen now() fest ist —
    fromisoformat/timedelta/timezone bleiben echt."""
    def __init__(self, fixed):
        self._fixed = fixed
    def now(self, tz=None):
        return self._fixed if tz is None else self._fixed.astimezone(tz)
    def __getattr__(self, name):
        return getattr(datetime, name)
