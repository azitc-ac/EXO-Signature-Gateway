"""Ankündigung der automatischen Verlängerung mit Link zum Abwählen (v1.12.116).

Ablauf: erste Schwelle → ankündigen statt bestellen; nächste Schwelle →
bestellen. Empfänger: Verwaltung bei Einwilligung des Inhabers (abschaltbar
über NOTIFY_RENEWAL_NOTICE), sonst der Inhaber. Abwählen per POST.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
sys.path.insert(0, str(Path(__file__).parent))

import settings_store  # noqa: E402

CERT = {"email": "a@x.de", "expiry": "31.12.2026", "days_left": 25}


@pytest.fixture
def welt(monkeypatch):
    werte = {"CERT_RENEWAL_THRESHOLDS": [30, 14, 7, 1], "MAILBOX_CONFIG": {},
             "CA_USER_CONFIG": {"a@x.de": {"backend": "hub:certum", "auto_renew": True}}}
    alt = settings_store.get
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte[k] if k in werte else alt(k, *a, **kw))
    monkeypatch.setattr(settings_store, "force_update", lambda d: werte.update(d))
    monkeypatch.setattr(settings_store, "update", lambda d: werte.update(d))
    import ca_backends
    import mailbox_match
    import notification
    import einwilligung
    import scheduler
    welt.bestellt, welt.mails = [], []

    class _B:
        def get_name(self): return "hub:certum"
        def get_label(self): return "Certum"
        def can_auto_renew(self): return True
        async def initiate_renewal(self, email, cfg, extra=None):
            welt.bestellt.append(email)
            return True
    monkeypatch.setattr(ca_backends, "get_backend", lambda n: _B())
    monkeypatch.setattr(mailbox_match, "match_sender", lambda c, e: {"smime": True})
    monkeypatch.setattr(notification, "_graph_send", lambda to, betreff, html: welt.mails.append((to, betreff, html)) or True)
    monkeypatch.setattr(notification, "_get_notify_to", lambda: "it@x.de")
    monkeypatch.setattr(notification, "send_renewal_notification_to_user", lambda **kw: True)
    monkeypatch.setattr(einwilligung, "liegt_vor", lambda email: welt.einwilligung)
    monkeypatch.setattr(scheduler, "_web_schleife", None)
    welt.einwilligung = {"von": "admin"}
    welt.werte = werte
    return welt


def _lauf(schwelle):
    import scheduler
    scheduler._send_user_renewal_notification("a@x.de", CERT, settings_store.get("CA_USER_CONFIG")["a@x.de"], schwelle)


def test_erst_ankuendigen_dann_bestellen(welt):
    _lauf(30)
    assert welt.bestellt == []
    assert [m[0] for m in welt.mails] == ["it@x.de"]                   # Einwilligung → Verwaltung
    assert "/erneuerung/abwahl/" in welt.mails[0][2]
    assert "17.12.2026" in welt.mails[0][1]                            # Bestellung ab: Ablauf − 14 Tage
    _lauf(30)                                                          # Neustart am selben Tag: dieselbe Schwelle
    assert welt.bestellt == [] and len(welt.mails) == 1
    _lauf(14)
    assert welt.bestellt == ["a@x.de"]
    assert "_ERNEUERUNG_ANGEKUENDIGT" not in welt.werte or "a@x.de" not in welt.werte["_ERNEUERUNG_ANGEKUENDIGT"]


def test_ohne_einwilligung_bekommt_der_inhaber_die_ankuendigung(welt):
    welt.einwilligung = None
    _lauf(30)
    assert [m[0] for m in welt.mails] == ["a@x.de"]
    assert "/erneuerung/abwahl/" in welt.mails[0][2]


def test_schalter_aus_keine_mail_aber_frist_bleibt(welt):
    welt.werte["NOTIFY_RENEWAL_NOTICE"] = False
    _lauf(30)
    assert welt.mails == [] and welt.bestellt == []
    _lauf(14)
    assert welt.bestellt == ["a@x.de"]


def test_einzige_schwelle_bestellt_ohne_ankuendigung(welt):
    welt.werte["CERT_RENEWAL_THRESHOLDS"] = [14]
    _lauf(14)
    assert welt.bestellt == ["a@x.de"] and welt.mails == []


def test_abwahl_get_aendert_nichts_post_schaltet_ab(welt):
    import re
    from fastapi.testclient import TestClient
    from webui.app import app
    _lauf(30)
    token = re.search(r"/erneuerung/abwahl/([A-Za-z0-9_\-]+)", welt.mails[0][2]).group(1)
    c = TestClient(app)
    r = c.get(f"/erneuerung/abwahl/{token}")
    assert r.status_code == 200 and "Verlängerung abschalten" in r.text
    assert welt.werte["CA_USER_CONFIG"]["a@x.de"]["auto_renew"] is True   # GET ändert nichts
    r = c.post(f"/erneuerung/abwahl/{token}")
    assert "Abgeschaltet" in r.text
    assert welt.werte["CA_USER_CONFIG"]["a@x.de"]["auto_renew"] is False
    assert "nicht mehr gültig" in c.post(f"/erneuerung/abwahl/{token}").text  # einmalig
    _lauf(14)
    assert welt.bestellt == []                                         # abgewählt: keine Bestellung


def test_falsches_merkmal_ist_ungueltig(welt):
    from fastapi.testclient import TestClient
    from webui.app import app
    _lauf(30)
    r = TestClient(app).post("/erneuerung/abwahl/falsch")
    assert "nicht mehr gültig" in r.text
    assert welt.werte["CA_USER_CONFIG"]["a@x.de"]["auto_renew"] is True
