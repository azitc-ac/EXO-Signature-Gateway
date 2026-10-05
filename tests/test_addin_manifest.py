"""Add-in-Manifest: ein Add-in, zwei Einstiegspunkte (Verfassen/Lesen) mit
GLEICHER Rubrik (Gateway-Name) und GLEICHEM Knopfnamen.

Der Gateway-Name stand bis v1.9.114 unmaskiert im XML — ein Name mit „&" machte
das Manifest ungültig, Outlook lehnte es beim Hochladen ab.
"""
from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET

import pytest

import settings_store
from webui.routen import addin

NS = {"o": "http://schemas.microsoft.com/office/appforoffice/1.1",
      "bt": "http://schemas.microsoft.com/office/officeappbasictypes/1.0",
      "v": "http://schemas.microsoft.com/office/mailappversionoverrides"}


class _Req:
    headers: dict = {}
    url = type("U", (), {"scheme": "https", "netloc": "sig.example", "hostname": "sig.example"})()


def _manifest(monkeypatch, name, abw):
    data = {"GATEWAY_NAME": name, "ABWESENHEIT_AKTIV": abw, "ADDIN_BASE_URL": "https://sig.example"}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: data.get(k, d))
    r = asyncio.run(addin.addin_manifest(_Req()))
    return ET.fromstring(r.body)          # wirft bei ungültigem XML


def _strings(root):
    return {e.get("id"): e.get("DefaultValue") for e in root.iter() if e.tag.endswith("}String")}


@pytest.mark.parametrize("name", ['Müller & Söhne <GW> "Prod"', "EXO Signature Gateway"])
def test_manifest_ist_gueltiges_xml_auch_mit_sonderzeichen(monkeypatch, name):
    root = _manifest(monkeypatch, name, True)
    assert root.find("o:ProviderName", NS).text == name
    assert _strings(root)["groupLabel"] == name


def test_beide_einstiegspunkte_teilen_rubrik_und_knopf(monkeypatch):
    root = _manifest(monkeypatch, "Mein GW", True)
    gruppen = [g for g in root.iter() if g.tag.endswith("}Group")]
    assert len(gruppen) == 2
    for g in gruppen:
        label = next(c for c in g if c.tag.endswith("}Label"))
        assert label.get("resid") == "groupLabel"
        knopf = next(c for c in g if c.tag.endswith("}Control"))
        assert next(c for c in knopf if c.tag.endswith("}Label")).get("resid") == "btnLabel"
    assert _strings(root)["btnLabel"] == "Signatur & Abwesenheit"


def test_knopf_ohne_abwesenheit_heisst_nur_signatur(monkeypatch):
    root = _manifest(monkeypatch, "Mein GW", False)
    assert _strings(root)["btnLabel"] == "Signatur"
    assert "Abwesenheit" not in ET.tostring(root, encoding="unicode")
