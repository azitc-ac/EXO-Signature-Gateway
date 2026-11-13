"""Key Vault antwortet 403: kein „Schlüssel fehlt“ (v1.12.3).

ANLASS (13.11.2026, DEV-Gateway): GET …/keys/smime-… → 403, danach „S/MIME
signed for sender=…“. `key_exists` wertete 403 wie 404; das Signieren nahm
still den lokalen Schlüssel — auch bei KV_KEY_MODE=strict, das genau diesen
Rückfall verbietet. Ein Rechteproblem am Vault blieb unsichtbar.
"""
import asyncio

import pytest

import keyvault
import settings_store
import smime_signer


class _Antwort:
    def __init__(self, code):
        self.status_code = code


def _vault(monkeypatch, code=None, fehler=None, token="tok"):
    monkeypatch.setattr(keyvault, "is_configured", lambda: True)
    monkeypatch.setattr(keyvault, "vault_url", lambda: "https://v.vault.azure.net")

    async def _token():
        return token
    monkeypatch.setattr(keyvault, "_get_kv_token", _token)

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            if fehler:
                raise fehler
            return _Antwort(code)
    monkeypatch.setattr(keyvault.httpx, "AsyncClient", _Client)


@pytest.mark.parametrize("code,erwartet", [
    (200, keyvault.KEY_VORHANDEN), (404, keyvault.KEY_FEHLT),
    (403, keyvault.KEY_VERWEIGERT), (500, keyvault.KEY_FEHLER),
])
def test_zustand_je_antwort(monkeypatch, code, erwartet):
    _vault(monkeypatch, code)
    assert asyncio.run(keyvault.key_zustand("max@firma.de")) == erwartet
    assert asyncio.run(keyvault.key_exists("max@firma.de")) is (erwartet == keyvault.KEY_VORHANDEN)


def test_ohne_token_oder_netz_ist_es_ein_fehler(monkeypatch):
    _vault(monkeypatch, token=None)
    assert asyncio.run(keyvault.key_zustand("max@firma.de")) == keyvault.KEY_FEHLER
    _vault(monkeypatch, fehler=OSError("weg"))
    assert asyncio.run(keyvault.key_zustand("max@firma.de")) == keyvault.KEY_FEHLER


def _signieren(monkeypatch, modus):
    werte = {"KV_KEY_MODE": modus}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None, *a, **kw: werte.get(k, d))
    lokal = []
    monkeypatch.setattr(smime_signer, "sign",
                        lambda b, s, use_backup=False: lokal.append(s) or b"SIGNIERT")
    return asyncio.run(smime_signer.sign_async(b"mail", "max@firma.de")), lokal


def test_strict_signiert_bei_403_nicht_lokal(monkeypatch):
    _vault(monkeypatch, 403)
    ergebnis, lokal = _signieren(monkeypatch, "strict")
    assert ergebnis is None and lokal == []


def test_rueckfall_signiert_bei_403_lokal_mit_warnung(monkeypatch, caplog):
    import logging
    _vault(monkeypatch, 403)
    with caplog.at_level(logging.WARNING):
        ergebnis, lokal = _signieren(monkeypatch, "fallback")
    assert ergebnis == b"SIGNIERT" and lokal == ["max@firma.de"]
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "403" in text and "nicht lesbar" in text


def test_fehlender_schluessel_signiert_lokal_auch_strict(monkeypatch):
    """404 bleibt „kein Schlüssel im Vault“: lokaler Schlüssel wie bisher."""
    _vault(monkeypatch, 404)
    ergebnis, lokal = _signieren(monkeypatch, "strict")
    assert ergebnis == b"SIGNIERT" and lokal == ["max@firma.de"]


# ── Übersicht: „nicht feststellbar“ statt „nicht im Tresor“ ───────────────────

from test_watchdog_config import anlage  # noqa: E402,F401


def test_status_abfrage_meldet_403_als_unbekannt(anlage, monkeypatch):
    c, werte = anlage
    werte["MAILBOX_CONFIG"] = {"a@firma.de": {"smime": True}, "b@firma.de": {"smime": True},
                               "c@firma.de": {"smime": True}}
    import smime_store
    monkeypatch.setattr(smime_store, "list_certs", lambda: [])
    monkeypatch.setattr(keyvault, "is_configured", lambda: True)
    antworten = {"a@firma.de": keyvault.KEY_VORHANDEN, "b@firma.de": keyvault.KEY_FEHLT,
                 "c@firma.de": keyvault.KEY_VERWEIGERT}

    async def _zustand(email):
        return antworten[email]
    monkeypatch.setattr(keyvault, "key_zustand", _zustand)
    r = c.post("/api/smime/kv-status/refresh")
    assert r.status_code == 200
    erg = r.json()["results"]
    assert [erg[e]["exists"] for e in ("a@firma.de", "b@firma.de", "c@firma.de")] == [True, False, None]
    assert erg["c@firma.de"]["zustand"] == "verweigert"
    assert werte["KV_KEY_STATUS"] == erg
