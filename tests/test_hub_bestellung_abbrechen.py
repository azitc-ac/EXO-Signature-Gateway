"""Offene Zertifikatsbestellung: abbrechen, Ende anzeigen, kein Hängen ohne Ende.

ANLASS (27.11.2026, Prod): Eine Certum-TEST-Bestellung vom 09.11. stand
18 Tage auf „Wartet auf Bestätigung durch den Postfachinhaber · seit 17:42 Uhr“.
Certum hatte die Bestätigung längst (ACCEPTED), nur der lokale Merker fehlte;
die Seite bot den Link an, und ihre Automatik schickte bei jedem Aufruf einen
Bestätigungs-POST, den Certum mit HTTP 400 beantwortete. Abbrechen ging nicht.

Seither: hub_orders.abbrechen (EIN Weg: Hub-Storno, lokal aufräumen, bei
Hub-Fehler nachholen), ACCEPTED zählt als bestätigt, Alter mit Datum,
Zeitlimit HAENGT_TAGE, Bestätigungsfehler und letztes Ende sichtbar.
"""
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import hub_client  # noqa: E402
import hub_orders  # noqa: E402

ADRESSE = "max.mustermann@firma.at"


@pytest.fixture
def ablage(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_orders, "_DIR", tmp_path)
    return tmp_path


def _bestellung(ablage, alter_tage=0, order_id="o1", **mehr):
    hub_orders.save_pending(order_id, ADRESSE, "certum_test", b"KEY", price_cents=1000)
    pfad = ablage / f"{order_id}.json"
    d = json.loads(pfad.read_text())
    d["created"] = (datetime.now(timezone.utc) - timedelta(days=alter_tage)).isoformat()
    d.update(mehr)
    pfad.write_text(json.dumps(d))
    return d


def _hub(monkeypatch, *, storno=None, order=None):
    aufrufe = {"storno": 0, "order": 0}

    async def cancel(oid):
        aufrufe["storno"] += 1
        return storno(oid) if callable(storno) else storno

    async def get(oid):
        aufrufe["order"] += 1
        return order(oid) if callable(order) else order
    monkeypatch.setattr(hub_client, "cert_cancel_order", cancel)
    monkeypatch.setattr(hub_client, "cert_get_order", get)
    return aufrufe


def _status():
    from webui.routen import smime
    r = asyncio.run(smime.api_hub_order_status(ADRESSE, user="admin"))
    return json.loads(r.body)


# ── Abbrechen ────────────────────────────────────────────────────────────────

def test_abbruch_waehrend_wartet_raeumt_auf_und_erlaubt_neue_bestellung(ablage, monkeypatch):
    import scheduler
    _bestellung(ablage)
    assert scheduler._bestellung_offen(ADRESSE)                # blockiert die Erneuerung
    _hub(monkeypatch, storno={"ok": True, "erstattet_cents": 1190})
    erg = asyncio.run(hub_orders.abbrechen("o1", actor="admin"))
    assert erg["ok"] and "Erstattet: 11.90 €" in erg["meldung"]
    assert not (ablage / "o1.json").exists() and not (ablage / "o1.key").exists()
    assert hub_orders.list_pending() == [] and not scheduler._bestellung_offen(ADRESSE)
    assert hub_orders.letztes_ende(ADRESSE)["art"] == "storniert"
    assert _status()["beendet"]["art"] == "storniert"
    # erneut bestellen: geht, und das gemerkte Ende ist überholt
    _bestellung(ablage, order_id="o2")
    assert [m["order_id"] for m in hub_orders.list_pending()] == ["o2"]
    assert hub_orders.letztes_ende(ADRESSE) is None


def test_abbruch_bei_hub_fehler_raeumt_lokal_auf_und_holt_nach(ablage, monkeypatch):
    _bestellung(ablage)
    antworten = [{"ok": False, "nachholen": True, "error": "Verbindungsfehler: weg"}]
    aufrufe = _hub(monkeypatch, storno=lambda oid: antworten[-1])
    erg = asyncio.run(hub_orders.abbrechen("o1"))
    assert erg["ok"] and erg["nachholen"] and "nachgeholt" in erg["meldung"]
    assert hub_orders.list_pending() == [] and hub_orders.stornos_offen() == 1
    # Hub weiter weg: bleibt vorgemerkt
    assert asyncio.run(hub_orders.stornos_nachholen()) == 0 and hub_orders.stornos_offen() == 1
    # Hub wieder da: nachgeholt, Vormerkung weg
    antworten.append({"ok": True, "erstattet_cents": 0})
    assert asyncio.run(hub_orders.stornos_nachholen()) == 1 and hub_orders.stornos_offen() == 0
    assert aufrufe["storno"] == 3


