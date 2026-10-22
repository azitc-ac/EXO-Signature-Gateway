"""Einwilligung: Das Gateway liest ein Postfach und handelt im Namen seines
Inhabers nur mit Rechtsgrund (einwilligung.py).

Bis v1.10.34 las und antwortete der CASTLE-Weg immer — der Haken
„automatische Bestätigung“ wurde dort nie geprüft und nannte den Umfang nicht.
Diese Tests halten die Kernsperren fest: vor der Bestellung, vor jedem Lesen,
vor jeder Antwort im Namen des Inhabers.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import config
import einwilligung


@pytest.fixture
def ablage(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    return tmp_path


# ── Modul ────────────────────────────────────────────────────────────────────

def test_erteilen_widerrufen(ablage):
    assert einwilligung.liegt_vor("a@x.de") is None
    einwilligung.erteilen("A@X.de", "admin:chefin")
    e = einwilligung.liegt_vor("a@x.de")
    assert e["von"] == "admin:chefin" and set(e["umfang"]) == {"lesen", "im_namen"}
    assert einwilligung.widerrufen("a@x.de")
    assert einwilligung.liegt_vor("a@x.de") is None


def test_neue_fassung_macht_alte_einwilligung_ungueltig(ablage, monkeypatch):
    einwilligung.erteilen("a@x.de", "nutzer")
    monkeypatch.setattr(einwilligung, "FASSUNG", einwilligung.FASSUNG + 1)
    assert einwilligung.liegt_vor("a@x.de") is None


def test_unvollstaendiger_umfang_gilt_nicht(ablage):
    einwilligung.erteilen("a@x.de", "nutzer")
    d = json.loads((ablage / "einwilligungen.json").read_text())
    d["erteilt"]["a@x.de"]["umfang"] = ["lesen"]
    (ablage / "einwilligungen.json").write_text(json.dumps(d))
    assert einwilligung.liegt_vor("a@x.de") is None


def test_datei_ist_600(ablage):
    einwilligung.erteilen("a@x.de", "nutzer")
    assert oct((ablage / "einwilligungen.json").stat().st_mode & 0o777) == "0o600"


def test_token_nur_als_hash_und_einmalig(ablage):
    token = einwilligung.anfrage_erstellen("a@x.de", "castle_acme", "2026-10-22T10:00:00Z", "admin:x")
    assert token not in (ablage / "einwilligungen.json").read_text(), "Token im Klartext gespeichert"
    # Lesen (GET) verbraucht nichts — Mail-Scanner öffnen Links vorab
    assert einwilligung.anfrage_lesen(token)["email"] == "a@x.de"
    assert einwilligung.anfrage_lesen(token)["email"] == "a@x.de"
    assert einwilligung.liegt_vor("a@x.de") is None
    a = einwilligung.anfrage_beantworten(token, zustimmung=True)
    assert a["anbieter"] == "castle_acme" and a["terms_at"]
    assert einwilligung.liegt_vor("a@x.de")["von"] == "nutzer"
    assert einwilligung.anfrage_beantworten(token, zustimmung=True) is None, "zweimal einlösbar"


def test_ablehnung_erteilt_nichts(ablage):
    token = einwilligung.anfrage_erstellen("a@x.de", "castle_acme")
    assert einwilligung.anfrage_beantworten(token, zustimmung=False)
    assert einwilligung.liegt_vor("a@x.de") is None
    assert einwilligung.anfrage_lesen(token) is None


def test_abgelaufene_anfrage_gilt_nicht(ablage, monkeypatch):
    token = einwilligung.anfrage_erstellen("a@x.de", "castle_acme")
    spaeter = datetime.now(timezone.utc) + timedelta(days=einwilligung.ANFRAGE_GUELTIG_TAGE + 1)
    monkeypatch.setattr(einwilligung, "_jetzt", lambda: spaeter)
    assert einwilligung.anfrage_lesen(token) is None
    assert einwilligung.anfrage_beantworten(token, zustimmung=True) is None
    assert einwilligung.liegt_vor("a@x.de") is None


def test_neue_anfrage_entwertet_die_alte(ablage):
    alt = einwilligung.anfrage_erstellen("a@x.de", "castle_acme")
    neu = einwilligung.anfrage_erstellen("a@x.de", "castle_acme")
    assert einwilligung.anfrage_lesen(alt) is None and einwilligung.anfrage_lesen(neu)


def test_anfrage_senden_nicht_doppelt(ablage, monkeypatch):
    import notification
    gesendet = []
    monkeypatch.setattr(notification, "send_einwilligung_anfrage",
                        lambda email, link, ca: gesendet.append(link) or True)
    assert einwilligung.anfrage_senden("a@x.de", "castle_acme") is True
    assert einwilligung.anfrage_senden("a@x.de", "castle_acme") is False, "Hintergrundwege schicken keine zweite"
    assert einwilligung.anfrage_senden("a@x.de", "castle_acme", erneut=True) is True
    assert len(gesendet) == 2 and all("/einwilligung/" in l for l in gesendet)


# ── Kernsperren in acme_state ────────────────────────────────────────────────

@pytest.fixture
def smime_aktiv(monkeypatch):
    import mailbox_match
    monkeypatch.setattr(mailbox_match, "match_sender", lambda cfg, email: {"smime": True})


def test_ohne_einwilligung_keine_castle_bestellung(ablage, smime_aktiv, monkeypatch):
    import acme_state
    monkeypatch.setattr(acme_state, "get_or_create_account_key",
                        lambda email: pytest.fail("Konto/Order ohne Einwilligung angelegt"))
    with pytest.raises(acme_state.EinwilligungFehlt):
        asyncio.run(acme_state.initiate_acme_order("a@x.de", {}))


def test_mit_einwilligung_geht_es_weiter(ablage, smime_aktiv, monkeypatch):
    import acme_state

    class Weiter(Exception):
        pass

    def _schluessel(email):
        raise Weiter()
    monkeypatch.setattr(acme_state, "get_or_create_account_key", _schluessel)
    einwilligung.erteilen("a@x.de", "nutzer")
    with pytest.raises(Weiter):
        asyncio.run(acme_state.initiate_acme_order("a@x.de", {}))


def test_ohne_einwilligung_keine_antwort_im_namen(ablage, monkeypatch):
    """Auch der SMTP-Abfangweg (handler.py) und eine vor dem Widerruf angelegte
    Order antworten nicht mehr."""
    import acme_state
    gespeichert = {}
    order = {"email": "a@x.de", "status": "waiting_challenge", "token_part2": "AAAA",
             "from_address": "acme@castle.cloud", "flow_id": "t"}
    monkeypatch.setattr(acme_state, "_orders", {"a@x.de": dict(order)})
    monkeypatch.setattr(acme_state, "_save_orders", lambda: None)
    monkeypatch.setattr(acme_state, "save_order", lambda email, st: gespeichert.update(st))
    monkeypatch.setattr(acme_state, "get_or_create_account_key", lambda email: object())
    monkeypatch.setattr(acme_state, "compute_key_authorization", lambda a, b, k: "digest")

    async def _senden(*a, **kw):
        pytest.fail("Antwort im Namen des Inhabers ohne Einwilligung")
    monkeypatch.setattr(acme_state, "_send_challenge_reply", _senden)
    asyncio.run(acme_state.handle_challenge_email(dict(order), "QUFBQQ"))
    assert gespeichert.get("status") == "error" and "Einwilligung" in gespeichert.get("error", "")


def test_ohne_einwilligung_wird_das_postfach_nicht_gelesen(ablage, monkeypatch):
    """Auch ein nach Neustart wieder aufgenommener Poll (resume_pending_polls)
    liest ohne Einwilligung nicht."""
    import acme_state
    import graph_client
    gespeichert = {}
    order = {"email": "a@x.de", "status": "waiting_challenge", "flow_id": "t",
             "created": "2026-10-22T10:00:00+00:00"}
    monkeypatch.setattr(acme_state, "get_order", lambda email: dict(order))
    monkeypatch.setattr(acme_state, "save_order", lambda email, st: gespeichert.update(st))

    async def _schlafen(s):
        return None
    monkeypatch.setattr(acme_state.asyncio, "sleep", _schlafen)

    async def _token():
        pytest.fail("Graph-Token für das Lesen ohne Einwilligung geholt")
    monkeypatch.setattr(graph_client, "_acquire_token_async", _token)
    asyncio.run(acme_state._poll_mailbox_for_challenge("a@x.de"))
    assert gespeichert.get("status") == "error"


# ── Aufrufer: fragen statt scheitern ─────────────────────────────────────────

def test_sammellauf_fragt_den_inhaber(ablage, monkeypatch):
    import acme_state
    import ca_backends
    import sammelbestellung as sb
    import settings_store
    angefragt = []

    class _Castle:
        def get_name(self): return "castle_acme"
        async def initiate_renewal(self, email, cfg, extra=None):
            raise acme_state.EinwilligungFehlt("fehlt")
    monkeypatch.setattr(ca_backends, "get_backend", lambda name: _Castle())
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: {} if k == "CA_USER_CONFIG" else d)
    monkeypatch.setattr(einwilligung, "anfrage_senden",
                        lambda email, anbieter, terms="", von="", erneut=False: angefragt.append((email, terms)) or True)
    r = asyncio.run(sb._eine_bestellung("a@x.de", "castle_acme", "2026-10-22T10:00:00Z"))
    assert r["ok"] is False and r.get("angefragt") is True
    assert angefragt == [("a@x.de", "2026-10-22T10:00:00Z")]
    assert r.get("grund_kurz") != "guthaben"


def test_verlaengerung_fragt_statt_still_zu_scheitern(ablage, monkeypatch):
    import acme_state
    import ca_backends
    import mailbox_match
    import scheduler
    import settings_store
    angefragt = []

    class _Castle:
        def can_auto_renew(self): return True
        async def initiate_renewal(self, email, cfg, extra=None):
            raise acme_state.EinwilligungFehlt("fehlt")
    monkeypatch.setattr(ca_backends, "get_backend", lambda name: _Castle())
    monkeypatch.setattr(mailbox_match, "match_sender", lambda cfg, email: {"smime": True})
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: {} if k == "MAILBOX_CONFIG" else d)
    monkeypatch.setattr(einwilligung, "anfrage_senden",
                        lambda email, anbieter, terms="", von="", erneut=False: angefragt.append(von) or True)
    import notification
    monkeypatch.setattr(notification, "send_renewal_notification_to_user",
                        lambda **kw: pytest.fail("Upload-Mail statt Einwilligungsanfrage"))
    scheduler._send_user_renewal_notification(
        "a@x.de", {}, {"backend": "castle_acme", "auto_renew": True, "notify_user": True}, 14)
    assert angefragt == ["verlaengerung"]


def test_automatik_willigt_nur_mit_bewusster_einwilligung_ein(monkeypatch):
    """Bis v1.10.34 gab die Automatik beim Aktivieren auto_confirm=True fest mit."""
    import sammelbestellung
    import settings_store
    from webui.routen import mailboxes
    gerufen = {}

    async def _start(weg, adressen, **kw):
        gerufen.update(kw)
        return {"ok": True}
    monkeypatch.setattr(sammelbestellung, "lauf_starten", _start)
    werte = {"SMIME_AUTO_ENROLL": True, "SMIME_AUTO_ENROLL_CA": "castle_acme",
             "SMIME_AUTO_ENROLL_EINWILLIGUNG_AT": ""}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: werte.get(k, d))
    asyncio.run(mailboxes._auto_enrollment_anstossen(["a@x.de"]))
    assert gerufen["auto_confirm"] is False
    werte["SMIME_AUTO_ENROLL_EINWILLIGUNG_AT"] = "2026-10-22T18:00:00Z"
    asyncio.run(mailboxes._auto_enrollment_anstossen(["a@x.de"]))
    assert gerufen["auto_confirm"] is True
