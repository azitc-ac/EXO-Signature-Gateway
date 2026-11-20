"""Automatische Verlängerung: Beleg, Haken, Bezugsweg (v1.12.115).

* Hub-Bestellungen: Der Zustimmungsbeleg der letzten angenommenen Bestellung
  wird gemerkt und bei der Verlängerung (ohne Beleg) mitgeschickt. Ohne ihn
  lehnt die Gegenstelle bei Anbietern mit Bedingungen ab.
* Sammel-/Auto-Bestellung setzt „Automatisch erneuern“ (abgewählt bleibt abgewählt).
* Ohne eigenen Bezugsweg nimmt der Scheduler die Vorgabe — nur bei
  ausdrücklichem auto_renew, nie für Altbestand mit nur „benachrichtigen“.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

import settings_store  # noqa: E402


@pytest.fixture
def speicher(monkeypatch):
    werte: dict = {}
    alt = settings_store.get
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte[k] if k in werte else alt(k, *a, **kw))
    monkeypatch.setattr(settings_store, "force_update", lambda d: werte.update(d))
    monkeypatch.setattr(settings_store, "update", lambda d: werte.update(d))
    return werte


@pytest.fixture
def hub(monkeypatch):
    import hub_client
    import hub_orders
    import einwilligung
    gesendet = []

    async def _order(email, csr, meta, provider="", ca_terms_accepted_at=""):
        gesendet.append(ca_terms_accepted_at)
        return hub.antwort
    hub.antwort = {"ok": True, "order_id": "o-1"}
    monkeypatch.setattr(hub_client, "cert_order", _order)
    monkeypatch.setattr(hub_client, "cert_is_registered", lambda: True)
    monkeypatch.setattr(hub_orders, "save_pending", lambda *a, **kw: None)
    monkeypatch.setattr(einwilligung, "liegt_vor", lambda email: {"von": "test"})
    import notification
    monkeypatch.setattr(notification, "send_user_cert_verification_pending", lambda *a, **kw: True)
    from ca_backends import hub_provider
    hub.backend = hub_provider.HubProviderBackend({"id": "certum", "label": "Certum"})
    hub.gesendet = gesendet
    return hub


def test_verlaengerung_nimmt_den_beleg_der_letzten_bestellung(speicher, hub):
    asyncio.run(hub.backend.initiate_renewal("A@x.de", {}, extra={"ca_terms_accepted_at": "2026-11-01T10:00:00Z"}))
    asyncio.run(hub.backend.initiate_renewal("a@x.de", {}))          # Scheduler: ohne extra
    assert hub.gesendet == ["2026-11-01T10:00:00Z", "2026-11-01T10:00:00Z"]


def test_beleg_gilt_nur_beim_selben_anbieter(speicher, hub):
    from ca_backends import hub_provider
    asyncio.run(hub.backend.initiate_renewal("a@x.de", {}, extra={"ca_terms_accepted_at": "T1"}))
    anderer = hub_provider.HubProviderBackend({"id": "swisssign", "label": "SwissSign"})
    asyncio.run(anderer.initiate_renewal("a@x.de", {}))
    assert hub.gesendet == ["T1", ""]


def test_abgelehnte_bestellung_hinterlaesst_keinen_beleg(speicher, hub):
    hub.antwort = {"ok": False, "error": "Bedingungen nicht akzeptiert"}
    with pytest.raises(RuntimeError):
        asyncio.run(hub.backend.initiate_renewal("a@x.de", {}, extra={"ca_terms_accepted_at": "T1"}))
    assert "_CA_ZUSTIMMUNG" not in speicher


@pytest.mark.parametrize("vorher,soll", [
    ({}, True),                                   # Sammellauf ohne Eintrag → an
    ({"backend": "hub:certum", "auto_renew": False}, False),   # bewusst aus bleibt aus
])
def test_sammelbestellung_setzt_automatisch_erneuern(speicher, monkeypatch, vorher, soll):
    import ca_backends
    import sammelbestellung

    class _B:
        def get_name(self): return "hub:certum"
        async def initiate_renewal(self, *a, **kw): return True
    monkeypatch.setattr(ca_backends, "get_backend", lambda name: _B())
    speicher["CA_USER_CONFIG"] = {"a@x.de": vorher} if vorher else {}
    r = asyncio.run(sammelbestellung._eine_bestellung("a@x.de", "hub:certum", "T"))
    assert r["ok"]
    eintrag = speicher["CA_USER_CONFIG"]["a@x.de"]
    assert eintrag["auto_renew"] is soll and eintrag["backend"] == "hub:certum"


@pytest.mark.parametrize("cfg,soll", [
    ({"auto_renew": True}, "castle_acme"),                 # ausdrücklich → Vorgabe
    ({"notify_user": True}, "assisted_manual"),            # Altbestand → keine Bestellung
    ({"backend": "hub:certum", "auto_renew": True}, "hub:certum"),
])
def test_scheduler_bezugsweg_ohne_eigenen_eintrag(speicher, monkeypatch, cfg, soll):
    import ca_backends
    import mailbox_match
    import notification
    import scheduler
    from ca_backends import registry
    genommen = []

    class _B:
        def __init__(self, n): self.n = n
        def get_name(self): return self.n
        def can_auto_renew(self): return self.n != "assisted_manual"
        async def initiate_renewal(self, *a, **kw): return True
    monkeypatch.setattr(ca_backends, "get_backend", lambda n: genommen.append(n) or _B(n))
    monkeypatch.setattr(registry, "get_backend", lambda n: _B(n))
    monkeypatch.setattr(mailbox_match, "match_sender", lambda c, e: {"smime": True})
    monkeypatch.setattr(notification, "send_renewal_notification_to_user", lambda **kw: True)
    monkeypatch.setattr(scheduler, "_web_schleife", None)
    speicher["SMIME_AUTO_ENROLL_CA"] = "castle_acme"
    speicher["MAILBOX_CONFIG"] = {}
    scheduler._send_user_renewal_notification("a@x.de", {}, cfg, 30)
    assert genommen[0] == soll


def test_aufgabe_meldet_unentschiedene_postfaecher(speicher, monkeypatch):
    import abnahme
    import ca_backends

    class _B:
        def __init__(self, n): self.n = n
        def can_auto_renew(self): return self.n != "assisted_manual"
    monkeypatch.setattr(ca_backends, "get_backend", lambda n: _B(n))
    speicher["CA_USER_CONFIG"] = {
        "alt@x.de": {"backend": "hub:certum"},                       # nie entschieden → offen
        "aus@x.de": {"backend": "hub:certum", "auto_renew": False},  # bewusst aus
        "hand@x.de": {"backend": "assisted_manual"},                 # bestellt nicht selbst
    }
    p = abnahme._erneuerung_unentschieden()
    assert p["zustand"] == abnahme.OFFEN and "alt@x.de" in p["befund"]
    assert "aus@x.de" not in p["befund"] and "hand@x.de" not in p["befund"]
    speicher["CA_USER_CONFIG"]["alt@x.de"]["auto_renew"] = True
    assert abnahme._erneuerung_unentschieden()["zustand"] == abnahme.OK
