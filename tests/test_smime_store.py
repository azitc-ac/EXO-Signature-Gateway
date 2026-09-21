"""S/MIME-Signaturzert-Lebenszyklus — zwei Bugs derselben Klasse wie der LE-Bug.

Fund ①: `store_p12_slot` setzte den Default-Zeiger nur, wenn keiner existierte →
  eine Erneuerung per PFX-Upload legte einen neuen Slot an, blieb aber wirkungslos
  (weiter mit dem alten Zert signiert). Muss IMMER der neue Default werden.
Fund ②: Der Konfig-Export las das leere Altlayout `user_dir/cert.pem` → exportierte
  still null Zertifikate. Muss die SLOT-Struktur erfassen; Import muss sie
  slot-basiert zurückschreiben und als Default setzen.

Die Tests schlagen bei Rückbau fehl — genau das ist der Sinn.
"""
import datetime

import pytest

import smime_store


def _cert(cn):
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365))
            .sign(key, hashes.SHA256()))
    return key, cert


def _pem(key, cert):
    from cryptography.hazmat.primitives import serialization as s
    return (cert.public_bytes(s.Encoding.PEM),
            key.private_bytes(s.Encoding.PEM, s.PrivateFormat.PKCS8, s.NoEncryption()))


def _pfx(cn):
    from cryptography.hazmat.primitives.serialization import pkcs12, NoEncryption
    key, cert = _cert(cn)
    return pkcs12.serialize_key_and_certificates(b"t", key, cert, None, NoEncryption()), cert


def _fp(cert):
    from cryptography.hazmat.primitives import hashes
    return cert.fingerprint(hashes.SHA256())


def _served_cert(email):
    from cryptography import x509
    paths = smime_store.get_signing_paths(email)
    assert paths is not None, "kein serviertes Zert gefunden"
    return x509.load_pem_x509_certificate(paths[0].read_bytes()), paths


EMAIL = "u@example.org"


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(smime_store, "SMIME_DIR", tmp_path)
    return tmp_path


def test_p12_upload_wird_immer_neuer_default(store):
    """Fund ①: der zweite Upload (Erneuerung) muss das servierte Zert werden."""
    pfxA, _ = _pfx("A")
    pfxB, certB = _pfx("B")
    smime_store.store_p12_slot(EMAIL, pfxA, "")
    smime_store.store_p12_slot(EMAIL, pfxB, "")
    served, _ = _served_cert(EMAIL)
    assert _fp(served) == _fp(certB), "die Erneuerung (B) muss der aktive Default sein"


def test_import_signing_pem_landet_im_slot_und_wird_serviert(store):
    """Fund ② (Import): roh importiertes Zert landet slot-basiert, wird Default, 600."""
    key, cert = _cert("imp")
    cert_pem, key_pem = _pem(key, cert)
    smime_store.import_signing_pem(EMAIL, cert_pem, key_pem)
    served, paths = _served_cert(EMAIL)
    assert _fp(served) == _fp(cert)
    assert oct(paths[1].stat().st_mode & 0o777) == "0o600"
    # slot-basiert, NICHT flaches Altlayout
    assert (store / EMAIL / "certs").exists()
    assert not (store / EMAIL / "cert.pem").exists()


def test_config_export_erfasst_slot_zertifikat(store):
    """Fund ② (Export): der Config-Export muss slot-basierte Zerts enthalten —
    mit dem alten Altlayout-Code exportierte die Schleife null Zertifikate."""
    import asyncio
    import xml.etree.ElementTree as ET
    key, cert = _cert("exp")
    cert_pem, key_pem = _pem(key, cert)
    smime_store.store_pem_slot(EMAIL, cert_pem, key_pem)   # slot-basiert wie ACME

    from webui.app import api_config_export

    async def _collect():
        resp = await api_config_export(user="admin")
        chunks = []
        async for c in resp.body_iterator:
            chunks.append(c if isinstance(c, bytes) else c.encode())
        return b"".join(chunks)

    body = asyncio.run(_collect())
    root = ET.fromstring(body)
    emails = [e.get("email") for e in root.findall("smime-signing-cert")]
    assert EMAIL in emails, "Export muss das slot-basierte Signaturzert enthalten"
