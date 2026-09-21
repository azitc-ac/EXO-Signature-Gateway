"""auth.pfx wird 600 geschrieben (privater EXO-Auth-Schlüssel, passwortlos).

Eine Schreibstelle für BEIDE Wege (Setup-Flow + Rotier-Endpunkt) → dieselbe
Härtung. Früher schrieben beide mit `write_bytes` (umask 644) — der private
Schlüssel lag welt-lesbar (Audit-Fund ⑤). Der Test schlägt fehl, wenn jemand die
sichere Schreibweise zurückbaut.
"""
import setup_wizard


def test_write_auth_pfx_setzt_600(monkeypatch, tmp_path):
    p = tmp_path / "auth.pfx"
    monkeypatch.setattr(setup_wizard, "_AUTH_CERT_PATH", p)
    setup_wizard._write_auth_pfx(b"pfx-bytes")
    assert p.read_bytes() == b"pfx-bytes"
    assert oct(p.stat().st_mode & 0o777) == "0o600"
