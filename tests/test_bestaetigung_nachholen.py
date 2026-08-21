"""Die Bestätigung der E-Mail-Adresse läuft ohne offenen Browser.

ANLASS (21.08.2026): Eine Zertifizierungsstelle stellt erst aus, wenn jemand
den Link in ihrer Bestätigungsmail angeklickt hat. Das Gateway kann das
übernehmen — bis zu diesem Tag allerdings nur, während eine S/MIME-Seite im
Browser offen war: Deren Fortschrittsabruf sah den Wartezustand und stiess die
Bestätigung an.

Für eine einzelne Bestellung, bei der jemand zusieht, genügt das. Für alles
andere nicht:

  * Ein Sammellauf über hundert Postfächer wird nicht hundertfach angezeigt.
  * Beim automatischen Bestellen hat niemand eine Seite offen — dort unterblieb
    die Bestätigung IMMER.

Die Zusage „läuft ohne Zutun" war damit an eine Bedingung geknüpft, die
nirgends stand: dass der Betreiber den Browser offen lässt. Im Livelauf am
20.08.2026 blieben drei von vier Bestellungen genau so liegen.
"""
import json
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import hub_orders  # noqa: E402


@pytest.fixture
def offen(tmp_path, monkeypatch):
    """Zwei offene Bestellungen, eine mit automatischer Bestätigung."""
    monkeypatch.setattr(hub_orders, "_DIR", tmp_path)
    for order_id, email in (("o1", "mit@x.de"), ("o2", "ohne@x.de")):
        (tmp_path / f"{order_id}.json").write_text(json.dumps({
            "order_id": order_id, "email": email, "provider": "certum_test",
            "created": "2026-08-20T10:00:00+00:00", "source": "hub"}))

    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: {
        "mit@x.de": {"auto_confirm": True, "backend": "hub:certum_test"},
        "ohne@x.de": {"auto_confirm": False},
    } if k == "CA_USER_CONFIG" else None)

    aufgerufen = []
    import ca_bestaetigung
    import hub_client

    async def _order(order_id):
        return {"ok": True, "status": "submitted", "ref": "REF-" + order_id}

    async def _link(email, ref, seit=""):
        return f"https://ca.example/verify?id={email}"

    async def _bestaetigen(link):
        aufgerufen.append(link)
        return True, "bestätigt (HTTP 202)"

    monkeypatch.setattr(hub_client, "cert_get_order", _order)
    monkeypatch.setattr(hub_orders, "bestaetigungslink", _link)
    monkeypatch.setattr(ca_bestaetigung, "bestaetigen", _bestaetigen)
    return tmp_path, aufgerufen


def _lauf():
    import asyncio
    return asyncio.run(hub_orders.bestaetigungen_nachholen())


def test_bestaetigt_nur_wo_es_eingeschaltet_ist(offen):
    """⚠️ Die Bestätigung ersetzt die Absicht der Zertifizierungsstelle, dass
    ein Mensch zustimmt. Wo der Betreiber sie nicht erteilt hat, wird sie nicht
    genommen."""
    _tmp, aufgerufen = offen
    assert _lauf() == 1
    assert len(aufgerufen) == 1
    assert "mit@x.de" in aufgerufen[0]


def test_zweiter_durchgang_bestaetigt_nicht_erneut(offen):
    """⚠️ Der Zeitplaner läuft im Minutentakt. Ohne Merkposten ginge bei jedem
    Durchgang ein weiterer Aufruf an die Zertifizierungsstelle — für jede
    offene Bestellung, dauerhaft."""
    _tmp, aufgerufen = offen
    assert _lauf() == 1
    assert _lauf() == 0, "hat ein zweites Mal bestätigt"
    assert len(aufgerufen) == 1


def test_merkposten_steht_in_der_metadatei(offen):
    tmp, _ = offen
    _lauf()
    daten = json.loads((tmp / "o1.json").read_text())
    assert daten.get("bestaetigt_am"), "kein Merkposten geschrieben"
    assert daten["order_id"] == "o1", "übrige Angaben verloren"