def test_abbruch_einer_inzwischen_ausgestellten_bestellung_holt_ab(ablage, monkeypatch):
    """Der Schlüssel liegt nur hier — ausgestellt heisst: einspielen, nicht verwerfen."""
    import smime_store
    _bestellung(ablage)
    _hub(monkeypatch, storno={"ok": False, "ende": True, "status": "issued"},
         order={"ok": True, "status": "issued", "cert_pem": "PEM"})
    eingespielt = []
    monkeypatch.setattr(smime_store, "store_pem_slot",
                        lambda e, c, k: eingespielt.append((e, k)) or {"slot_id": "s1"})
    monkeypatch.setattr(hub_orders, "_notify_issued", lambda *a: None)
    erg = asyncio.run(hub_orders.abbrechen("o1"))
    assert not erg["ok"] and "bereits ausgestellt" in erg["meldung"]
    assert eingespielt == [(ADRESSE, b"KEY")]


def test_hub_kennt_sie_nicht_mehr_lokal_trotzdem_weg(ablage, monkeypatch):
    _bestellung(ablage)
    _hub(monkeypatch, storno={"ok": False, "ende": True, "status": "rejected"})
    assert asyncio.run(hub_orders.abbrechen("o1"))["ok"]
    assert hub_orders.list_pending() == [] and hub_orders.stornos_offen() == 0


# ── Ende durch die Stelle, Zeitlimit ─────────────────────────────────────────

def test_ca_meldet_abgelehnt_raeumt_auf_und_zeigt_es(ablage, monkeypatch):
    _bestellung(ablage)
    _hub(monkeypatch, order={"ok": True, "status": "rejected", "note": "Order abgelaufen"})
    monkeypatch.setattr(hub_orders, "_notify_rejected", lambda *a: None)
    assert asyncio.run(hub_orders.poll_all()) == 1
    assert hub_orders.list_pending() == []
    s = _status()
    assert s["aktiv"] is False and s["beendet"]["art"] == "abgelehnt" and "abgelaufen" in s["beendet"]["text"]


def test_am_hub_storniert_wird_lokal_nachgezogen(ablage, monkeypatch):
    _bestellung(ablage)
    _hub(monkeypatch, order={"ok": True, "status": "storniert"})
    assert asyncio.run(hub_orders.poll_all()) == 1 and hub_orders.list_pending() == []


@pytest.mark.parametrize("tage,haengt", [(hub_orders.HAENGT_TAGE - 1, False), (hub_orders.HAENGT_TAGE, True)])
def test_zeitlimit_ohne_meldung_der_stelle(ablage, monkeypatch, tage, haengt):
    _bestellung(ablage, alter_tage=tage, bestaetigt_am="2026-01-01T00:00:00+00:00")
    _hub(monkeypatch, order={"ok": True, "status": "submitted", "ref": "R",
                             "ca_zustand": "Certum-Order-Status: ACCEPTED"})
    s = _status()
    assert s["aktiv"] and s["haengt"] is haengt and s["alter_tage"] == tage


# ── ACCEPTED zählt als bestätigt ─────────────────────────────────────────────

@pytest.mark.parametrize("antwort", [
    {"ok": True, "status": "submitted", "ref": "R", "ca_zustand": "Certum-Order-Status: ACCEPTED"},
    {"ok": True, "status": "submitted", "ref": "R", "ca_angenommen": True, "ca_zustand": "Certum HTTP 500"},
])
def test_angenommen_heisst_nicht_mehr_warten(ablage, monkeypatch, antwort):
    _bestellung(ablage)
    _hub(monkeypatch, order=antwort)

    async def kein_link(*a, **k):
        raise AssertionError("Postfach durchsucht, obwohl die Stelle schon angenommen hat")
    monkeypatch.setattr(hub_orders, "bestaetigungslink", kein_link)
    s = _status()
    assert s["wartet_auf_nutzer"] is False and s["bestaetigt_am"] and s["bestaetigungslink"] == ""


def test_bestaetigen_ruft_die_stelle_nicht_mehr_an_wenn_angenommen(ablage, monkeypatch):
    import ca_bestaetigung
    meta = _bestellung(ablage)
    _hub(monkeypatch, order={"ok": True, "status": "submitted", "ref": "R",
                             "ca_zustand": "Certum-Order-Status: ACCEPTED"})

    async def nie(link):
        raise AssertionError("Bestätigungs-POST trotz ACCEPTED")
    monkeypatch.setattr(ca_bestaetigung, "bestaetigen", nie)
    ok, meldung = asyncio.run(hub_orders.bestaetigen(meta))
    assert ok and "angenommen" in meldung
    assert json.loads((ablage / "o1.json").read_text()).get("bestaetigt_am")


def test_gescheiterte_bestaetigung_wird_angezeigt(ablage, monkeypatch):
    import ca_bestaetigung
    meta = _bestellung(ablage)
    _hub(monkeypatch, order={"ok": True, "status": "submitted", "ref": "R",
                             "ca_zustand": "Certum-Order-Status: VERIFICATION"})

    async def link(*a, **k):
        return "https://ca.example/bestaetigen"
    monkeypatch.setattr(hub_orders, "bestaetigungslink", link)

    async def abgelehnt(lnk):
        return False, "abgelehnt (HTTP 400)"
    monkeypatch.setattr(ca_bestaetigung, "bestaetigen", abgelehnt)
    asyncio.run(hub_orders.bestaetigen(meta))
    assert _status()["bestaetigung_fehler"] == "abgelehnt (HTTP 400)"


