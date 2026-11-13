"""Erneute Einlieferung: keine doppelte Zustellung aus der Warteschlange,
Tenant-Weiterleitung wartet länger als das Ziel (v1.12.1).

ANLASS (12.11.2026, Exchange-Störung): Ein Gateway leitete Post eines fremden
Tenants an ein zweites Gateway weiter und gab nach 30 s auf. Das Ziel hatte da
die Mail schon angenommen; sein eigener Rückweg zu Exchange hing 30 s, dann
reihte es sie ein und antwortete nach 33 s mit 250. Exchange lieferte erneut
ein, und jeder gescheiterte Versuch legte eine weitere Kopie in die
Warteschlange (drei Stück). Jede wurde später nachgestellt.
"""
import asyncio
import inspect
import json

import pytest

import held_mails
import reinject


def _mail(mid="<a1@firma.de>", datum="Thu, 12 Nov 2026 20:39:16 +0100", koerper="x"):
    kopf = b"From: max@firma.de\r\nTo: erika@beispiel.de, eva@beispiel.de\r\n"
    if mid:
        kopf += f"Message-ID: {mid}\r\n".encode()
    if datum:
        kopf += f"Date: {datum}\r\n".encode()
    return kopf + b"Subject: Test\r\n\r\n" + koerper.encode()


@pytest.fixture(autouse=True)
def leer(tmp_path, monkeypatch):
    monkeypatch.setattr(held_mails, "_HELD_DIR", tmp_path)
    monkeypatch.setattr(held_mails, "_zugestellt", {})
    return tmp_path


def _eintraege(ordner):
    return [json.loads(p.read_text(encoding="utf-8")) for p in ordner.glob("*.json")]


# ── Warteschlange ─────────────────────────────────────────────────────────────

def test_zweite_einlieferung_legt_keine_zweite_kopie_an(leer):
    a = held_mails.hold("max@firma.de", ["erika@beispiel.de"], _mail(), reason="delivery_failed")
    b = held_mails.hold("max@firma.de", ["Erika@Beispiel.de"], _mail(koerper="neu signiert"),
                        reason="delivery_failed")
    assert b == a and len(_eintraege(leer)) == 1


def test_nur_neue_empfaenger_werden_nachgereiht(leer):
    held_mails.hold("max@firma.de", ["erika@beispiel.de"], _mail(), reason="delivery_failed")
    held_mails.hold("max@firma.de", ["erika@beispiel.de", "eva@beispiel.de"], _mail(),
                    reason="delivery_failed")
    an = sorted(a for e in _eintraege(leer) for a in e["to_addrs"])
    assert an == ["erika@beispiel.de", "eva@beispiel.de"] and len(_eintraege(leer)) == 2


def test_zustellung_traegt_wartende_kopie_aus(leer):
    held_mails.hold("max@firma.de", ["erika@beispiel.de", "eva@beispiel.de"], _mail(),
                    reason="delivery_failed")
    assert held_mails.merke_zugestellt(_mail(), ["erika@beispiel.de"]) == 1
    (e,) = _eintraege(leer)
    assert e["to_addrs"] == ["eva@beispiel.de"]                  # gekürzt
    assert held_mails.merke_zugestellt(_mail(), ["EVA@beispiel.de"]) == 1
    assert _eintraege(leer) == []                                # entfernt


def test_nach_zustellung_wird_nicht_mehr_eingereiht(leer):
    """Umgekehrte Reihenfolge: Der erste Versuch kam durch, die Antwort ging
    verloren, der zweite scheitert — er darf keine Kopie hinterlassen."""
    held_mails.merke_zugestellt(_mail(), ["erika@beispiel.de"])
    r = held_mails.hold("max@firma.de", ["erika@beispiel.de"], _mail(), reason="delivery_failed")
    assert r == "bereits-zugestellt" and _eintraege(leer) == []


def test_zugestellt_merken_laeuft_ab(leer, monkeypatch):
    held_mails.merke_zugestellt(_mail(), ["erika@beispiel.de"])
    jetzt = held_mails.time.monotonic()
    monkeypatch.setattr(held_mails.time, "monotonic",
                        lambda: jetzt + held_mails._ZUGESTELLT_FENSTER_S + 1)
    held_mails.hold("max@firma.de", ["erika@beispiel.de"], _mail(), reason="delivery_failed")
    assert len(_eintraege(leer)) == 1


@pytest.mark.parametrize("zweite", [
    _mail(datum="Thu, 12 Nov 2026 20:41:00 +0100"),   # Gerät mit fester Message-ID
    _mail(mid="<b2@firma.de>"),                        # andere Nachricht
])
def test_andere_nachricht_wird_nicht_zusammengelegt(leer, zweite):
    held_mails.hold("max@firma.de", ["erika@beispiel.de"], _mail(), reason="delivery_failed")
    held_mails.hold("max@firma.de", ["erika@beispiel.de"], zweite, reason="delivery_failed")
    assert len(_eintraege(leer)) == 2
    held_mails.merke_zugestellt(zweite, ["erika@beispiel.de"])
    assert len(_eintraege(leer)) == 1