def test_ohne_wartezustand_wird_nicht_angeklopft(offen, monkeypatch):
    """Ein Aufruf „auf Verdacht" wäre ein Zugriff auf ein fremdes System ohne
    Anlass — und bei einer bereits ausgestellten Bestellung sinnlos."""
    _tmp, aufgerufen = offen
    import hub_client

    async def _fertig(order_id):
        return {"ok": True, "status": "issued", "ref": "R"}
    monkeypatch.setattr(hub_client, "cert_get_order", _fertig)
    assert _lauf() == 0
    assert not aufgerufen


def test_fehlende_mail_bleibt_fuer_den_naechsten_durchgang(offen, monkeypatch):
    """Die Bestätigungsmail braucht ein paar Sekunden. Wer dann aufgibt, müsste
    von Hand nachfassen — und niemand wüsste, dass es nötig ist."""
    tmp, aufgerufen = offen

    async def _keine(email, ref, seit=""):
        return ""
    monkeypatch.setattr(hub_orders, "bestaetigungslink", _keine)
    assert _lauf() == 0
    assert not json.loads((tmp / "o1.json").read_text()).get("bestaetigt_am"), (
        "als erledigt vermerkt, obwohl nichts bestätigt wurde")


def test_ein_fehlschlag_nimmt_die_uebrigen_nicht_mit(offen, monkeypatch):
    """Bei hundert Bestellungen darf ein hängendes Postfach nicht die anderen
    neunundneunzig blockieren."""
    tmp, aufgerufen = offen
    (tmp / "o3.json").write_text(json.dumps({
        "order_id": "o3", "email": "mit@x.de", "provider": "certum_test",
        "created": "2026-08-20T11:00:00+00:00", "source": "hub"}))
    import hub_client
    erste = {"n": 0}

    async def _mal_so(order_id):
        erste["n"] += 1
        if erste["n"] == 1:
            raise RuntimeError("Gegenstelle antwortet nicht")
        return {"ok": True, "status": "submitted", "ref": "R"}
    monkeypatch.setattr(hub_client, "cert_get_order", _mal_so)
    assert _lauf() == 1, "der zweite Vorgang wurde mit abgeräumt"


# ── Der Zeitplaner muss sie auch tatsächlich rufen ───────────────────────────

def test_zeitplaner_bestaetigt_vor_dem_abfragen(monkeypatch):
    """⚠️ Ohne diesen Test wäre die ganze Funktion tot, sobald jemand die eine
    Zeile im Zeitplaner entfernt — und nichts würde fehlschlagen. Genau die
    Klasse Fehler, die hier zweimal zwölf Tage unbemerkt lief.

    Die Reihenfolge zählt: Erst bestätigen, dann abfragen. Andersherum fände
    das Abfragen nichts Neues und die Bestellung bliebe bis zum nächsten
    Durchgang liegen.
    """
    import scheduler
    import hub_client
    import hub_catalog
    import hub_orders as ho

    ablauf = []

    async def _refresh():
        pass

    async def _nachholen():
        ablauf.append("bestaetigen")
        return 1

    monkeypatch.setattr(hub_client, "cert_is_registered", lambda: True)
    monkeypatch.setattr(hub_catalog, "refresh", _refresh)
    monkeypatch.setattr(ho, "list_pending", lambda: [{"order_id": "o1"}])
    monkeypatch.setattr(ho, "bestaetigungen_nachholen", _nachholen)
    monkeypatch.setattr(ho, "poll_all_sync", lambda: ablauf.append("abfragen") or 0)

    scheduler._poll_hub_orders()
    assert ablauf == ["bestaetigen", "abfragen"], (
        f"Zeitplaner ruft nicht beides in dieser Reihenfolge: {ablauf}")
