"""Conditional-Access-Step-up (MFA) im Auth-Code-Flow.

Diese Tests MÜSSEN fehlschlagen, wenn:
  - der Token-Tausch einen Claims-Challenge (AADSTS50076) still als normalen Fehler
    behandelt, statt InteractionRequired mit dem Claims zu werfen,
  - der Claims-Challenge NICHT an /authorize weitergereicht wird.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import pkce


class _FakeResp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


class _FakeClient:
    def __init__(self, data):
        self._d = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, data=None):
        return _FakeResp(self._d)


def test_exchange_code_wirft_interaction_required_bei_claims(monkeypatch):
    import httpx
    claims = '{"access_token":{"acr":{"essential":true,"values":["urn:...mfa"]}}}'
    data = {"error": "interaction_required",
            "error_description": "AADSTS50076: ... multi-factor authentication ...",
            "claims": claims}
    monkeypatch.setattr(pkce, "_get_client_id", lambda: "cid")
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(data))
    with pytest.raises(pkce.InteractionRequired) as ei:
        asyncio.run(pkce.exchange_code("code", "verifier", "https://rp/cb",
                                       scopes=["openid", "profile", "email"]))
    assert ei.value.claims == claims                     # Challenge erhalten


def test_exchange_code_50076_ohne_claims_wirft_interaction_required(monkeypatch):
    # Realität auf Prod: AADSTS50076 kommt bei der Auth-Code-Einlösung OHNE claims-Feld.
    import httpx
    data = {"error": "interaction_required", "error_codes": [50076],
            "error_description": "AADSTS50076: ... multi-factor authentication ..."}
    monkeypatch.setattr(pkce, "_get_client_id", lambda: "cid")
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(data))
    with pytest.raises(pkce.InteractionRequired) as ei:
        asyncio.run(pkce.exchange_code("code", "verifier", "https://rp/cb", scopes=["openid"]))
    assert ei.value.claims == ""                          # kein Challenge, trotzdem erkannt


def test_create_session_prompt_login(monkeypatch):
    monkeypatch.setattr(pkce, "_get_client_id", lambda: "cid")
    _s, url = pkce.create_session("https://rp/cb", scopes=["openid"], flow="sso",
                                  prompt="login")
    assert "prompt=login" in url                          # frische Anmeldung erzwungen
    _s2, url2 = pkce.create_session("https://rp/cb", scopes=["openid"], flow="sso")
    assert "prompt=select_account" in url2                # Vorgabe unverändert


def test_exchange_code_normaler_fehler_bleibt_runtimeerror(monkeypatch):
    import httpx
    data = {"error": "invalid_grant", "error_description": "bad code"}   # kein claims
    monkeypatch.setattr(pkce, "_get_client_id", lambda: "cid")
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(data))
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(pkce.exchange_code("code", "verifier", "https://rp/cb", scopes=["openid"]))
    assert not isinstance(ei.value, pkce.InteractionRequired)


def test_create_session_haengt_claims_an(monkeypatch):
    monkeypatch.setattr(pkce, "_get_client_id", lambda: "cid")
    _s, url = pkce.create_session("https://rp/cb", scopes=["openid"], flow="sso",
                                  claims='{"a":1}')
    assert "claims=" in url                              # Challenge geht an /authorize
    _s2, url2 = pkce.create_session("https://rp/cb", scopes=["openid"], flow="sso")
    assert "claims=" not in url2                         # ohne Challenge kein Param
