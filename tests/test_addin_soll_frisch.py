"""Soll-Fassung der Add-ins ohne Abruf des Manifests (v1.12.42).

Bis v1.12.41 entstand die Fassung nur, wenn jemand das Manifest abrief. Ein
per Adresse bereitgestelltes Add-in ruft Microsoft selten ab — nach einem
Update mit geändertem Manifest (hier: neue Ereignisse) blieb der gemerkte
Stand alt, die Postfachprüfung verglich alt mit alt und meldete „aktuell“.
"""
from __future__ import annotations

import pytest

import config
import health_check
import settings_store


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(settings_store, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings_store, "_data", {})
    settings_store.init()
    settings_store.update({"ADDIN_ENABLED": True, "ADDIN_BASE_URL": "https://gw.firma.at"})
    monkeypatch.setattr(config, "VERSION", "1.12.42")
    # Gemerkt aus einer alten Fassung: anderer Inhalt (Hash), alte Nummer.
    alt = {"hash": "0" * 64, "version": "1.11.8", "seit": "2026-11-01"}
    settings_store.force_update({"_ADDIN_VERFASSEN_STAND": alt, "_ADDIN_MANIFEST_STAND": dict(alt)})
    return settings_store


def test_soll_ist_die_aktuelle_fassung_ohne_abruf(store):
    soll = {a["art"]: a["version"] for a in health_check._addin_soll()}
    assert soll == {"verfassen": "1.12.42", "knopf": "1.12.42"}, soll


def test_unveraenderter_inhalt_behaelt_seine_fassung(store, monkeypatch):
    health_check._addin_soll()
    monkeypatch.setattr(config, "VERSION", "1.12.50")
    soll = {a["art"]: a["version"] for a in health_check._addin_soll()}
    assert soll["verfassen"] == "1.12.42", "ohne Änderung am Manifest keine neue Fassung"


def test_ohne_aussenadresse_bleibt_der_stand(store):
    settings_store.update({"ADDIN_BASE_URL": "", "PUBLIC_HOSTNAME": ""})
    from webui.routen import addin
    addin.manifest_staende_auffrischen()
    assert settings_store.get("_ADDIN_VERFASSEN_STAND")["version"] == "1.11.8"


def test_abruf_und_auffrischen_liefern_dieselbe_fassung(store):
    """Ausgeliefertes Manifest und Soll der Prüfung aus EINER Quelle."""
    from webui.routen import addin, addin_verfassen
    soll = {a["art"]: a["version"] for a in health_check._addin_soll()}
    assert f'<Version>{soll["verfassen"]}</Version>' in addin_verfassen.verfassen_manifest_xml("https://x.de")
    assert f'<Version>{soll["knopf"]}</Version>' in addin.knopf_manifest_xml("https://x.de")
