"""Auth-Zertifikat an der App: Gültigkeit aus dem Zertifikat, nicht neu gerechnet.

Entra weist ein endDateTime hinter dem Ablauf des Zertifikats mit
„KeyCredentialsInvalidEndDate“ ab. Bis v1.11.53 stand dort „jetzt + 3650 Tage“,
gerechnet NACH dem Erzeugen des Zertifikats — gemessen 12.11.2026 auf dem
Raspi: Einrichtung abgebrochen.
"""
import asyncio
import time

import setup_wizard


def test_ende_gleich_ablauf_des_zertifikats(monkeypatch):
    der, _pfx = setup_wizard._generate_auth_cert()
    time.sleep(1.1)                       # Upload kommt nach dem Erzeugen
    gesendet = {}

    async def _gh(methode, url, token, json=None):
        gesendet.update(json or {})
        return {}
    monkeypatch.setattr(setup_wizard, "_gh", _gh)
    asyncio.run(setup_wizard._upload_key_credential("t", "obj", der))
    eintrag = gesendet["keyCredentials"][0]
    from cryptography import x509
    zert = x509.load_der_x509_certificate(der)
    ende = getattr(zert, "not_valid_after_utc", None) or zert.not_valid_after
    assert eintrag["endDateTime"] == ende.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert eintrag["startDateTime"] <= eintrag["endDateTime"]
