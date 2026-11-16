"""Bereitschaft: Kennt ein Hybrid-Eingangsconnector die Ausgangsadresse des
Direktversands, ordnet Microsoft Notfall-Post an andere Microsoft-Kunden dem
EIGENEN (gestörten) Tenant zu. Gemessen 16.11.2026 auf der Testinstallation:
DIR:OUT, DKIM selector1 des Tenants — Ursache war ein OnPremises-Connector
mit der Gateway-IP. Alle Werte erfunden.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import abnahme  # noqa: E402
import config  # noqa: E402
import direktversand  # noqa: E402
import notbetrieb_bereitschaft as nbb  # noqa: E402
import notbetrieb_rechte  # noqa: E402
import settings_store  # noqa: E402

GW_IP = "203.0.113.25"
ROH = {"konten": [{"primary": "a@firma.at", "upn": "a@firma.at", "typ": "UserMailbox"}], "rechte": []}


@pytest.fixture
def lage(monkeypatch, tmp_path):
    werte = {"NOTBETRIEB_AKTIV": True, "DIREKT_ENABLED": True, "DIREKT_MODUS": "mx", "DIREKT_AUSSEN_IP": GW_IP}
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings_store, "get", lambda k, d=None, *a, **kw: werte.get(k, d))

    def abgleich(connectoren):
        roh = dict(ROH)
        if connectoren is not ...:
            roh["connectoren"] = connectoren
        notbetrieb_rechte.speichern(notbetrieb_rechte.parse(json.dumps(roh)))
    werte["_abgleich"] = abgleich
    return werte


def _con(name, ips=(), zert=""):
    return {"name": name, "ips": list(ips), "zertifikat": zert}


def test_ip_treffer_meldet_offen_mit_erklaerung(lage):
    lage["_abgleich"]([_con("Hybrid Inbound", [GW_IP]), _con("Anderer", zert="mail.firma.at")])
    p = nbb._hybrid()
    assert p["zustand"] == abnahme.OFFEN
    assert "„Hybrid Inbound“" in p["befund"] and f"({GW_IP})" in p["befund"]
    assert "dieselbe öffentliche IP" in p["befund"] and "deinem Tenant zugeordnet" in p["befund"]
    assert "Zertifikat um" in p["zu_tun"]


def test_cidr_bereich_trifft(lage):
    lage["_abgleich"]([_con("Hybrid Inbound", ["198.51.100.0/24", "203.0.113.0/26"])])
    p = nbb._hybrid()
    assert p["zustand"] == abnahme.OFFEN and "203.0.113.0/26" in p["befund"]


def test_kein_treffer_ok_mit_bezugsgroesse(lage):
    lage["_abgleich"]([_con("Hybrid Inbound", ["203.0.113.64/26"]), _con("Zert", zert="*.firma.at")])
    p = nbb._hybrid()
    assert p["zustand"] == abnahme.OK
    assert p["befund"].startswith("2 Hybrid-Eingangsconnectoren geprüft") and GW_IP in p["befund"]


def test_keine_connectoren_ist_ok_mit_null(lage):
    lage["_abgleich"]([])
    p = nbb._hybrid()
    assert p["zustand"] == abnahme.OK and p["befund"].startswith("0 Hybrid-Eingangsconnectoren")


def test_nie_gelesen_oder_nicht_lesbar_ist_unbekannt(lage):
    p = nbb._hybrid()                         # noch kein Abgleich
    assert p["zustand"] == abnahme.UNBEKANNT and "noch nicht gelesen" in p["befund"]
    lage["_abgleich"](...)                    # alte Ausgabe ohne Schlüssel
    assert nbb._hybrid()["zustand"] == abnahme.UNBEKANNT
    lage["_abgleich"](None)                   # Get-InboundConnector scheiterte
    p = nbb._hybrid()
    assert p["zustand"] == abnahme.UNBEKANNT and "nicht lesbar" in p["befund"]


def test_ohne_ausgangsadresse_unbekannt_smarthost_und_aus_entfallen(lage):
    lage["_abgleich"]([_con("Hybrid Inbound", [GW_IP])])
    lage["DIREKT_AUSSEN_IP"] = ""
    assert nbb._hybrid()["zustand"] == abnahme.UNBEKANNT
    lage["DIREKT_AUSSEN_IP"] = GW_IP
    lage["DIREKT_MODUS"] = "smarthost"
    assert nbb._hybrid()["zustand"] == abnahme.ENTFAELLT
    lage["DIREKT_MODUS"] = "mx"
    lage["DIREKT_ENABLED"] = False
    assert nbb._hybrid()["zustand"] == abnahme.ENTFAELLT


def test_zertifikat_treffer_auch_mit_wildcard():
    cons = [_con("Hybrid Zert", zert="*.firma.at")]
    assert nbb.hybrid_treffer(cons, "10.99.0.1", ["gw.firma.at"]) == [
        {"name": "Hybrid Zert", "art": "zertifikat", "wert": "gw.firma.at"}]
    assert nbb.hybrid_treffer(cons, "10.99.0.1", ["a.b.firma.at"]) == []      # nur eine Ebene
    assert nbb.hybrid_treffer(cons, "10.99.0.1", ["firma.at"]) == []
    assert nbb.hybrid_treffer([_con("Z", zert="mail.firma.at")], "10.99.0.1", ["MAIL.firma.at"])[0]["art"] \
        == "zertifikat"


def test_unlesbare_eintraege_zaehlen_nicht():
    assert nbb.hybrid_treffer([_con("X", ["kaputt", "", "10.99.0.0/33"])], "10.99.0.5", []) == []
    assert nbb.hybrid_treffer([_con("X", ["10.99.0.5"])], "keine-ip", []) == []


def test_postfix_zeigt_kein_client_zertifikat():
    """Die Prüfung vergleicht nur die IP, weil der Direktversand kein
    Client-Zertifikat zeigt. Bekommt Postfix eines, muss dessen Name in
    notbetrieb_bereitschaft._gateway_zertifikat_namen — sonst meldete die
    Prüfung einen Zertifikat-Treffer nie."""
    for modus in ("mx", "smarthost"):
        zeilen = direktversand.postconf_zeilen({"aktiv": True, "modus": modus, "smarthost": "relay.firma.at"},
                                               "gw.firma.at")
        assert not [z for z in zeilen if z.split("=")[0].strip() in ("smtp_tls_cert_file", "smtp_tls_chain_files")]
    assert nbb._gateway_zertifikat_namen() == []


def test_ps_skript_liest_nur_aktive_onpremises_connectoren():
    s = notbetrieb_rechte._ps_script("app", "firma.onmicrosoft.com")
    assert "Get-InboundConnector -ErrorAction Stop" in s
    assert "-eq 'OnPremises' -and $_.Enabled" in s
    assert "connectoren = $connectoren" in s


def test_pruefung_steht_in_der_bereitschaft(lage, monkeypatch):
    lage["_abgleich"]([_con("Hybrid Inbound", [GW_IP])])
    namen = [p["name"] for p in nbb.pruefen(mit_netz=False)["punkte"]]
    assert nbb.HYBRID in namen
