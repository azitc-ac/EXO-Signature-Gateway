"""CSP-Verstöße werden gesammelt (v1.11.7) — Grundlage, um die Richtlinie
später durchzusetzen. Ohne `report-uri` kam vorher nie ein Bericht an."""
import json

import pytest
from fastapi.testclient import TestClient

import csp_berichte


@pytest.fixture
def leer(tmp_path, monkeypatch):
    monkeypatch.setattr(csp_berichte.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(csp_berichte, "_arten", None)
    monkeypatch.setattr(csp_berichte, "_geschrieben", 0.0)
    return tmp_path


def _bericht(direktive="script-src-elem", uri="https://appsforoffice.microsoft.com/lib/1/office.js?t=geheim",
             seite="https://gw.example/addin/taskpane?token=x"):
    return {"csp-report": {"effective-directive": direktive, "blocked-uri": uri,
                           "document-uri": seite}}


def test_gleiche_art_wird_gezaehlt_ohne_pfad_und_query(leer):
    csp_berichte.merken(_bericht())
    csp_berichte.merken(_bericht())
    arten = csp_berichte.liste()
    assert len(arten) == 1 and arten[0]["anzahl"] == 2
    assert arten[0]["quelle"] == "https://appsforoffice.microsoft.com"
    assert arten[0]["seite"] == "/addin/taskpane", "keine Query (Tokens) speichern"
    assert "geheim" not in (leer / "csp_berichte.json").read_text()


def test_obergrenze(leer):
    for i in range(csp_berichte.MAX_ARTEN + 20):
        csp_berichte.merken(_bericht(seite=f"https://gw.example/s{i}"))
    assert len(csp_berichte.liste()) == csp_berichte.MAX_ARTEN


def test_unsinn_wird_ignoriert(leer):
    for x in (None, [], {"csp-report": "x"}, {"anders": 1}):
        csp_berichte.merken(x)
    assert csp_berichte.liste() == []


def test_kopf_nennt_die_berichtsadresse_und_endpunkt_nimmt_an(leer):
    from webui.app import app
    c = TestClient(app)
    r = c.get("/health")
    assert "report-uri /api/csp-bericht" in r.headers["content-security-policy-report-only"]
    r = c.post("/api/csp-bericht", content=json.dumps(_bericht()),
               headers={"Content-Type": "application/csp-report"})
    assert r.status_code == 204
    assert csp_berichte.liste()[0]["direktive"] == "script-src-elem"
    assert c.post("/api/csp-bericht", content=b"x" * 20000).status_code == 204


def test_abnahme(leer):
    import abnahme
    assert abnahme._csp_berichte()["zustand"] == abnahme.OK
    csp_berichte.merken(_bericht())
    p = abnahme._csp_berichte()
    assert p["zustand"] == abnahme.OFFEN and "appsforoffice" in p["befund"]
