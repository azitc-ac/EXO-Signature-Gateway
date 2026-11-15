"""Tägliche Direktversand-Probe: der Notfallweg nach außen, im Alltag geprüft.

Zusagen: einmal je Tag, nur außerhalb des Notfalls und nur mit Probe-Postfach;
genau der Notfallweg (domain_routing DIREKT → _an_postfix) mit Schleifenmarke
und Auto-Submitted; Ankunft, Ordner und spf/dkim/dmarc werden ausgewertet und
die Probe danach gelöscht; „ok“ in der Bereitschaft nur nach angekommener,
bestandener Probe — nicht mehr bloß „eingerichtet“.
"""
from __future__ import annotations

import email
from datetime import datetime, timedelta, timezone

import pytest

import notbetrieb_probe as np_
from test_notbetrieb import nb  # noqa: F401

PF = "probe@firma.at"


class _Resp:
    def __init__(self, status=200, data=None):
        self.status_code, self._d = status, data or {}

    def json(self):
        return self._d


class _Graph:
    def __init__(self):
        self.da, self.auth, self.ordner, self.aufrufe = False, "", "inbox", []

    def client(self, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None, params=None):
        self.aufrufe.append(("GET", url))
        # Wie Graph v1.0: kein wellKnownName am Ordner; „junkemail“ als Adresse.
        if "$select" in (params or {}) and "wellKnownName" in params["$select"]:
            return _Resp(400, {"error": "Could not find a property named 'wellKnownName'"})
        if url.endswith("/mailFolders/junkemail"):
            return _Resp(200, {"id": "f1" if self.ordner == "junkemail" else "f-junk"})
        if "/mailFolders/" in url:
            return _Resp(200, {"displayName": "Posteingang"})
        if not self.da:
            return _Resp(200, {"value": []})
        return _Resp(200, {"value": [{"id": "p1", "receivedDateTime": datetime.now(timezone.utc).isoformat(),
                                      "parentFolderId": "f1", "internetMessageHeaders": [
                                          {"name": "Authentication-Results", "value": self.auth}]}]})

    def delete(self, url, headers=None):
        self.aufrufe.append(("DELETE", url))
        return _Resp(204)


@pytest.fixture
def probe(nb, monkeypatch):  # noqa: F811
    import direktversand
    import domain_routing
    import reinject
    nb["NOTBETRIEB_PROBE_POSTFACH"] = PF
    monkeypatch.setattr(direktversand, "aktiv", lambda: True)
    monkeypatch.setattr(domain_routing, "aufloesen", lambda z: {"host": "postfix", "port": 25, "z": z})
    raus = []
    monkeypatch.setattr(reinject, "_an_postfix", lambda von, empf, inhalt, host, port: raus.append((von, empf, inhalt, host)))
    g = _Graph()
    monkeypatch.setattr(np_.httpx, "Client", g.client)
    return raus, g, nb


def test_faellig_nur_einmal_am_tag_und_nicht_im_notfall(probe):
    raus, _, nb = probe
    assert np_.faellig()
    np_.senden()
    assert not np_.faellig()
    nb[np_.KEY]["ts"] = (datetime.now(timezone.utc) - timedelta(hours=np_.TAKT_H + 1)).isoformat()
    assert np_.faellig()
    nb["NOTBETRIEB_AUSGERUFEN"] = True
    assert not np_.faellig()
    nb["NOTBETRIEB_AUSGERUFEN"] = False
    nb["NOTBETRIEB_PROBE_POSTFACH"] = ""
    assert not np_.faellig()


def test_senden_ueber_den_notfallweg_mit_schleifenmarke(probe):
    import domain_routing
    import loop_detector
    raus, _, _ = probe
    st = np_.senden()
    (von, empf, inhalt, host), = raus
    assert von == PF and empf == [PF] and host == "postfix" and st["zustand"] == "gesendet"
    msg = email.message_from_bytes(inhalt)
    assert loop_detector.marker_wert() in str(msg["X-Sig-Applied"])  # sonst holt die Transportregel sie zurück
    assert msg["Auto-Submitted"] == "auto-generated" and msg["Message-ID"] == st["mid"]
    assert domain_routing.DIREKT == "direkt"


