"""Notbetrieb Dauerbetrieb 3b: Änderungen der Kopie zurück nach Exchange.

Zusagen: gelesen und verschieben (löschen = nach „Gelöschte Elemente“) wirken
sofort in der Kopie und gehen danach nach Exchange; das Jüngere gewinnt;
nur wer im Webmail arbeitet, schreibt zurück; Verschieben übernimmt die neue
Exchange-Kennung; der Abgleich überschreibt keine wartende Änderung; ein
Fehler bleibt sichtbar in der Warteschlange.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import notbetrieb
import notbetrieb_aenderungen as na
from test_notbetrieb import nb  # noqa: F401

PF = "a@firma.at"


class _Resp:
    def __init__(self, status=200, data=None):
        self.status_code, self._d = status, data or {}
        self.text = str(data)

    def json(self):
        return self._d


class Exchange:
    """Nachrichten mit Änderungszeit; PATCH/move schreiben mit."""

    def __init__(self):
        self.mails: dict[str, dict] = {}
        self.aufrufe: list[tuple] = []
        self.kaputt = False
        self.imid: dict[str, str] = {}          # internetMessageId → id

    def client(self, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None, params=None):
        self.aufrufe.append(("GET", url))
        if params and "$filter" in params:
            imid = params["$filter"].split("'", 1)[1].rsplit("'", 1)[0]
            return _Resp(200, {"value": [{"id": self.imid[imid]}] if imid in self.imid else []})
        mid = url.rsplit("/", 1)[1]
        if mid not in self.mails:
            return _Resp(404)
        # Wie Exchange: isRead ändert lastModifiedDateTime NICHT (gemessen 15.11.).
        return _Resp(200, {"lastModifiedDateTime": self.mails[mid]["geaendert"],
                           "isRead": self.mails[mid].get("isRead", False)})

    def patch(self, url, json=None, headers=None):
        self.aufrufe.append(("PATCH", url, json))
        if self.kaputt:
            return _Resp(500)
        self.mails[url.rsplit("/", 1)[1]]["isRead"] = json["isRead"]
        return _Resp(200, {})

    def post(self, url, json=None, headers=None):
        self.aufrufe.append(("POST", url, json))
        alt = url.rsplit("/", 2)[-2]
        neu = alt + "-verschoben"
        self.mails[neu] = {**self.mails.pop(alt), "ordner": json["destinationId"]}
        return _Resp(201, {"id": neu})


@pytest.fixture
def ex(nb, monkeypatch):  # noqa: F811
    nb["NOTBETRIEB_LESEN_ALLE"] = True
    nb["NOTBETRIEB_SENDEN_ALLE"] = True                             # a@ arbeitet im Webmail
    e = Exchange()
    monkeypatch.setattr(na.httpx, "Client", e.client)
    monkeypatch.setattr(na, "bald", lambda: None)
    with notbetrieb._lock, notbetrieb._conn() as conn:
        conn.executemany("INSERT INTO ordner (postfach, id, eltern, name, wk) VALUES (?,?,?,?,?)",
                         [(PF, "ord-in", "root", "Posteingang", "inbox"), (PF, "ord-arch", "root", "Archiv", None),
                          (PF, "ord-del", "root", "Gelöschte Elemente", "deleteditems")])
    return e


def _zeile(gid="g1", herkunft=notbetrieb.EXO, zurueck=None, exo_id=None, imid="<m1@x>"):
    with notbetrieb._lock, notbetrieb._conn() as conn:
        conn.execute("INSERT INTO mails (postfach, graph_id, ordner, imid, empfangen, gelesen, datei, herkunft,"
                     " zurueck, exo_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (PF, gid, "ord-in", imid, "2026-11-15T08:00:00+00:00", 0, "/x", herkunft, zurueck, exo_id))


def _mail(gid="g1"):
    with notbetrieb._lock, notbetrieb._conn() as conn:
        z = conn.execute("SELECT * FROM mails WHERE postfach=? AND graph_id LIKE ?", (PF, gid + "%")).fetchone()
    return dict(z) if z else None


def _vorher():
    return (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


def test_gelesen_und_verschieben_gehen_nach_exchange(ex):
    _zeile()
    ex.mails["g1"] = {"geaendert": _vorher(), "isRead": False}
    assert na.merken(PF, PF, "g1", na.GELESEN, "1")
    assert na.merken(PF, PF, "g1", na.VERSCHIEBEN, "ord-arch")
    assert _mail()["gelesen"] == 1 and _mail()["ordner"] == "ord-arch"   # sofort in der Kopie
    assert na.offen()["offen"] == 2
    z = na.zurueckschreiben()
    assert z["geschrieben"] == 1 and z["offen"] == 0
    assert ("PATCH", f"{notbetrieb.GRAPH}/users/{PF}/messages/g1", {"isRead": True}) in ex.aufrufe
    assert ex.mails["g1-verschoben"]["ordner"] == "ord-arch" and ex.mails["g1-verschoben"]["isRead"]
    assert _mail()["graph_id"] == "g1-verschoben"                   # neue Kennung übernommen


def test_exchange_juenger_gewinnt(ex):
    _zeile()
    na.merken(PF, PF, "g1", na.GELESEN, "1")
    ex.mails["g1"] = {"geaendert": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                      "isRead": False}
    z = na.zurueckschreiben()
    assert z["verworfen"] == 1 and not any(a[0] == "PATCH" for a in ex.aufrufe)
    assert na.offen()["offen"] == 0


def test_gelesen_in_exchange_geaendert_gewinnt_ohne_neue_zeit(ex):
    """Live 15.11.: Gelesen ändert in Exchange weder lastModifiedDateTime noch
    changeKey — die Zeitprüfung allein überschrieb Exchange mit dem lokalen Stand."""
    _zeile()                                                         # Kopie: ungelesen (= Exchange beim Abgleich)
    ex.mails["g1"] = {"geaendert": _vorher(), "isRead": False}
    na.merken(PF, PF, "g1", na.GELESEN, "1")                         # lokal gelesen …
    ex.mails["g1"]["isRead"] = True                                  # … Exchange inzwischen auch, Zeit unverändert
    na.merken(PF, PF, "g1", na.GELESEN, "0")                         # lokal wieder ungelesen: vorher bleibt 0
    ex.mails["g1"]["isRead"] = True
    z = na.zurueckschreiben()
    assert z["verworfen"] == 1 and not any(a[0] == "PATCH" for a in ex.aufrufe)
    assert ex.mails["g1"]["isRead"] is True and _mail()["gelesen"] == 1   # Kopie übernimmt Exchange sofort
    assert na.offen()["offen"] == 0


def test_gelesen_ohne_aenderung_in_exchange_wird_geschrieben(ex):
    _zeile()
    ex.mails["g1"] = {"geaendert": _vorher(), "isRead": False}
    na.merken(PF, PF, "g1", na.GELESEN, "1")
    na.merken(PF, PF, "g1", na.GELESEN, "0")                         # zweimal geklickt ist keine Exchange-Änderung
    na.merken(PF, PF, "g1", na.GELESEN, "1")
    assert na.zurueckschreiben()["geschrieben"] == 1 and ex.mails["g1"]["isRead"] is True


def test_beide_seiten_gleich_geaendert_ist_nicht_verworfen(ex):
    _zeile()
    ex.mails["g1"] = {"geaendert": _vorher(), "isRead": False}
    na.merken(PF, PF, "g1", na.GELESEN, "1")
    ex.mails["g1"]["isRead"] = True                                  # in Outlook ebenfalls gelesen
    z = na.zurueckschreiben()
    assert z["verworfen"] == 0 and z["geschrieben"] == 1 and not any(a[0] == "PATCH" for a in ex.aufrufe)


def test_bestandszeile_ohne_vorher_wird_geschrieben(ex):
    _zeile()
    ex.mails["g1"] = {"geaendert": _vorher(), "isRead": True}
    with notbetrieb._lock, notbetrieb._conn() as conn:
        na._tabelle(conn)
        conn.execute("INSERT INTO aenderungen (postfach, graph_id, art, wert, ts) VALUES (?,?,?,?,?)",
                     (PF, "g1", na.GELESEN, "0", na._jetzt()))
    assert na.zurueckschreiben()["geschrieben"] == 1 and ex.mails["g1"]["isRead"] is False


def test_in_exchange_weg(ex):
    _zeile()
    na.merken(PF, PF, "g1", na.GELESEN, "1")
    assert na.zurueckschreiben()["verworfen"] == 1


def test_nur_wer_im_webmail_arbeitet_schreibt_zurueck(ex, nb):  # noqa: F811
    nb["NOTBETRIEB_SENDEN_ALLE"] = False                            # reine Vorschau
    _zeile()
    assert na.merken(PF, PF, "g1", na.GELESEN, "1")
    assert _mail()["gelesen"] == 1 and na.offen()["offen"] == 0     # nur in der Kopie


def test_lokale_post_vor_dem_rueckspielen_braucht_nichts(ex):
    _zeile(gid="lokal:1", herkunft=notbetrieb.LOKAL)
    na.merken(PF, PF, "lokal:1", na.VERSCHIEBEN, "ord-arch")
    assert na.zurueckschreiben()["geschrieben"] == 1 and ex.aufrufe == []
    assert _mail("lokal:1")["ordner"] == "ord-arch"                 # das Rückspielen nimmt das mit


def test_lokale_post_nach_dem_rueckspielen_ueber_exo_id(ex):
    _zeile(gid="lokal:2", herkunft=notbetrieb.LOKAL, zurueck="eingespielt x", exo_id="e9")
    ex.mails["e9"] = {"geaendert": _vorher(), "isRead": False}
    na.merken(PF, PF, "lokal:2", na.GELESEN, "1")
    na.zurueckschreiben()
    assert ex.mails["e9"]["isRead"] is True


def test_lokale_post_ohne_exo_id_ueber_message_id(ex):
    _zeile(gid="lokal:3", herkunft=notbetrieb.LOKAL, zurueck="exchange_kopie x", imid="<s1@firma.at>")
    ex.imid["<s1@firma.at>"] = "e7"
    ex.mails["e7"] = {"geaendert": _vorher(), "isRead": False}
    na.merken(PF, PF, "lokal:3", na.VERSCHIEBEN, "ord-del")
    na.zurueckschreiben()
    assert ex.mails["e7-verschoben"]["ordner"] == "ord-del"
    assert _mail("lokal:3")["exo_id"] == "e7-verschoben"


def test_fehler_bleibt_sichtbar_in_der_schlange(ex):
    _zeile()
    ex.mails["g1"] = {"geaendert": _vorher(), "isRead": False}
    ex.kaputt = True
    na.merken(PF, PF, "g1", na.GELESEN, "1")
    assert na.zurueckschreiben()["fehler"] == 1
    assert na.offen() == {"offen": 1, "mit_fehler": 1}
    ex.kaputt = False
    assert na.zurueckschreiben()["geschrieben"] == 1 and na.offen()["offen"] == 0


def test_abgleich_ueberschreibt_wartende_aenderung_nicht(ex):
    _zeile()
    na.merken(PF, PF, "g1", na.GELESEN, "1")

    class _Delta:
        def get(self, url, headers=None):
            return _Resp(200, {"value": [{"id": "g1", "isRead": False, "receivedDateTime": "2026-11-15T08:00:00Z",
                                          "internetMessageId": "<m1@x>"}], "@odata.deltaLink": "ende"})
    notbetrieb._abgleich_ordner(_Delta(), "tok", PF, "ord-in", 3, {"neu": 0, "entfernt": 0, "fehler": 0})
    assert _mail()["gelesen"] == 1
    # Ohne wartende Änderung gilt Exchange.
    with notbetrieb._lock, notbetrieb._conn() as conn:
        conn.execute("DELETE FROM aenderungen")
    notbetrieb._abgleich_ordner(_Delta(), "tok", PF, "ord-in", 3, {"neu": 0, "entfernt": 0, "fehler": 0})
    assert _mail()["gelesen"] == 0


def test_abgleich_schreibt_zuerst_zurueck(ex, nb, monkeypatch):  # noqa: F811
    aufrufe = []
    monkeypatch.setattr(na, "zurueckschreiben", lambda: aufrufe.append(1) or {})
    monkeypatch.setattr(notbetrieb, "_postfaecher", lambda: [])
    notbetrieb.abgleich_alle()
    assert aufrufe == [1]
    nb["NOTBETRIEB_AUSGERUFEN"] = True
    notbetrieb.abgleich_alle()
    assert aufrufe == [1]
