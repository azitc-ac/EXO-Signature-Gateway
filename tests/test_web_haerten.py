"""Web-UI-Härtung: Sicherheits-Header und Herkunftsprüfung (CSRF).

Prüft die Middleware in `webui/app.py`:
- Sicherheits-Header auf normalen Seiten, KEIN Frame-Verbot auf Add-in-Seiten
  (Office rahmt sie in einem iframe).
- Mutierende Anfragen aus fremder Herkunft werden abgewiesen; eigene Herkunft,
  fehlende Herkunft und der Add-in-Weg (Token im Kopffeld) kommen durch.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

pytest.importorskip("starlette.testclient", reason="httpx wird für TestClient benötigt")


@pytest.fixture
def client():
    from starlette.testclient import TestClient
    from webui import app as wa
    with TestClient(wa.app) as c:
        yield c


def test_sicherheits_header_auf_normaler_seite(client):
    r = client.get("/auth/login")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("Referrer-Policy")
    assert "Content-Security-Policy-Report-Only" in r.headers


def test_addin_seite_ohne_frame_verbot(client):
    # Office rahmt /addin/… — hier darf KEIN X-Frame-Options: DENY stehen,
    # sonst lädt das Add-in nicht. (Statuscode egal, Middleware greift immer.)
    r = client.get("/addin/manifest.xml")
    assert r.headers.get("X-Frame-Options") != "DENY"


def test_fremde_herkunft_wird_abgewiesen(client):
    r = client.post("/api/test-mail", json={"from_email": "a@x.de", "to_email": "b@x.de"},
                    headers={"Origin": "https://boese.example"})
    assert r.status_code == 403
    assert "Herkunft" in r.text


def test_eigene_herkunft_passiert_die_pruefung(client):
    # testserver ist der Host des TestClients → gilt als eigene Herkunft.
    # Danach greift die Anmeldung (401), NICHT die Herkunftsprüfung (403/Herkunft).
    r = client.post("/api/test-mail", json={"from_email": "a@x.de", "to_email": "b@x.de"},
                    headers={"Origin": "http://testserver"})
    assert "Herkunft" not in r.text


def test_ohne_herkunft_passiert_die_pruefung(client):
    # Kein Origin/Referer (z.B. Nicht-Browser) → Herkunftsprüfung greift nicht.
    r = client.post("/api/test-mail", json={"from_email": "a@x.de", "to_email": "b@x.de"})
    assert "Herkunft" not in r.text


def test_auth_local_wird_nach_fehlversuchen_gedrosselt(client):
    """Wiederholte Fehlanmeldungen führen zu 429 — die 429 kann nur aus der
    Drosselung stammen (nichts sonst liefert sie auf /auth/local)."""
    import login_drossel
    login_drossel._FEHLER.clear()
    try:
        letzte = None
        for _ in range(login_drossel._FREI + 3):
            letzte = client.post("/auth/local",
                                 json={"username": "admin", "password": "falsch-xyz"})
        assert letzte.status_code == 429, letzte.status_code
    finally:
        login_drossel._FEHLER.clear()


def test_addin_token_ist_von_der_pruefung_ausgenommen(client):
    # Add-in schickt das Token im Kopffeld (kein Cookie-CSRF) → auch bei fremder
    # Herkunft kein Herkunfts-403.
    r = client.post("/api/test-mail", json={"from_email": "a@x.de", "to_email": "b@x.de"},
                    headers={"Origin": "https://boese.example", "X-Addin-Session": "x"})
    assert "Herkunft" not in r.text
