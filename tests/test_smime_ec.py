"""S/MIME mit EC-Zertifikaten (z. B. CASTLE, P-256): verschlüsseln und entschlüsseln.

Bis v1.11.35 konnte das Gateway an EC-Empfänger nicht verschlüsseln (eigener
CMS-Weg nur RSA, `openssl smime` baut PKCS#7 ohne ECDH) und eine Hülle mit
EC-Empfänger nicht entschlüsseln (`openssl smime -decrypt` liest sie nicht —
auch dann nicht, wenn der eigene Empfänger darin RSA ist).
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import smime_decrypt  # noqa: E402
import smime_encrypt  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="openssl fehlt")

MAIL = (b'Content-Type: multipart/related; boundary="b"\r\n\r\n--b\r\n'
        b"Content-Type: text/html\r\n\r\n<p>Geheim</p>\r\n--b\r\n"
        b"Content-Type: image/png\r\nContent-ID: <i1>\r\n\r\nPNGDATA\r\n--b--\r\n")


def _zert(tmp_path, name, art):
    k, c = tmp_path / f"{name}.key", tmp_path / f"{name}.pem"
    neu = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256"] if art == "ec" \
        else ["-newkey", "rsa:2048"]
    subprocess.run(["openssl", "req", "-x509", *neu, "-nodes", "-keyout", str(k),
                    "-out", str(c), "-subj", f"/CN={name}@example.org", "-days", "2"],
                   check=True, capture_output=True)
    return c, k


@pytest.fixture
def pruefungen_aus(monkeypatch):
    import smime_store
    import settings_store
    monkeypatch.setattr(smime_store, "zeitlich_gueltig", lambda p: (True, ""))
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: False if k == "CRL_CHECK" else None)


def _verschluesseln(monkeypatch, pfade):
    import smime_store
    monkeypatch.setattr(smime_store, "get_recipient_cert_path", lambda r: pfade[r])
    return smime_encrypt.encrypt(MAIL, list(pfade))


def _entschluesseln(monkeypatch, verschl, c, k):
    import smime_store
    monkeypatch.setattr(smime_store, "get_signing_paths", lambda *a, **kw: (c, k))
    monkeypatch.setattr(smime_store, "openssl_passin", lambda: ([], None))
    return smime_decrypt._decrypt_local(verschl, "x@example.org")


@pytest.mark.parametrize("arten", [("ec",), ("ec", "rsa"), ("rsa",)])
def test_verschluesseln_und_jeder_empfaenger_entschluesselt(tmp_path, monkeypatch,
                                                            pruefungen_aus, arten):
    zerts = {f"{a}{i}@example.org": _zert(tmp_path, f"{a}{i}", a) for i, a in enumerate(arten)}
    verschl, fehlend = _verschluesseln(monkeypatch, {r: c for r, (c, _) in zerts.items()})
    assert verschl and fehlend == []
    assert b"Geheim" not in verschl and b"pkcs7-mime" in verschl
    for c, k in zerts.values():
        klar = _entschluesseln(monkeypatch, verschl, c, k)
        # alle MIME-Teile erhalten, nicht nur der erste
        assert klar is not None and b"<p>Geheim</p>" in klar and b"PNGDATA" in klar
