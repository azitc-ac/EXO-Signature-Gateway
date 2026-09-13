"""Der Gesundheits-Check darf NICHT als Nachrichten-Signatur zählen.

Hintergrund: „Key Vault Signaturen" im Tagesbericht wurde von der halbstündlichen
Schlüssel-Probe des Health-Checks mit hochgezählt — an mailfreien Tagen standen
dort ~48 „Signaturen" ohne eine einzige Mail. Dieser Test schlägt fehl, wenn die
Probe wieder als echte Signatur (`zaehlen=True`) gezählt würde.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import health_check
import keyvault


def test_health_probe_ruft_sign_mit_zaehlen_false(monkeypatch):
    erfasst = {}

    async def fake_sign(email, digest_bytes, algorithm="RS256", zaehlen=True):
        erfasst["zaehlen"] = zaehlen
        return b"sig"

    monkeypatch.setattr(keyvault, "sign", fake_sign)
    res = asyncio.run(health_check._check_kv_sign("a@f.de"))
    assert res["status"] == "ok"
    assert erfasst.get("zaehlen") is False        # Probe zählt NICHT als Signatur


def test_kv_key_pruefung_ist_ein_eigener_zaehler():
    import stats
    assert "kv_key_pruefung" in stats.KEYS         # separater Ausweis der Proben
    assert "kv_sign_calls" in stats.KEYS
