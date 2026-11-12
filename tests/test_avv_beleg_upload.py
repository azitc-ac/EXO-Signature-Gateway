"""AVV-Beleg geht mit jedem Diagnosepaket an den Hub (v1.11.53).

Bis dahin verlangte das Gateway die Zustimmung zur AVV, übermittelte den Beleg
aber nie — der Hub konnte nicht nachweisen, dass vor der Übermittlung ein
Vertrag nach Art. 28 DSGVO bestand.
"""
import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

import legal_consent


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setattr(legal_consent, "_DB_PATH", tmp_path / "legal_consent.db")
    # Im Container /app/legal — hier das Repository
    monkeypatch.setattr(legal_consent, "_LEGAL_DIR",
                        Path(__file__).resolve().parent.parent / "legal")
    return tmp_path


def _aktuell():
    return (legal_consent.CURRENT_DOCUMENTS["dpa"]["version"],
            legal_consent.compute_document_hash("dpa"))


def test_ohne_zustimmung_kein_beleg(db):
    assert legal_consent.avv_beleg() is None


def test_beleg_mit_zustimmendem(db):
    version, h = _aktuell()
    assert h
    legal_consent.record_consent("dpa", version, h, "support_upload", accepted_by="admin")
    b = legal_consent.avv_beleg()
    assert b["doc_id"] == "dpa" and b["doc_version"] == version and b["doc_hash"] == h
    assert b["accepted_by"] == "admin" and b["accepted_at"]


def test_aeltere_fassung_zaehlt_nicht(db):
    legal_consent.record_consent("dpa", "0.9", "f" * 64, accepted_by="admin")
    assert legal_consent.avv_beleg() is None


def test_alte_datenbank_bekommt_die_spalte(db):
    with sqlite3.connect(str(db / "legal_consent.db")) as c:
        c.execute("CREATE TABLE consents (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                  "document_id TEXT NOT NULL, version TEXT NOT NULL, content_hash TEXT NOT NULL, "
                  "context TEXT NOT NULL DEFAULT '', accepted_at TEXT NOT NULL DEFAULT 'x')")
        version, h = _aktuell()
        c.execute("INSERT INTO consents (document_id, version, content_hash) VALUES ('dpa',?,?)",
                  (version, h))
    b = legal_consent.avv_beleg()
    assert b and b["accepted_by"] == ""


def test_upload_schickt_den_beleg(db, monkeypatch):
    import hub_client
    import settings_store
    import support_upload
    version, h = _aktuell()
    legal_consent.record_consent("dpa", version, h, accepted_by="admin")
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: {
        "HUB_BASE_URL": "https://hub.example", "HUB_API_KEY": "k"}.get(k, d))
    monkeypatch.setattr(support_upload, "build_bundle", lambda logs: (b"PK", "p.zip"))
    monkeypatch.setattr(hub_client, "_gateway_headers", lambda key=None: {})
    gesendet = {}

    class _Antwort:
        status_code = 200

        def json(self):
            return {"stored_as": "p.zip"}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, files=None, data=None):
            gesendet.update(data or {})
            return _Antwort()
    monkeypatch.setattr(hub_client.httpx, "AsyncClient", _Client)
    r = asyncio.run(hub_client.upload_bundle([], note="n"))
    assert r["ok"]
    beleg = json.loads(gesendet["avv_beleg"])
    assert beleg["doc_version"] == version and beleg["doc_hash"] == h
