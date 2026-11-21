"""Abgelaufene Signaturzertifikate (Entscheidung 21.11.2026, v1.12.145).

Zusagen:
1. Mit einem abgelaufenen Zertifikat wird NICHT signiert — weder lokal noch über
   den Key Vault. Gibt es kein gültiges, geht die Mail unsigniert; das zählt
   `smime_unsigniert_abgelaufen`, und die Ablaufmail an die Verwaltung sagt es.
   Entschlüsseln geht weiter.
2. Die Erneuerungsmail an den Postfachinhaber folgt NOTIFY_USER_CERT.
3. Nach dem Ablauf kommt die Mail an die Verwaltung wöchentlich, nicht täglich —
   auch über Neustarts hinweg.
4. Bewusst abgewählte Erneuerung + Zertifikat in der Warnfrist → Aufgabe.
Dazu: Jeder stats.increment()-Schlüssel steht in stats.KEYS (sonst verworfen).
"""
import asyncio
import re
import sys
from datetime import datetime as _dt
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import abnahme  # noqa: E402
import notification  # noqa: E402
import scheduler  # noqa: E402
import settings_store  # noqa: E402
import smime_signer  # noqa: E402
import smime_store  # noqa: E402
import stats  # noqa: E402
from test_zertifikat_widerruf import EMAIL, _slot  # noqa: E402

ROH = b"From: max.mustermann@firma.at\r\nSubject: x\r\n\r\nx\r\n"


@pytest.fixture
def ablage(tmp_path, monkeypatch):
    werte = {"CA_USER_CONFIG": {EMAIL: {"backend": "castle_acme"}}}
    monkeypatch.setattr(smime_store, "SMIME_DIR", tmp_path)
    monkeypatch.setattr(smime_store, "schluessel_passwort", lambda: "")
    monkeypatch.setattr(settings_store, "get", lambda k, v=None: werte.get(k, v))
    monkeypatch.setattr(settings_store, "force_update", lambda patch: werte.update(patch))
    gezaehlt = []
    monkeypatch.setattr(stats, "increment", lambda k: gezaehlt.append(k))
    return tmp_path, werte, gezaehlt


def test_abgelaufen_wird_nicht_signiert_aber_entschluesselt(ablage):
    basis, _, gezaehlt = ablage
    slot = _slot(basis, tage=-5)
    assert smime_store.get_signing_paths(EMAIL, allow_backup=True, nur_unwiderrufen=True,
                                         nur_gueltig=True) is None
    assert smime_store.get_signing_cert_path(EMAIL, nur_unwiderrufen=True, nur_gueltig=True) is None
    assert smime_signer.sign(ROH, EMAIL) is None
    assert gezaehlt == ["smime_unsigniert_abgelaufen"]
    # Entschlüsseln: der Schlüssel bleibt erreichbar
    assert smime_store.get_signing_paths(EMAIL, allow_backup=True)[0].parent.name == slot


def test_gueltiges_zweites_zertifikat_signiert(ablage):
    basis, _, gezaehlt = ablage
    _slot(basis, tage=-5)                                     # Standard: abgelaufen
    neu = _slot(basis, standard=False, tage=300)
    assert smime_store.get_signing_paths(EMAIL, nur_unwiderrufen=True,
                                         nur_gueltig=True)[0].parent.name == neu
    assert smime_signer.sign(ROH, EMAIL) is not None
    assert "smime_unsigniert_abgelaufen" not in gezaehlt


def test_key_vault_signiert_nicht_mit_abgelaufenem(ablage, monkeypatch):
    basis, _, gezaehlt = ablage
    _slot(basis, tage=-5)
    import cms_sign
    import keyvault
    monkeypatch.setattr(keyvault, "is_configured", lambda: True)

    async def _vorhanden(_):
        return keyvault.KEY_VORHANDEN
    monkeypatch.setattr(keyvault, "key_zustand", _vorhanden)
    aufrufe = []

    async def _sign(*a, **k):
        aufrufe.append(a)
        return b"signiert"
    monkeypatch.setattr(cms_sign, "sign_smime_keyvault", _sign)
    assert asyncio.run(smime_signer.sign_async(ROH, EMAIL)) is None
    assert aufrufe == [], "Key Vault darf mit abgelaufenem Zertifikat nicht signieren"
    assert gezaehlt == ["smime_unsigniert_abgelaufen"]


