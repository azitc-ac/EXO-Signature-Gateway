"""Knopf-Manifest für Exchange lokal (v1.12.74).

Exchange lokal (2016/2019/SE) unterstützt die Mailbox-Sätze 1.1–1.5 (MS Learn,
„Outlook JavaScript API requirement sets“, „Exchange server support“). New-App
lehnte das Exchange-Online-Manifest ab: „The API version (1.13) required by this
app isn't supported by the Exchange Server version (15.2.2562.17)“ — verlangt in
der inneren VersionOverridesV1_1 (Anheften, Lese-Knopf ohne Mail).

Gesichert:
* Die lokale Fassung verlangt nirgends mehr als 1.5, ist gültiges XML und trägt
  dieselbe Kennung und dieselben Knöpfe (VersionOverrides 1.0).
* Die Exchange-Online-Fassung bleibt, wie sie war (1.13-Block vorhanden).
* Beide Fassungen führen getrennte Versionsstände — abwechselnde Abrufe zählen
  die Version nicht hoch.
* Die Befehle für Exchange lokal zeigen beim Knopf auf die lokale Fassung, und
  `manifest_staende_auffrischen` frischt sie mit auf.
* Das Verfassen-Manifest (lokal eingespielt ✓) verlangt höchstens 1.5.
"""
from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET

import pytest

import settings_store
from webui.routen import addin, addin_verfassen

BASE = "https://sig.example"
LOKAL_MAX = (1, 5)


class _Req:
    headers: dict = {}
    url = type("U", (), {"scheme": "https", "netloc": "sig.example", "hostname": "sig.example"})()


@pytest.fixture
def daten(monkeypatch):
    d = {"GATEWAY_NAME": "Mein GW", "ABWESENHEIT_AKTIV": True, "ADDIN_BASE_URL": BASE}
    monkeypatch.setattr(settings_store, "get", lambda k, default=None: d.get(k, default))
    monkeypatch.setattr(settings_store, "force_update", lambda patch: d.update(patch))
    return d


def _versionen(xml: str) -> list[tuple[int, ...]]:
    root = ET.fromstring(xml)
    werte = [e.get("MinVersion") for e in root.iter() if e.tag.endswith("}Set") and e.get("MinVersion")]
    werte += [e.get("DefaultMinVersion") for e in root.iter() if e.get("DefaultMinVersion")]
    assert werte, "keine Versionsangabe gefunden — Test misst nichts"
    return [tuple(int(t) for t in w.split(".")) for w in werte]


def test_lokale_fassung_verlangt_hoechstens_1_5(daten):
    xml = addin.knopf_manifest_xml(BASE, lokal=True)
    assert max(_versionen(xml)) <= LOKAL_MAX
    assert "VersionOverridesV1_1" not in xml and "SupportsPinning" not in xml


def test_exchange_online_fassung_unveraendert_mit_1_13(daten):
    xml = asyncio.run(addin.addin_manifest(_Req())).body.decode()
    assert (1, 13) in _versionen(xml)
    assert xml.count("VersionOverridesV1_1") == 1 and "<SupportsPinning>true</SupportsPinning>" in xml


def test_route_liefert_die_lokale_fassung(daten):
    r = asyncio.run(addin.addin_manifest_lokal(_Req()))
    assert r.media_type == "application/xml"
    assert max(_versionen(r.body.decode())) <= LOKAL_MAX


def test_gleiche_kennung_und_gleiche_knoepfe(daten):
    ns = "{http://schemas.microsoft.com/office/appforoffice/1.1}"
    cloud = ET.fromstring(addin.knopf_manifest_xml(BASE))
    lokal = ET.fromstring(addin.knopf_manifest_xml(BASE, lokal=True))
    assert lokal.find(f"{ns}Id").text == cloud.find(f"{ns}Id").text == addin.knopf_addin_id("sig.example")

    def knoepfe(root):
        return sorted(c.get("id") for c in root.iter() if c.tag.endswith("}Control"))
    assert knoepfe(lokal) == ["exo.self.btn", "exo.sig.btn"]
    assert set(knoepfe(cloud)) == set(knoepfe(lokal))


def test_getrennte_staende_abwechselnd_abgerufen(daten):
    """Mit gemeinsamem Stand zählte jeder Wechsel die vierte Stelle hoch."""
    erste = (addin.knopf_manifest_xml(BASE), addin.knopf_manifest_xml(BASE, lokal=True))
    for _ in range(3):
        addin.knopf_manifest_xml(BASE)
        addin.knopf_manifest_xml(BASE, lokal=True)
    assert (addin.knopf_manifest_xml(BASE), addin.knopf_manifest_xml(BASE, lokal=True)) == erste
    assert "_ADDIN_MANIFEST_LOKAL_STAND" in daten and "_ADDIN_MANIFEST_STAND" in daten


def test_befehle_zeigen_auf_die_lokale_fassung(daten):
    knopf = next(b for b in addin.lokal_bereitstellung(BASE) if b["art"] == "knopf")
    assert knopf["url"] == BASE + addin.KNOPF_LOKAL_PFAD
    assert f"'{BASE}{addin.KNOPF_LOKAL_PFAD}'" in knopf["neu_url"]


def test_auffrischen_erfasst_die_lokale_fassung(daten, monkeypatch):
    import aussenadresse
    monkeypatch.setattr(aussenadresse, "konfiguriert", lambda: BASE)
    addin.manifest_staende_auffrischen()
    assert "_ADDIN_MANIFEST_LOKAL_STAND" in daten


def test_verfassen_manifest_passt_fuer_exchange_lokal(daten):
    assert max(_versionen(addin_verfassen.verfassen_manifest_xml(BASE))) <= LOKAL_MAX


def test_anleitung_nennt_filedata_zuerst():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "app/webui/templates/setup.html").read_text("utf-8")
    zeile = next(z for z in html.splitlines() if "a.neu_datei" in z and "{% for teil" in z)
    assert zeile.index("a.neu_datei") < zeile.index("a.neu_url")
