"""auth.pfx wird 600 geschrieben (privater EXO-Auth-Schlüssel, passwortlos).

Eine Schreibstelle für BEIDE Wege (Setup-Flow + Rotier-Endpunkt) → dieselbe
Härtung. Früher schrieben beide mit `write_bytes` (umask 644) — der private
Schlüssel lag welt-lesbar (Audit-Fund ⑤). Der Test schlägt fehl, wenn jemand die
sichere Schreibweise zurückbaut.
"""
import asyncio

import pytest

import setup_wizard


def test_write_auth_pfx_setzt_600(monkeypatch, tmp_path):
    p = tmp_path / "auth.pfx"
    monkeypatch.setattr(setup_wizard, "_AUTH_CERT_PATH", p)
    setup_wizard._write_auth_pfx(b"pfx-bytes")
    assert p.read_bytes() == b"pfx-bytes"
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_gen_auth_cert_ersetzt_bestehendes_nicht_still(monkeypatch, tmp_path):
    """Fund ③: der manuelle „generieren"-Weg lädt nicht in Entra hoch. Ein
    bestehendes Auth-Zert darf er deshalb NICHT ersetzen (sonst bräche
    Connect-ExchangeOnline still) — er verweist auf den Auto-Weg. Nur bei
    fehlendem Zert (Reparatur) darf er generieren."""
    from fastapi import HTTPException
    from webui.routen import setup as setup_routen
    p = tmp_path / "auth.pfx"
    p.write_bytes(b"existing")
    monkeypatch.setattr(setup_wizard, "_AUTH_CERT_PATH", p)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(setup_routen.api_gen_auth_cert(request=None, user="admin"))
    assert ei.value.status_code == 409
    assert p.read_bytes() == b"existing"     # das laufende Zert bleibt unangetastet
