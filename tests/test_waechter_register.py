"""Wächter-Register: mehrere Wächter, je eigenes Token/Zustand, Migration.

Kern des Mehrwächter-Modells. Diese Tests MÜSSEN fehlschlagen, wenn ein Wächter
den anderen beim Entfernen mitnimmt, wenn die Zuordnung per Id/Token verrutscht
oder die Legacy-Migration den bestehenden Wächter nicht übernimmt.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import waechter_register as reg


@pytest.fixture(autouse=True)
def _tmp_register(monkeypatch, tmp_path):
    monkeypatch.setattr(reg, "PFAD", tmp_path / "watchdog_watchers.json")


def _verify(token, h):
    # einfacher Stub: Hash ist hier "H:" + token
    return h == "H:" + token


def test_registrieren_und_liste_ohne_geheimnis():
    reg.registrieren(id="wd_a", name="A", kind="azure", token_hash="H:ta",
                     azure={"subscription": "s", "resource_group": "rg", "app_name": "app"})
    reg.registrieren(id="wd_b", name="B", kind="cron", token_hash="H:tb")
    liste = reg.liste()
    assert reg.anzahl() == 2 and len(liste) == 2
    for e in liste:
        assert "token_hash" not in e                 # Geheimnis nie nach außen
    a = next(e for e in liste if e["id"] == "wd_a")
    assert a["azure"]["app_name"] == "app"           # Teardown-Metadaten bleiben


def test_entfernen_stoert_den_anderen_nicht():
    reg.registrieren(id="wd_a", name="A", kind="azure", token_hash="H:ta")
    reg.registrieren(id="wd_b", name="B", kind="cron", token_hash="H:tb")
    weg = reg.entfernen("wd_a")
    assert weg["id"] == "wd_a"
    assert reg.anzahl() == 1 and reg.holen("wd_b") is not None
    assert reg.holen("wd_a") is None
    assert reg.entfernen("gibtsnicht") is None


def test_zuordnen_per_id_und_per_scan():
    reg.registrieren(id="wd_a", name="A", kind="azure", token_hash="H:ta")
    reg.registrieren(id="wd_b", name="B", kind="cron", token_hash="H:tb")
    # direkte Id
    assert reg.zuordnen("ta", "wd_a", _verify) == "wd_a"
    # Id gesetzt, aber Token passt nicht → None
    assert reg.zuordnen("tb", "wd_a", _verify) is None
    # ohne Id: Scan über alle
    assert reg.zuordnen("tb", None, _verify) == "wd_b"
    # unbekanntes Token
    assert reg.zuordnen("xxx", None, _verify) is None
    assert reg.zuordnen("", None, _verify) is None


def test_heartbeat_nur_erlaubte_felder():
    reg.registrieren(id="wd_a", name="A", kind="cron", token_hash="H:ta")
    ok = reg.heartbeat_aktualisieren("wd_a", {
        "last_seen": "2026-09-08T10:00:00Z", "healthy": True, "bypass_active": True,
        "fails": 2, "oks": 5, "exo_error": "x", "name": "GEKAPERT", "kind": "azure"})
    assert ok
    e = reg.holen("wd_a")
    assert e["healthy"] and e["bypass_active"] and e["fails"] == 2
    assert e["name"] == "A" and e["kind"] == "cron"   # Stammdaten NICHT über Heartbeat änderbar
    assert reg.heartbeat_aktualisieren("unbekannt", {"healthy": True}) is False


def test_umbenennen():
    reg.registrieren(id="wd_a", name="Alt", kind="azure", token_hash="H:ta")
    assert reg.umbenennen("wd_a", "  Neu  ") is True
    assert reg.holen("wd_a")["name"] == "Neu"          # getrimmt
    assert reg.umbenennen("wd_a", "   ") is False       # leer → abgelehnt
    assert reg.holen("wd_a")["name"] == "Neu"           # unverändert
    assert reg.umbenennen("unbekannt", "X") is False


def test_merke_azure_traegt_appid_und_grants_nach():
    reg.registrieren(id="wd_a", name="A", kind="azure", token_hash="H:ta",
                     azure={"principal_id": "obj-1", "app_id": ""})
    assert reg.merke_azure("wd_a", app_id="app-1")
    assert reg.holen("wd_a")["azure"]["app_id"] == "app-1"
    assert reg.holen("wd_a")["azure"]["principal_id"] == "obj-1"   # Bestehendes bleibt
    assert reg.merke_azure("wd_a", grants_done=True)
    assert reg.holen("wd_a")["azure"]["grants_done"] is True
    assert reg.merke_azure("unbekannt", app_id="x") is False


def test_irgendein_bypass_aktiv():
    reg.registrieren(id="wd_a", name="A", kind="cron", token_hash="H:ta")
    reg.registrieren(id="wd_b", name="B", kind="azure", token_hash="H:tb")
    assert reg.irgendein_bypass_aktiv() is False
    reg.heartbeat_aktualisieren("wd_b", {"bypass_active": True})
    assert reg.irgendein_bypass_aktiv() is True


def test_migration_uebernimmt_legacy_waechter(monkeypatch):
    import settings_store
    import waechter_state
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a: {"WATCHDOG_TOKEN_HASH": "H:legacy",
                                       "WATCHDOG_KIND": "azure"}.get(k, ""))
    monkeypatch.setattr(waechter_state, "lesen",
                        lambda: {"last_seen": "2026-09-01T00:00:00Z", "bypass_active": True,
                                 "fails": 1, "oks": 9, "healthy": True, "exo_error": ""})
    assert reg.migrieren_falls_noetig() is True
    e = reg.holen("wd_legacy")
    assert e and e["token_hash"] == "H:legacy" and e["kind"] == "azure"
    assert e["last_seen"] == "2026-09-01T00:00:00Z" and e["bypass_active"] is True
    # Der Legacy-Wächter ist per Token-Scan (ohne Id) auffindbar.
    assert reg.zuordnen("legacy", None, _verify) == "wd_legacy"
    # Zweiter Lauf: nichts mehr zu tun.
    assert reg.migrieren_falls_noetig() is False


def test_migration_no_op_ohne_legacy_token(monkeypatch):
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, *a: "")
    assert reg.migrieren_falls_noetig() is False
    assert reg.anzahl() == 0


def test_migration_uebersprungen_wenn_register_da(monkeypatch):
    import settings_store
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a: {"WATCHDOG_TOKEN_HASH": "H:legacy"}.get(k, ""))
    reg.registrieren(id="wd_neu", name="Neu", kind="azure", token_hash="H:t")
    assert reg.migrieren_falls_noetig() is False       # es gibt schon Wächter
    assert reg.holen("wd_legacy") is None
