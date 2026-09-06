"""Fallnetz: nach Verarbeitung/Entschlüsselung unzustellbare Post landet in der
Warteschlange (Grund „delivery_failed") statt verloren zu gehen — und der
Aufrufer wirft NICHT, sodass Exchange 250 OK bekommt (kein NDR, keine Dublette).
"""
import held_mails
import reinject
import settings_store
import stats


def test_held_reason_und_count_nach_grund(tmp_path, monkeypatch):
    monkeypatch.setattr(held_mails, "_HELD_DIR", tmp_path)
    held_mails.hold("a@x.de", ["b@x.de"], b"From: a@x.de\r\n\r\nx", reason="delivery_failed")
    held_mails.hold("c@x.de", ["d@x.de"], b"From: c@x.de\r\n\r\ny")  # default = maintenance
    assert held_mails.count() == 2
    assert held_mails.count("delivery_failed") == 1
    assert held_mails.count("maintenance") == 1
    assert sorted(m["reason"] for m in held_mails.list_all()) == ["delivery_failed", "maintenance"]


def test_smtp_zustellfehler_wird_eingereiht_nicht_geworfen(monkeypatch):
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a: "smtp" if k == "REINJECT_MODE" else None)

    def boom(*a, **k):
        raise RuntimeError("smarthost down")
    monkeypatch.setattr(reinject, "_send_smtp", boom)

    cap = {}
    monkeypatch.setattr(held_mails, "hold",
                        lambda s, r, b, processed_msg=None, reason="maintenance":
                        cap.update(reason=reason, to=list(r)) or "id1")
    monkeypatch.setattr(stats, "increment", lambda *a, **k: None)

    # darf NICHT werfen
    reinject.send("a@x.de", ["b@x.de"], b"From: a@x.de\r\nTo: b@x.de\r\n\r\nbody")
    assert cap.get("reason") == "delivery_failed"
    assert cap.get("to") == ["b@x.de"]


def test_manueller_retry_wirft_statt_neu_einzureihen(monkeypatch):
    """queue_on_failure=False (Release-Route): erneuter Fehlschlag wirft, reiht
    NICHT neu ein — die Freigabe meldet ehrlich Fehler, Mail bleibt in der Queue."""
    import pytest
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a: "smtp" if k == "REINJECT_MODE" else None)
    monkeypatch.setattr(reinject, "_send_smtp",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    called = {"hold": False}
    monkeypatch.setattr(held_mails, "hold",
                        lambda *a, **k: called.update(hold=True) or "id")
    monkeypatch.setattr(stats, "increment", lambda *a, **k: None)
    with pytest.raises(RuntimeError):
        reinject.send("a@x.de", ["b@x.de"],
                      b"From: a@x.de\r\nTo: b@x.de\r\n\r\nbody", queue_on_failure=False)
    assert called["hold"] is False
