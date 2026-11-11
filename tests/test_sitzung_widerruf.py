"""Sitzungen enden mit Kennwortwechsel und Rollenentzug (v1.11.3).

Bis v1.11.2 galt ein Sitzungsmerkmal seine vollen 8 Stunden, auch nachdem das
örtliche Kennwort geändert oder einem Entra-Konto die Rolle entzogen war.
"""
import pytest

import settings_store
import sso


@pytest.fixture
def stand(monkeypatch):
    werte = {"ADMIN_PASSWORD_HASH": "pbkdf2:sha256:alt",
             "ADMIN_USERS": [{"upn": "chefin@firma.de", "role": sso.ROLE_ADMIN},
                             {"upn": "bearbeiter@firma.de", "role": sso.ROLE_EDITOR}]}
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))
    monkeypatch.setattr(sso, "_get_secret", lambda: "testgeheimnis-widerruf")
    return werte


def test_kennwortwechsel_beendet_oertliche_sitzungen(stand):
    keks = sso.create_session_cookie("admin", local=True)
    assert sso.verify_session_cookie(keks)
    stand["ADMIN_PASSWORD_HASH"] = "pbkdf2:sha256:neu"
    assert sso.verify_session_cookie(keks) is None
    assert sso.verify_session_cookie(sso.create_session_cookie("admin", local=True))


def test_alte_oertliche_merkmale_ohne_kennung_gelten_nicht(stand):
    """Merkmale von vor v1.11.3 tragen keine Kennung — einmal neu anmelden."""
    from itsdangerous import URLSafeTimedSerializer
    alt = URLSafeTimedSerializer("testgeheimnis-widerruf").dumps(
        {"u": "admin", "t": "local", "ts": 0, "r": sso.ROLE_ADMIN})
    assert sso.verify_session_cookie(alt) is None


@pytest.mark.parametrize("aenderung", ["entzogen", "herabgestuft"])
def test_rollenentzug_beendet_entra_sitzung(stand, aenderung):
    keks = sso.create_session_cookie("chefin@firma.de", role=sso.ROLE_ADMIN)
    assert sso.verify_session_cookie(keks)
    if aenderung == "entzogen":
        stand["ADMIN_USERS"] = stand["ADMIN_USERS"][1:]
    else:
        stand["ADMIN_USERS"][0]["role"] = sso.ROLE_EDITOR
    assert sso.verify_session_cookie(keks) is None


def test_kennwortwechsel_laesst_entra_sitzungen_stehen(stand):
    keks = sso.create_session_cookie("bearbeiter@firma.de", role=sso.ROLE_EDITOR)
    stand["ADMIN_PASSWORD_HASH"] = "pbkdf2:sha256:neu"
    assert sso.verify_session_cookie(keks)["r"] == sso.ROLE_EDITOR


def test_self_service_haengt_an_keiner_rolle(stand):
    keks = sso.create_session_cookie("irgendwer@firma.de", role=sso.ROLE_SELF)
    assert sso.verify_session_cookie(keks)["r"] == sso.ROLE_SELF


def test_kennwort_aendern_gibt_der_eigenen_sitzung_ein_neues_merkmal(stand, monkeypatch):
    from fastapi.testclient import TestClient
    from webui.app import app
    from webui.routen import setup as setup_routen
    monkeypatch.setattr(settings_store, "update", lambda d: stand.update(d))
    monkeypatch.setattr(setup_routen, "_verify_password", lambda pw, h: pw == "altes-kennwort")
    gesichert = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    try:
        c = TestClient(app)
        alt = sso.create_session_cookie("admin", local=True)
        c.cookies.set(sso.SESSION_COOKIE, alt)
        r = c.post("/api/setup/change-password",
                   json={"old_password": "altes-kennwort", "password": "neues-kennwort-1"})
        assert r.status_code == 200, r.text
        assert sso.verify_session_cookie(alt) is None, "das alte Merkmal ist ungültig"
        neu = r.cookies.get(sso.SESSION_COOKIE)
        assert neu and sso.verify_session_cookie(neu), "die eigene Sitzung läuft weiter"
    finally:
        app.dependency_overrides.update(gesichert)