def test_ablaufmail_sagt_unsigniert(monkeypatch):
    gesendet = []
    monkeypatch.setattr(notification, "_should_notify", lambda k: True)
    monkeypatch.setattr(notification, "_get_notify_to", lambda: "admin@firma.at")
    monkeypatch.setattr(notification, "_graph_send", lambda to, s, h: gesendet.append(h) or True)
    notification.send_cert_expiry_alert({"email": EMAIL, "expiry": "01.01.2026",
                                         "days_left": -3, "expired": True})
    assert "unsigniert" in gesendet[0]
    notification.send_cert_expiry_alert({"email": EMAIL, "expiry": "01.01.2027",
                                         "days_left": 1, "expired": False})
    assert "unsigniert" not in gesendet[1] and "1 Tag ab" in gesendet[1]


def test_erneuerungsmail_folgt_notify_user_cert(monkeypatch):
    gesendet = []
    monkeypatch.setattr(notification, "_graph_send", lambda *a: gesendet.append(a) or True)
    monkeypatch.setattr(notification, "_should_notify", lambda k: k != "NOTIFY_USER_CERT")
    assert notification.send_renewal_notification_to_user(
        EMAIL, {"days_left": 7, "expiry": "x"}, "https://u", "castle_acme") is False
    assert gesendet == []


def _lauf(monkeypatch, tag: str):
    class _Uhr(_dt):
        @classmethod
        def now(cls, tz=None):
            return _dt.fromisoformat(tag + "T09:00:00")
    monkeypatch.setattr(scheduler, "datetime", _Uhr)
    scheduler._cert_alerts_sent.clear()                       # wie nach einem Neustart
    scheduler._check_smime_lifecycle()


def test_abgelaufen_nur_woechentlich_auch_nach_neustart(ablage, monkeypatch):
    basis, werte, _ = ablage
    _slot(basis, tage=-5)
    werte["CA_USER_CONFIG"] = {}
    mails = []
    monkeypatch.setattr(notification, "send_cert_expiry_alert", lambda c: mails.append(c) or True)
    _lauf(monkeypatch, "2026-11-21")
    _lauf(monkeypatch, "2026-11-22")
    _lauf(monkeypatch, "2026-11-27")
    assert len(mails) == 1, "nach dem Ablauf höchstens einmal pro Woche"
    _lauf(monkeypatch, "2026-11-28")
    assert len(mails) == 2
    assert werte["_ZERT_ABGELAUFEN_GEMELDET"] == {EMAIL: "2026-11-28"}


def test_vor_dem_ablauf_weiter_taeglich(ablage, monkeypatch):
    basis, werte, _ = ablage
    _slot(basis, tage=5)
    werte["CA_USER_CONFIG"] = {}
    mails = []
    monkeypatch.setattr(notification, "send_cert_expiry_alert", lambda c: mails.append(c) or True)
    _lauf(monkeypatch, "2026-11-21")
    _lauf(monkeypatch, "2026-11-22")
    assert len(mails) == 2


@pytest.mark.parametrize("auto_renew,tage,offen", [
    (False, 5, True), (False, -3, True), (False, -40, False), (False, 60, False),
    (True, 5, False), (None, 5, False)])
def test_aufgabe_ablauf_ohne_erneuerung(ablage, auto_renew, tage, offen):
    basis, werte, _ = ablage
    _slot(basis, tage=tage)
    cfg = {"backend": "castle_acme"}
    if auto_renew is not None:
        cfg["auto_renew"] = auto_renew
    werte["CA_USER_CONFIG"] = {EMAIL: cfg}
    p = abnahme._ablauf_ohne_erneuerung()
    assert (p["zustand"] == abnahme.OFFEN) is offen, p
    if offen:
        assert EMAIL in p["befund"]


def test_jeder_zaehler_steht_in_keys():
    """delivery_queued, graph_ruecklauf, enc_intern_normal wurden bis v1.12.145
    gezählt, standen aber nicht in KEYS — _load verwarf sie, angezeigt wurden
    sie nirgends."""
    app = Path(__file__).resolve().parent.parent / "app"
    fehlt, dynamisch = set(), []
    for p in app.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        fehlt |= set(re.findall(r'\bstats\.increment\(\s*"([a-z_]+)"', text)) - set(stats.KEYS)
        dynamisch += [f"{p.name}" for _ in re.findall(r'\bstats\.increment\(\s*[^"\s)]', text)]
    assert not fehlt, f"stats.increment mit Schlüssel außerhalb von stats.KEYS: {sorted(fehlt)}"
    assert not dynamisch, f"stats.increment ohne festen Schlüssel (nicht prüfbar): {dynamisch}"


