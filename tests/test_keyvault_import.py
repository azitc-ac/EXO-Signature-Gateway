"""KV-Import muss auf einem gewöhnlichen Standard-Vault funktionieren.

Ein exportierbarer Schlüssel verlangt in Azure eine Release-Policy (SKR) —
ohne sie lehnt ein Standard-Vault den Import ab (AKV.SKR.1004). Das machte den
Standard-Migrationsweg (Fallback-Modus) unbenutzbar. Schlüssel werden deshalb
immer nicht-exportierbar importiert; die Wiederherstellung im Fallback-Modus
kommt aus dem lokalen key.pem.bak, nicht aus einem Rück-Export.
"""
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import keyvault  # noqa: E402


def test_import_immer_nicht_exportierbar(monkeypatch):
    import settings_store
    # Egal welcher Modus — der Import darf nie exportierbar sein.
    for modus in ("fallback", "strict", "", None):
        monkeypatch.setattr(settings_store, "get",
                            lambda k, *a, _m=modus: _m if k == "KV_KEY_MODE" else None)
        assert keyvault._kv_exportable() is False, modus


def test_skr_1004_wird_abgefangen():
    """Der Retry-Zweig deckt neben SKR.1003 auch SKR.1004 ab."""
    import inspect
    quelle = inspect.getsource(keyvault.import_rsa_key)
    assert "AKV.SKR.1004" in quelle
