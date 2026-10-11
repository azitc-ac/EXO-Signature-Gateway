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


def _manifest(monkeypatch, name, abw, data=None):
    data = data if data is not None else {}
    data.update({"GATEWAY_NAME": name, "ABWESENHEIT_AKTIV": abw, "ADDIN_BASE_URL": "https://sig.example"})
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: data.get(k, d))
    monkeypatch.setattr(settings_store, "force_update", lambda patch: data.update(patch))
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


@pytest.mark.parametrize("ver,erwartet", [("1.9.143", "1.9.143"), ("2.0.07", "2.0.7"),
                                          ("dev", "1.0.0"), ("1.9.143-rc1", "1.0.0"), ("", "1.0.0")])
def test_erstes_manifest_traegt_die_gateway_version(monkeypatch, ver, erwartet):
    import config
    monkeypatch.setattr(config, "VERSION", ver)
    root = _manifest(monkeypatch, "Mein GW", True)
    assert root.find("o:Version", NS).text == erwartet


def _version(root):
    return root.find("o:Version", NS).text


def test_version_bleibt_beim_gateway_update_ohne_manifestaenderung(monkeypatch):
    """Integrierte Apps meldet sonst bei JEDEM Gateway-Update ein Add-in-Update."""
    import config
    data = {}
    monkeypatch.setattr(config, "VERSION", "1.9.146")
    assert _version(_manifest(monkeypatch, "Mein GW", True, data)) == "1.9.146"
    monkeypatch.setattr(config, "VERSION", "1.9.150")
    assert _version(_manifest(monkeypatch, "Mein GW", True, data)) == "1.9.146"


def test_version_steigt_bei_inhaltlicher_aenderung(monkeypatch):
    """Neuer Text (hier: Abwesenheit abgeschaltet) → höhere Version, sonst greift „Aktualisieren" nicht."""
    import config
    data = {}
    monkeypatch.setattr(config, "VERSION", "1.9.146")
    _manifest(monkeypatch, "Mein GW", True, data)
    monkeypatch.setattr(config, "VERSION", "1.9.150")
    assert _version(_manifest(monkeypatch, "Mein GW", False, data)) == "1.9.150"


def test_aenderung_ohne_gateway_update_zaehlt_die_vierte_stelle(monkeypatch):
    """Gleiche Gateway-Version, anderer Inhalt: nie gleich oder niedriger, und keine
    künftige Gateway-Version vorwegnehmen."""
    import config
    data = {}
    monkeypatch.setattr(config, "VERSION", "1.9.146")
    _manifest(monkeypatch, "Mein GW", True, data)
    assert _version(_manifest(monkeypatch, "Mein GW", False, data)) == "1.9.146.1"
    assert _version(_manifest(monkeypatch, "Mein GW", True, data)) == "1.9.146.2"


def test_version_faellt_nie(monkeypatch):
    """Gespeicherter Stand höher als die laufende Gateway-Version (Rückspielen eines
    älteren Images): eine niedrigere Version würde Integrierte Apps nicht annehmen."""
    import config
    data = {"_ADDIN_MANIFEST_STAND": {"hash": "anderer", "version": "1.9.200"}}
    monkeypatch.setattr(config, "VERSION", "1.9.146")
    assert _version(_manifest(monkeypatch, "Mein GW", True, data)) == "1.9.200.1"


def test_versionsnummer_ist_schemagerecht(monkeypatch):
    """Office-Manifest: höchstens vier Teile aus Ziffern."""
    import config, re
    data = {"_ADDIN_MANIFEST_STAND": {"hash": "x", "version": "1.9.146.7"}}
    monkeypatch.setattr(config, "VERSION", "1.9.146")
    v = _version(_manifest(monkeypatch, "Mein GW", True, data))
    assert v == "1.9.146.8" and re.fullmatch(r"\d{1,5}(\.\d{1,5}){0,3}", v)


def test_fremde_adresse_aendert_weder_version_noch_speicher(monkeypatch):
    """Ohne ADDIN_BASE_URL kommt die Adresse aus X-Forwarded-Host, und das
    Manifest ist anonym abrufbar. Ein Abruf mit fremdem Host darf die Version
    nicht hochzählen und nichts in settings.json schreiben."""
    import config
    data = {}
    schreibvorgaenge = []
    monkeypatch.setattr(config, "VERSION", "1.9.148")
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: data.get(k, d))
    monkeypatch.setattr(settings_store, "force_update",
                        lambda patch: (schreibvorgaenge.append(patch), data.update(patch)))
    data.update({"GATEWAY_NAME": "Mein GW", "ABWESENHEIT_AKTIV": True})   # KEIN ADDIN_BASE_URL

    def abruf(host):
        req = type("R", (), {"headers": {"x-forwarded-host": host, "x-forwarded-proto": "https"},
                             "url": _Req.url})()
        return ET.fromstring(asyncio.run(addin.addin_manifest(req)).body).find("o:Version", NS).text

    erste = abruf("sig.example")
    assert [abruf(h) for h in ("evil.example", "x.test", "sig.example")] == [erste] * 3
    assert len(schreibvorgaenge) == 1, schreibvorgaenge