# ── hub_client: Antworten des Hubs richtig einordnen ─────────────────────────

class _Antwort:
    def __init__(self, code, daten=None):
        self.status_code = code
        self._daten = daten
        self.headers = {"content-type": "application/json"} if daten is not None else {}
        self.text = ""

    def json(self):
        return self._daten


@pytest.mark.parametrize("code,daten,erwartet", [
    (200, {"ok": True, "erstattet_cents": 1190}, {"ok": True, "erstattet_cents": 1190}),
    (409, {"ok": False, "status": "issued"}, {"ok": False, "ende": True, "status": "issued"}),
    (404, {"detail": "Order nicht gefunden."}, {"ok": False, "ende": True, "status": "unbekannt"}),
    (404, {"detail": "Not Found"}, "nachholen"),           # Hub zu alt: Endpunkt fehlt
    (405, {"detail": "Method Not Allowed"}, "nachholen"),
    (500, None, "nachholen"),
])
def test_cert_cancel_order_ordnet_antworten_ein(monkeypatch, code, daten, erwartet):
    import httpx
    monkeypatch.setattr(hub_client, "_base", lambda: "https://hub.example")
    monkeypatch.setattr(hub_client, "_cert_key", lambda: "k")

    class Klient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None):
            assert url == "https://hub.example/api/cert/order/o1/stornieren"
            return _Antwort(code, daten)
    monkeypatch.setattr(httpx, "AsyncClient", Klient)
    erg = asyncio.run(hub_client.cert_cancel_order("o1"))
    if erwartet == "nachholen":
        assert erg["ok"] is False and erg["nachholen"] is True
    else:
        assert erg == erwartet


# ── Anzeige (Node): Rückfrage nennt die Erstattung, Hängen/Ende/Fehler sichtbar ──

def _js_funktion(text: str, name: str) -> str:
    import re
    m = re.search(r"(?:async\s+)?function\s+" + name + r"\s*\(", text)
    assert m, name
    i, tiefe = text.index("{", m.end()), 0
    for j in range(i, len(text)):
        tiefe += (text[j] == "{") - (text[j] == "}")
        if tiefe == 0:
            return text[m.start():j + 1]
    raise AssertionError(name)


@pytest.mark.skipif(not __import__("shutil").which("node"), reason="node fehlt")
def test_anzeige_und_rueckfrage():
    import subprocess
    seite = (WURZEL / "app/webui/templates/smime.html").read_text(encoding="utf-8")
    skript = ("const esc = s => String(s); const anzahlText = (n, e, m) => n + ' ' + (n === 1 ? e : m);\n"
              + _js_funktion(seite, "_hubOrderText") + "\n" + _js_funktion(seite, "_stornoText") + """
const t = d => _hubOrderText(Object.assign({aktiv: true, email: 'max@firma.at'}, d));
const st = (storno, preis) => _stornoText({dataset: {storno, preis}});
console.log(JSON.stringify({
  haengt: t({haengt: true, alter_tage: 18, status: 'submitted', ca_zustand: 'Certum-Order-Status: ACCEPTED'}),
  ende: t({aktiv: false, beendet: {art: 'abgelehnt', text: 'Order abgelaufen', am: '2026-11-20T10:00:00Z'}}),
  fehler: t({wartet_auf_nutzer: true, status: 'submitted', bestaetigung_fehler: 'abgelehnt (HTTP 400)'}),
  voll: st('1190', '1000'), keine: st('0', '1000'), gratis: st('0', '0'), alt: st('', '1000'),
}));""")
    aus = __import__("json").loads(subprocess.run(["node", "-e", skript], capture_output=True,
                                                  text=True, check=True).stdout)
    assert aus["haengt"][1] == "unklar" and "18 Tagen" in aus["haengt"][0] and "abbrechen" in aus["haengt"][0]
    assert aus["ende"][1] == "fehler" and "abgelaufen" in aus["ende"][0] and "Neu bestellen" in aus["ende"][0]
    assert "abgelehnt (HTTP 400)" in aus["fehler"][0]
    assert aus["voll"] == "Erstattet werden 11,90 €."
    assert aus["keine"].startswith("Keine Erstattung")
    assert aus["gratis"] == "Die Bestellung war kostenlos."
    assert "ältere Fassung" in aus["alt"]


def test_knopf_und_rueckfrage_in_der_seite():
    seite = (WURZEL / "app/webui/templates/smime.html").read_text(encoding="utf-8")
    rumpf = _js_funktion(seite, "hubOrderAbbrechen")
    assert "confirm(" in rumpf and "_stornoText(" in rumpf
    assert "'/api/smime/hub-order/' + encodeURIComponent(email) + '/abbrechen'" in rumpf
    assert "Bestellung abbrechen</button>" in _js_funktion(seite, "_renderHubOrder")