def test_uebergabe_scheitert(probe, monkeypatch):
    import reinject
    monkeypatch.setattr(reinject, "_an_postfix", lambda *a: (_ for _ in ()).throw(OSError("Verbindung abgelehnt")))
    assert np_.senden()["zustand"] == "fehler"
    assert np_.bewertung()[0] == "fehler"


def test_angekommen_ausgewertet_und_geloescht(probe):
    _, g, _ = probe
    np_.senden()
    assert np_.pruefen()["zustand"] == "gesendet"                   # noch nicht da
    assert np_.bewertung()[0] == "warnung"                          # gesendet beweist noch nichts
    g.da, g.auth = True, "eop.example; spf=pass smtp.mailfrom=firma.at; dkim=pass header.d=firma.at; dmarc=pass"
    st = np_.pruefen()
    assert st["zustand"] == "angekommen" and st["auth"] == {"spf": "pass", "dkim": "pass", "dmarc": "pass"}
    assert ("DELETE", f"https://graph.microsoft.com/v1.0/users/{PF}/messages/p1") in g.aufrufe
    assert np_.bewertung()[0] == "ok"
    assert "angekommen nach " in np_.bewertung()[1] and " s (" in np_.bewertung()[1]   # unter 2 min in Sekunden


@pytest.mark.parametrize("auth, ordner, ampel", [
    ("spf=pass; dkim=fail; dmarc=fail", "inbox", "fehler"),
    ("spf=pass; dkim=pass; dmarc=pass", "junkemail", "warnung"),
    ("spf=pass", "inbox", "fehler"),                                 # fehlendes Ergebnis zählt nicht als pass
])
def test_bewertung_streng(probe, auth, ordner, ampel):
    _, g, _ = probe
    np_.senden()
    g.da, g.auth, g.ordner = True, auth, ordner
    np_.pruefen()
    assert np_.bewertung()[0] == ampel


def test_nicht_angekommen_wird_fehlt(probe):
    _, _, nb = probe
    np_.senden()
    nb[np_.KEY]["ts"] = (datetime.now(timezone.utc) - timedelta(minutes=np_.PRUEF_FRIST_MIN + 5)).isoformat()
    assert np_.pruefen()["zustand"] == "fehlt" and np_.bewertung()[0] == "fehler"


def test_alte_probe_ist_kein_ok(probe):
    _, g, nb = probe
    np_.senden()
    g.da, g.auth = True, "spf=pass; dkim=pass; dmarc=pass"
    np_.pruefen()
    nb[np_.KEY]["ts"] = (datetime.now(timezone.utc) - timedelta(hours=np_.TAKT_H + 4)).isoformat()
    assert np_.bewertung()[0] == "fehler"


def test_bereitschaft_ok_nur_nach_bestandener_probe(probe):
    import abnahme
    import notbetrieb_bereitschaft as nbb
    _, g, nb = probe
    assert nbb._versand()["zustand"] == abnahme.UNBEKANNT           # eingerichtet, aber noch nie geprüft
    np_.senden()
    g.da, g.auth = True, "spf=pass; dkim=pass; dmarc=pass"
    np_.pruefen()
    assert nbb._versand()["zustand"] == abnahme.OK
    nb[np_.KEY]["zustand"] = "fehlt"
    assert nbb._versand()["zustand"] == abnahme.OFFEN
    nb["NOTBETRIEB_PROBE_POSTFACH"] = ""
    assert nbb._versand()["zustand"] == abnahme.UNBEKANNT


def test_tagesbericht_zeile(probe):
    import notification
    zeilen = "".join(notification._notbetrieb_zeilen(lambda k: 0))
    assert "Direktversand-Probe" in zeilen and "noch keine Probe" in zeilen and "#e67e22" in zeilen
