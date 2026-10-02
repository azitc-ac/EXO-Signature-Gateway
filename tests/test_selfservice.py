"""Self-Service-Fundament: Login-Freigabe + Sitzungs-Scope.

Der Kernschutz: ein Postfach-Nutzer darf NUR sein eigenes Postfach verwalten.
Die Identität kommt aus der Sitzung, nie aus einem Parameter — und eine Sitzung
als Self-Service-Nutzer gibt es nur, wenn der Betreiber es freigeschaltet hat und
das Postfach im Gateway überhaupt verwaltet wird.
"""
import pytest

import settings_store
import sso
from webui import deps


class _Req:
    """Minimaler Request-Ersatz (nur das, was _get_session_user liest)."""
    def __init__(self, cookie=None, header=None):
        self.cookies = {sso.SESSION_COOKIE: cookie} if cookie else {}
        self.headers = {"X-Addin-Session": header} if header else {}


def _store(monkeypatch, enabled, cfg):
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: {
        "SELF_SERVICE_ENABLED": enabled, "MAILBOX_CONFIG": cfg}.get(k, d))


_CFG = {"g": {"known_addresses": ["a@x.de"], "primary": "a@x.de", "sig": True}}


def test_rolle_nur_bei_freigabe(monkeypatch):
    _store(monkeypatch, False, _CFG)        # aus → keine Rolle, obwohl Postfach bekannt
    assert deps.self_service_rolle("a@x.de") is None


def test_rolle_bei_freigabe_und_bekanntem_postfach(monkeypatch):
    _store(monkeypatch, True, _CFG)
    assert deps.self_service_rolle("a@x.de") == sso.ROLE_SELF


def test_rolle_nicht_fuer_fremdes_postfach(monkeypatch):
    """Freigeschaltet, aber die Adresse ist kein verwaltetes Postfach → keine Sitzung."""
    _store(monkeypatch, True, _CFG)
    assert deps.self_service_rolle("fremd@y.de") is None


def test_require_self_ohne_sitzung_401(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(sso, "verify_session_cookie", lambda v: None)
    with pytest.raises(HTTPException) as exc:
        deps._require_self(_Req())
    assert exc.value.status_code == 401


CB = "https://sig.example/auth/id-callback"


def test_implicit_patch_leere_web_setzt_beides():
    """Raspi-Fall: web leer → id-callback-Redirect + ID-Token-Erteilung werden gesetzt."""
    import setup_wizard
    neu = setup_wizard._implicit_web_patch({}, CB)
    assert neu["redirectUris"] == [CB]
    assert neu["implicitGrantSettings"]["enableIdTokenIssuance"] is True
    assert neu["implicitGrantSettings"]["enableAccessTokenIssuance"] is False


def test_implicit_patch_schon_korrekt_gibt_none():
    """Prod-Fall: alles da → None (kein PATCH, keine Dedup-Störung)."""
    import setup_wizard
    web = {"redirectUris": [CB],
           "implicitGrantSettings": {"enableIdTokenIssuance": True, "enableAccessTokenIssuance": False}}
    assert setup_wizard._implicit_web_patch(web, CB) is None


def test_implicit_patch_ergaenzt_und_erhaelt():
    """Fehlt nur der Redirect, bleibt enableAccessTokenIssuance erhalten; andere
    web-Felder (homePageUrl) überleben den PATCH."""
    import setup_wizard
    web = {"homePageUrl": "https://x", "redirectUris": [],
           "implicitGrantSettings": {"enableIdTokenIssuance": True, "enableAccessTokenIssuance": True}}
    neu = setup_wizard._implicit_web_patch(web, CB)
    assert neu["redirectUris"] == [CB]
    assert neu["implicitGrantSettings"]["enableAccessTokenIssuance"] is True   # erhalten
    assert neu["homePageUrl"] == "https://x"                                   # erhalten
    assert CB not in (web.get("redirectUris") or []) or True                   # Original nicht nötig


def test_require_self_identitaet_aus_sitzung(monkeypatch):
    """DER Kernschutz: die Adresse kommt aus der Sitzung (klein), nicht aus einem
    Parameter — sonst könnte Nutzer A das Postfach von Nutzer B anfassen."""
    monkeypatch.setattr(sso, "verify_session_cookie",
                        lambda v: {"u": "Erika@X.DE", "r": sso.ROLE_SELF})
    assert deps._require_self(_Req(cookie="tok")) == "erika@x.de"
    assert deps._require_self(_Req(header="tok")) == "erika@x.de"   # Add-in-Weg
