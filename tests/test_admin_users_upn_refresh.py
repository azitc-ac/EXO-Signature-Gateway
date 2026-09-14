"""ADMIN_USERS: den gespeicherten UPN aus der stabilen oid auffrischen.

Diese Tests MÜSSEN fehlschlagen, wenn:
  - ein umbenannter UPN NICHT nachgezogen würde (Anzeige/Aktionen blieben stale),
  - dabei die oid verloren ginge (der stabile Anker!),
  - ohne Änderung trotzdem geschrieben würde (unnötige Persistenz).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import sso


def test_refresh_zieht_umbenannten_upn_nach_und_erhaelt_oid(monkeypatch):
    stored = [
        {"upn": "alt@f.de", "role": "editor", "id": "OID-1"},
        {"upn": "bleibt@f.de", "role": "admin", "id": "OID-2"},
        {"upn": "ohne-oid@f.de", "role": "editor"},
    ]
    monkeypatch.setattr(sso, "normalize_users", lambda: [dict(u) for u in stored])
    monkeypatch.setattr(sso, "resolve_oid_to_upn",
                        lambda oid: {"OID-1": "neu@f.de", "OID-2": "bleibt@f.de"}.get(oid))
    geschrieben = {}
    monkeypatch.setattr(sso.settings_store, "update", lambda d: geschrieben.update(d))

    res = sso.refresh_stored_upns()

    e1 = [u for u in res if u.get("id") == "OID-1"][0]
    assert e1["upn"] == "neu@f.de"                       # umbenannt → nachgezogen
    assert [u for u in res if u.get("id") == "OID-2"][0]["upn"] == "bleibt@f.de"
    assert any(u["upn"] == "ohne-oid@f.de" for u in res)  # ohne oid unberührt
    assert "ADMIN_USERS" in geschrieben                   # persistiert
    persisted = {u["upn"]: u for u in geschrieben["ADMIN_USERS"]}
    assert persisted["neu@f.de"]["id"] == "OID-1"         # oid NICHT verloren


def test_refresh_ohne_aenderung_schreibt_nicht(monkeypatch):
    stored = [{"upn": "gleich@f.de", "role": "editor", "id": "OID-1"}]
    monkeypatch.setattr(sso, "normalize_users", lambda: [dict(u) for u in stored])
    monkeypatch.setattr(sso, "resolve_oid_to_upn", lambda oid: "gleich@f.de")
    calls = []
    monkeypatch.setattr(sso.settings_store, "update", lambda d: calls.append(d))

    sso.refresh_stored_upns()
    assert calls == []                                    # nichts geändert → kein Schreiben


def test_refresh_bei_graph_fehler_behaelt_gespeicherten_upn(monkeypatch):
    stored = [{"upn": "alt@f.de", "role": "editor", "id": "OID-1"}]
    monkeypatch.setattr(sso, "normalize_users", lambda: [dict(u) for u in stored])
    monkeypatch.setattr(sso, "resolve_oid_to_upn", lambda oid: None)   # Graph liefert nichts
    calls = []
    monkeypatch.setattr(sso.settings_store, "update", lambda d: calls.append(d))

    res = sso.refresh_stored_upns()
    assert res[0]["upn"] == "alt@f.de"                    # Fallback: gespeicherter UPN bleibt
    assert calls == []
