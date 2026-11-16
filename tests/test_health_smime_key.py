"""Der smime_key-Health-Check darf „Key Vault" nur melden, wenn für DIESES
Postfach wirklich ein Schlüssel im Vault liegt.

Regression: Ein global gesetztes KEYVAULT_URL genügte für „ok/Key Vault" —
auch bei einem Postfach ganz ohne Schlüssel. Das widersprach dem cert=fehler-
und kv_sign=skip-Status und verschleierte einen fehlenden Schlüssel.
"""
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import health_check  # noqa: E402


def test_smime_key_error_ohne_jeden_schluessel(monkeypatch):
    import smime_store
    monkeypatch.setattr(smime_store, "get_signing_paths", lambda *a, **k: None)
    monkeypatch.setattr(smime_store, "default_key_location", lambda e: "none")
    r = health_check._check_smime_key("erika@example.com")
    assert r["status"] == "error"


def _vault(monkeypatch, url):
    import settings_store
    alt = settings_store.get
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a, **kw: url if k == "KEYVAULT_URL" else alt(k, *a, **kw))


def test_smime_key_ok_wenn_im_vault(monkeypatch):
    import smime_store
    monkeypatch.setattr(smime_store, "get_signing_paths", lambda *a, **k: None)
    monkeypatch.setattr(smime_store, "default_key_location", lambda e: "kv")
    _vault(monkeypatch, "https://kv-test.vault.azure.net")
    r = health_check._check_smime_key("erika@example.com")
    assert r["status"] == "ok"
    assert "Key Vault" in r["detail"]


def test_smime_key_ohne_vault_kein_ok(monkeypatch):
    """„kv“ heisst nur: Zertifikat da, lokal kein Schlüssel. Ohne Key Vault liegt
    er nirgends — bis v1.12.42 stand trotzdem „ok / Key Vault“."""
    import smime_store
    monkeypatch.setattr(smime_store, "get_signing_paths", lambda *a, **k: None)
    monkeypatch.setattr(smime_store, "default_key_location", lambda e: "kv")
    _vault(monkeypatch, "")
    r = health_check._check_smime_key("erika@example.com")
    assert r["status"] == "error" and "kein Key Vault" in r["detail"]