@pytest.mark.parametrize("roh", [_mail(mid=""), _mail(datum="")])
def test_ohne_kennung_keine_entdoppelung(leer, roh):
    held_mails.hold("max@firma.de", ["erika@beispiel.de"], roh, reason="delivery_failed")
    held_mails.hold("max@firma.de", ["erika@beispiel.de"], roh, reason="delivery_failed")
    assert len(_eintraege(leer)) == 2
    assert held_mails.merke_zugestellt(roh, ["erika@beispiel.de"]) == 0
    assert len(_eintraege(leer)) == 2


def test_wartungsmodus_bleibt_unberuehrt(leer):
    held_mails.hold("max@firma.de", ["erika@beispiel.de"], _mail())          # maintenance
    held_mails.hold("max@firma.de", ["erika@beispiel.de"], _mail())
    held_mails.merke_zugestellt(_mail(), ["erika@beispiel.de"])
    assert len(_eintraege(leer)) == 2


# ── send(): wer zugestellt wurde, wird vermerkt ───────────────────────────────

def test_send_vermerkt_nur_tatsaechlich_zugestellte(leer, monkeypatch):
    def _senden(von, an, roh, force_mime=False, queue=True):
        reinject._fail_delivery(von, ["eva@beispiel.de"], roh, "test", queue)
    monkeypatch.setattr(reinject, "_senden", _senden)
    reinject.send("max@firma.de", ["erika@beispiel.de", "eva@beispiel.de"], _mail())
    (e,) = _eintraege(leer)
    assert e["to_addrs"] == ["eva@beispiel.de"]
    assert held_mails._bereits_zugestellt(held_mails._kennung(_mail()), "erika@beispiel.de",
                                          held_mails.time.monotonic())
    assert not held_mails._bereits_zugestellt(held_mails._kennung(_mail()), "eva@beispiel.de",
                                              held_mails.time.monotonic())


def test_erfolgreiche_zustellung_raeumt_frueheren_versuch_weg(leer, monkeypatch):
    """Der Ablauf vom 12.11.: erster Versuch eingereiht, zweiter zugestellt."""
    held_mails.hold("max@firma.de", ["erika@beispiel.de"], _mail(), reason="delivery_failed")
    monkeypatch.setattr(reinject, "_senden", lambda *a, **k: None)
    reinject.send("max@firma.de", ["erika@beispiel.de"], _mail(koerper="zweiter Versuch"))
    assert _eintraege(leer) == []


def test_gescheiterter_manueller_versuch_vermerkt_nichts(leer, monkeypatch):
    def _senden(von, an, roh, force_mime=False, queue=True):
        reinject._fail_delivery(von, an, roh, "test", queue)
    monkeypatch.setattr(reinject, "_senden", _senden)
    with pytest.raises(RuntimeError):
        reinject.send("max@firma.de", ["erika@beispiel.de"], _mail(), queue_on_failure=False)
    assert held_mails._zugestellt == {}


# ── Tenant-Weiterleitung: Wartezeit ───────────────────────────────────────────

def test_weiterleitung_wartet_laenger_als_der_rueckweg_des_ziels():
    # Das Ziel blockiert bis zu 30 s auf seinem Rückweg, reiht dann ein und
    # antwortet. Die Weiterleitung muss deutlich länger warten …
    assert reinject.ANTWORT_NACH_DATEN_S >= 4 * 30
    # … aber kürzer als die eigene Annahmeseite offen hält (aiosmtpd: 300 s ab DATA).
    from aiosmtpd.smtp import SMTP
    grenze = inspect.signature(SMTP.__init__).parameters["timeout"].default
    assert reinject.ANTWORT_NACH_DATEN_S < grenze - 30


def test_weiterleitung_setzt_die_wartezeit_vor_dem_senden(monkeypatch):
    import settings_store
    import aussenadresse
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: {
        "RELAY_TARGETS": {"gw2": {"host": "gw2.firma.de", "port": 25, "starttls": False}},
        "RELAY_TARGET_PW": {}}.get(k))
    monkeypatch.setattr(aussenadresse, "ehlo_hostname", lambda: "gw.test")
    ablauf = []

    class _SMTP:
        def __init__(self, host, port, timeout=None, local_hostname=None):
            ablauf.append(("verbinden", timeout))
            self.sock = self
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def ehlo(self): pass
        def settimeout(self, t): ablauf.append(("warten", t))
        def sendmail(self, *a): ablauf.append(("senden", None))
    monkeypatch.setattr(reinject.smtplib, "SMTP", _SMTP)
    reinject._relay_to_target("gw2", "a@x.de", ["b@y.de"], b"x", antwort_s=240)
    assert ablauf == [("verbinden", 30), ("warten", 240), ("senden", None)]


def test_tenant_weiterleitung_laeuft_im_thread_mit_langer_wartezeit():
    """Ein blockierender Aufruf in handle_DATA hielte jede andere Verbindung an."""
    import handler
    quelle = inspect.getsource(handler)
    stelle = quelle.split("if _tziel:\n", 1)[1].split("except Exception", 1)[0]
    assert "await asyncio.to_thread(" in stelle
    assert "reinject._relay_to_target" in stelle
    assert "antwort_s=reinject.ANTWORT_NACH_DATEN_S" in stelle
    # Aus send() heraus (synchron in der Ereignisschleife) bleibt es bei 30 s.
    assert inspect.signature(reinject._relay_to_target).parameters["antwort_s"].default == 30