def test_nur_widerrufenes_zertifikat_wird_gezaehlt(ablage):
    """Review 21.11.: Geschwister zu „abgelaufen“ — ein nur noch widerrufenes
    Zertifikat ließ die Post ebenfalls unsigniert, ohne Zahl."""
    basis, _, gezaehlt = ablage
    slot = _slot(basis, tage=300)
    d = basis / EMAIL / "certs" / slot
    import json
    (d / "widerrufen.json").write_text(json.dumps({"grund": "nicht_mehr_benoetigt"}))
    assert smime_store._widerrufen(d)
    assert smime_signer.sign(ROH, EMAIL) is None
    assert gezaehlt == ["smime_unsigniert_abgelaufen"]


def test_sammelbestellung_zaehlt_widerrufene_nicht(ablage):
    basis, _, _ = ablage
    import json
    import sammelbestellung
    slot = _slot(basis, tage=300)
    assert sammelbestellung._gueltige_zertifikate(EMAIL) == 1
    (basis / EMAIL / "certs" / slot / "widerrufen.json").write_text(json.dumps({"grund": "x"}))
    assert sammelbestellung._gueltige_zertifikate(EMAIL) == 0


# ── S1 (Review 21.11.): Widerruf bestellt nicht still neu ───────────────────

@pytest.mark.parametrize("grund,standard,aus", [
    ("nicht_mehr_benoetigt", True, True),
    ("ersetzt", True, False),
    ("schluessel_kompromittiert", True, False),
    ("nicht_mehr_benoetigt", False, False),          # alter Slot: Einstellung bleibt
])
def test_widerruf_schaltet_erneuerung_ab(ablage, monkeypatch, grund, standard, aus):
    from test_zertifikat_widerruf import _Client, _knopf
    basis, werte, _ = ablage
    aktuell = _slot(basis, tage=300)
    slot = aktuell if standard else _slot(basis, standard=False, tage=100)
    werte["CA_USER_CONFIG"] = {EMAIL: {"backend": "castle_acme", "auto_renew": True}}
    r = _knopf(monkeypatch, _Client(), slot, grund)
    assert r["ok"] and r["erneuerung_aus"] is aus
    assert werte["CA_USER_CONFIG"][EMAIL]["auto_renew"] is (not aus)


# ── S2 (Review 21.11.): Bestellungen nach einem Neustart wieder aufnehmen ───

@pytest.mark.parametrize("status", ["challenge_replied", "validating", "finalizing"])
def test_neustart_nimmt_bestellung_wieder_auf(monkeypatch, status):
    import acme_state
    order = {"email": EMAIL, "status": status, "flow_id": "f1"}
    monkeypatch.setattr(acme_state, "_load_orders", lambda: None)
    monkeypatch.setattr(acme_state, "_orders", {EMAIL: order})
    aufgenommen = []

    async def _fertig(o):
        aufgenommen.append(o["status"])
    monkeypatch.setattr(acme_state, "complete_order_after_challenge", _fertig)
    monkeypatch.setattr(acme_state, "_register_task", lambda e, t: None)

    async def _lauf():
        acme_state.resume_pending_polls()
        await asyncio.sleep(0)
    asyncio.run(_lauf())
    assert aufgenommen == [status]


class _AcmeClient:
    """ACME-Attrappe: Bestellung ist schon „valid“ (Finalize lief vor dem Neustart)."""
    def __init__(self, *a, **k):
        self.aufrufe = []

    async def trigger_challenge(self, url):
        self.aufrufe.append("trigger")

    async def poll_order_status(self, url, timeout_sec=0):
        self.aufrufe.append("poll")
        return {"status": "valid", "certificate": "https://zert"}

    async def finalize(self, *a):
        self.aufrufe.append("finalize")

    async def download_certificate(self, url):
        self.aufrufe.append("download")
        return "PEM"


def test_finalizing_holt_nur_ab_ohne_zweites_finalize(monkeypatch):
    import acme_state
    client = _AcmeClient()
    monkeypatch.setattr(acme_state, "AcmeClient", lambda *a, **k: client)
    monkeypatch.setattr(acme_state, "get_or_create_account_key", lambda e: None)
    monkeypatch.setattr(acme_state, "get_account_url", lambda e, staging=False: "u")
    uebernommen = []

    async def _uebernehmen(order, pem, email, fid):
        uebernommen.append(pem)
    monkeypatch.setattr(acme_state, "_zertifikat_uebernehmen", _uebernehmen)
    monkeypatch.setattr(acme_state, "save_order", lambda e, s: None)
    order = {"email": EMAIL, "status": "finalizing", "flow_id": "f1", "order_url": "o",
             "challenge_url": "c"}
    asyncio.run(acme_state.complete_order_after_challenge(order))
    assert client.aufrufe == ["poll", "download"] and uebernommen == ["PEM"]
