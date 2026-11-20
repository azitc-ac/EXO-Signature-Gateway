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
    welt.offen = ""
    monkeypatch.setattr(scheduler, "_bestellung_offen", lambda email: welt.offen)
    # Aktuelles Standard-Zertifikat = das angekündigte (Abwahl-Link prüft das).
    import smime_store
    monkeypatch.setattr(smime_store, "list_user_certs",
                        lambda email: [{"is_default": True, "expiry": welt.ablauf}])
    welt.ablauf = "31.12.2026"
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
    assert welt.werte["_ERNEUERUNG_ANGEKUENDIGT"]["a@x.de"]["bestellt_am"]   # markiert, nicht gelöscht
    # Review 20.11.: Ist die Bestellung noch offen (Hub wartet auf Bestätigung),
    # darf die nächste Schwelle weder erneut ankündigen noch erneut bestellen.
    welt.offen = "offene Bestellung 42 bei hub:certum"
    _lauf(7)
    _lauf(1)
    assert welt.bestellt == ["a@x.de"] and len(welt.mails) == 1
    # Erledigt sich die Bestellung ohne Zertifikat (abgelehnt): neu bestellen,
    # aber NICHT erneut ankündigen.
    welt.offen = ""
    import scheduler
    scheduler._user_notif_sent.clear() if hasattr(scheduler, "_user_notif_sent") else None
    _lauf(1)
    assert welt.bestellt == ["a@x.de", "a@x.de"] and len(welt.mails) == 1


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


def test_abwahl_link_gilt_nur_fuer_das_aktuelle_zertifikat(welt):
    """Review 20.11.: Wurde inzwischen (auch von Hand) ein neues Zertifikat
    eingespielt, schaltete ein später Klick die Verlängerung des NEUEN ab."""
    import erneuerung_vorankuendigung as ev
    _lauf(30)
    token = welt.mails[0][2].split("/erneuerung/abwahl/")[1].split('"')[0]
    assert ev.abwahl_lesen(token) is not None
    welt.ablauf = "31.12.2027"                                  # neues Zertifikat ist Standard
    assert ev.abwahl_lesen(token) is None and ev.abwahl_ausfuehren(token) is None
    assert welt.werte["CA_USER_CONFIG"]["a@x.de"]["auto_renew"] is True


def test_bestellung_offen_erkennt_hub_und_castle(monkeypatch):
    from datetime import datetime, timedelta, timezone
    import acme_state
    import hub_orders
    import scheduler
    monkeypatch.setattr(hub_orders, "list_pending", lambda: [{"order_id": "42", "email": "A@x.de", "provider": "hub:certum"}])
    monkeypatch.setattr(acme_state, "get_order", lambda e: None)
    assert "42" in scheduler._bestellung_offen("a@x.de")
    assert scheduler._bestellung_offen("b@x.de") == ""
    monkeypatch.setattr(hub_orders, "list_pending", lambda: [])
    jetzt = datetime.now(timezone.utc)
    monkeypatch.setattr(acme_state, "get_order", lambda e: {"status": "validating", "created": jetzt.isoformat()})
    assert "CASTLE" in scheduler._bestellung_offen("a@x.de")
    # Hängender Auftrag von vorgestern blockiert nicht für immer
    monkeypatch.setattr(acme_state, "get_order",
                        lambda e: {"status": "validating", "created": (jetzt - timedelta(days=2)).isoformat()})
    assert scheduler._bestellung_offen("a@x.de") == ""
    monkeypatch.setattr(acme_state, "get_order", lambda e: {"status": "error", "created": jetzt.isoformat()})
    assert scheduler._bestellung_offen("a@x.de") == ""


def test_widerrufenes_standardzertifikat_wird_sofort_bestellt(welt):
    """Review 20.11.: Mit widerrufenem Standard wird nicht mehr signiert — keine
    Ankündigungsfrist, sofort bestellen."""
    import scheduler
    scheduler._send_user_renewal_notification("a@x.de", {**CERT, "days_left": 200, "widerrufen": {"grund": "x"}},
                                              settings_store.get("CA_USER_CONFIG")["a@x.de"], 30)
    assert welt.bestellt == ["a@x.de"] and welt.mails == []


def test_abnahme_zaehlt_notify_user_als_entschieden(monkeypatch):
    import abnahme
    werte = {"CA_USER_CONFIG": {"a@x.de": {"backend": "castle_acme", "notify_user": True},
                                "b@x.de": {"backend": "castle_acme"}}}
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))
    p = abnahme._erneuerung_unentschieden()
    assert "b@x.de" in p["befund"] and "a@x.de" not in p["befund"]
