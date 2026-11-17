"""Eigene Hosts-Einträge (namensaufloesung.py): Name im Gateway → feste Adresse.

Anlass (17.11.2026): Ein On-Prem-Ziel löste nur öffentlich auf; die Probe lief
über die Firewall zurück ins eigene Netz. Mit einem Eintrag geht die
Verbindung direkt, und TLS prüft weiter gegen den Namen.
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import namensaufloesung as na  # noqa: E402
import settings_store  # noqa: E402


@pytest.mark.parametrize("text,eintraege", [
    ("10.99.0.25  mail.firma.at", {"mail.firma.at": "10.99.0.25"}),
    ("10.99.0.25 Mail.Firma.AT. smtp.firma.at   # Exchange on-prem\n\n# nur Kommentar",
     {"mail.firma.at": "10.99.0.25", "smtp.firma.at": "10.99.0.25"}),
    ("fd00::25 mail.firma.at", {"mail.firma.at": "fd00::25"}),
    ("10.99.0.25 mail.firma.at\n10.99.0.25 mail.firma.at", {"mail.firma.at": "10.99.0.25"}),
])
def test_zerlegen_gueltig(text, eintraege):
    assert na.zerlegen(text) == (eintraege, [])


@pytest.mark.parametrize("text,stichwort", [
    ("10.99.0 mail.firma.at", "keine IP-Adresse"),
    ("10.99.0.25", "fehlt der Name"),
    ("10.99.0.25 mail_firma.at", "kein gültiger Name"),
    ("10.99.0.25 -mail.firma.at", "kein gültiger Name"),
    ("10.99.0.25 mail.firma.at\n10.99.0.26 mail.firma.at", "schon mit 10.99.0.25"),
    ("10.99.0.25 a\n" * 201, "Höchstens"),
])
def test_zerlegen_meldet_jede_ungueltige_zeile(text, stichwort):
    _, fehler = na.zerlegen(text)
    assert fehler and stichwort in fehler[0]
    with pytest.raises(ValueError):
        na.normalisieren(text)


def test_normalisieren_eine_zeile_je_adresse():
    assert na.normalisieren("10.99.0.25 Mail.firma.at\n10.99.0.26 dc01.firma.local\n10.99.0.25 smtp.firma.at") == (
        "10.99.0.25  mail.firma.at smtp.firma.at\n10.99.0.26  dc01.firma.local")


@pytest.fixture
def aufloesung(monkeypatch):
    """Der Haken vor socket.getaddrinfo — das ursprüngliche Auflösen wird
    mitgeschrieben statt ins Netz zu gehen."""
    gefragt = []

    def original(host, port, *a, **k):
        gefragt.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (str(host), port))]
    monkeypatch.setattr(na, "_original", original)
    monkeypatch.setattr(socket, "getaddrinfo", socket.getaddrinfo)   # nach dem Test zurück
    werte = {na.SCHLUESSEL: "10.99.0.25  mail.firma.at"}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: werte.get(k, d))
    na.einschalten()
    return gefragt, werte


def test_eintrag_geht_vor_dns_name_ohne_gross_und_punkt(aufloesung):
    gefragt, _ = aufloesung
    assert socket.getaddrinfo("Mail.Firma.AT.", 25)[0][4] == ("10.99.0.25", 25)
    assert socket.getaddrinfo(b"mail.firma.at", 25)[0][4] == ("10.99.0.25", 25)
    assert gefragt == ["10.99.0.25", "10.99.0.25"]


def test_andere_namen_gehen_unveraendert_ans_dns(aufloesung):
    gefragt, _ = aufloesung
    socket.getaddrinfo("login.microsoftonline.com", 443)
    socket.getaddrinfo(None, 25)
    assert gefragt == ["login.microsoftonline.com", None]


def test_wirkt_sofort_nach_dem_speichern(aufloesung):
    gefragt, werte = aufloesung
    socket.getaddrinfo("mail.firma.at", 25)
    werte[na.SCHLUESSEL] = ""
    socket.getaddrinfo("mail.firma.at", 25)
    assert gefragt == ["10.99.0.25", "mail.firma.at"]


def test_speichern_prueft_und_normalisiert(monkeypatch):
    from fastapi.testclient import TestClient
    from webui import deps
    from webui.app import app
    gespeichert = {}
    monkeypatch.setattr(settings_store, "update", lambda d: gespeichert.update(d))
    app.dependency_overrides[deps._require_admin] = lambda: "admin"
    try:
        c = TestClient(app)
        r = c.post("/settings", json={"HOSTS_EINTRAEGE": "10.99.0 mail.firma.at"})
        assert r.status_code == 400 and "keine IP-Adresse" in r.json()["error"]
        assert "HOSTS_EINTRAEGE" not in gespeichert
        r = c.post("/settings", json={"HOSTS_EINTRAEGE": "10.99.0.25 Mail.Firma.at  # on-prem"})
        assert r.status_code == 200 and gespeichert["HOSTS_EINTRAEGE"] == "10.99.0.25  mail.firma.at"
    finally:
        app.dependency_overrides.pop(deps._require_admin, None)


def test_start_schaltet_ein():
    """main() legt den Haken vor der ersten Verbindung — sonst gälte die
    Tabelle nur, wenn zufällig jemand die Seite geöffnet hat."""
    quelle = (Path(__file__).resolve().parent.parent / "app" / "main.py").read_text(encoding="utf-8")
    main = quelle[quelle.index("def main() -> None:"):]
    assert main.index("namensaufloesung.einschalten()") < main.index("mail_audit.init_db()")
