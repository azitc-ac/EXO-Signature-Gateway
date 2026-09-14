"""SSO Implicit-id_token-Flow für nicht verwaltete Geräte.

Der Login holt die Identität direkt vom /authorize (kein Graph-Token), damit ein
Conditional-Access-MFA-Step-up am interaktiven Sign-in erscheint. Ein
Front-Channel-id_token MUSS vollständig verifiziert werden — diese Tests sichern
genau das ab:
  - falscher nonce / falsche Audience / fremder Tenant / abgelaufen / falsche
    Signatur → KEIN Login (None),
  - der Implicit-Authorize-Request hat kein Graph-Token/kein PKCE.
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import pkce
import sso


# ── Implicit-Authorize-URL ──────────────────────────────────────────────────────

def test_create_implicit_session_url_und_sitzung(monkeypatch):
    import urllib.parse
    monkeypatch.setattr(pkce, "_get_client_id", lambda: "CID")
    state, url = pkce.create_implicit_session("https://rp/cb", next_url="/x")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["response_type"] == ["id_token"]
    assert q["response_mode"] == ["form_post"]
    assert q["scope"] == ["openid profile email"]      # KEIN Graph-Resource-Scope
    assert q["nonce"][0]
    assert "code_challenge" not in q                   # kein PKCE im Implicit-Flow
    sess = pkce.pop_session(state)
    assert sess["flow"] == "sso_implicit"
    assert sess["nonce"] == q["nonce"][0]
    assert sess["next_url"] == "/x"


# ── id_token-Verifikation ───────────────────────────────────────────────────────

def _keypair():
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


class _SigningKey:
    def __init__(self, pub):
        self.key = pub


class _JWKS:
    def __init__(self, pub):
        self._pub = pub

    def get_signing_key_from_jwt(self, token):
        return _SigningKey(self._pub)


def _setup(monkeypatch, pub, client_id="CID", tenant="TID"):
    monkeypatch.setattr(sso.settings_store, "get",
                        lambda k, *a, **kw: {"BOOTSTRAP_CLIENT_ID": client_id,
                                             "TENANT_ID": tenant}.get(k))
    monkeypatch.setattr(sso, "_jwks_client", lambda uri: _JWKS(pub))


def _token(priv, **over):
    import jwt
    now = int(time.time())
    claims = {"aud": "CID", "iss": "https://login.microsoftonline.com/TID/v2.0",
              "tid": "TID", "exp": now + 300, "iat": now, "nonce": "N1",
              "oid": "OID-1", "preferred_username": "erika@zarenko.net"}
    claims.update(over)
    return jwt.encode(claims, priv, algorithm="RS256")


def test_verify_ok(monkeypatch):
    priv, pub = _keypair()
    _setup(monkeypatch, pub)
    c = sso.verify_id_token(_token(priv), "N1")
    assert c and c["oid"] == "OID-1" and c["preferred_username"] == "erika@zarenko.net"


def test_verify_falscher_nonce(monkeypatch):
    priv, pub = _keypair()
    _setup(monkeypatch, pub)
    assert sso.verify_id_token(_token(priv), "ANDERER") is None


def test_verify_falsche_audience(monkeypatch):
    priv, pub = _keypair()
    _setup(monkeypatch, pub)
    assert sso.verify_id_token(_token(priv, aud="FREMD"), "N1") is None


def test_verify_fremder_tenant(monkeypatch):
    priv, pub = _keypair()
    _setup(monkeypatch, pub)
    assert sso.verify_id_token(_token(priv, tid="FREMDE-TID"), "N1") is None


def test_verify_abgelaufen(monkeypatch):
    priv, pub = _keypair()
    _setup(monkeypatch, pub)
    now = int(time.time())
    assert sso.verify_id_token(_token(priv, exp=now - 100, iat=now - 400), "N1") is None


def test_verify_falsche_signatur(monkeypatch):
    priv, pub = _keypair()
    fremd_priv, _ = _keypair()
    _setup(monkeypatch, pub)                   # JWKS liefert pub des ERSTEN Schlüssels
    assert sso.verify_id_token(_token(fremd_priv), "N1") is None   # signiert mit fremdem
