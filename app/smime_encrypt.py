"""S/MIME encryption — Python CMS (cryptography + asn1crypto) primary, openssl fallback."""
import base64
import logging
import os
import subprocess
import tempfile
from pathlib import Path

import smime_store

log = logging.getLogger(__name__)

# DER OID for id-envelopedData: 1.2.840.113549.1.7.3
_OID_ENVELOPED_DATA = bytes([
    0x06, 0x09,
    0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x07, 0x03,
])


def _der_len(n: int) -> bytes:
    """Minimal DER length encoding."""
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _pem_to_der(data: bytes) -> bytes:
    if b"-----BEGIN" not in data:
        return data
    lines = [l for l in data.splitlines() if l and not l.startswith(b"-----")]
    return base64.b64decode(b"".join(lines))


def encrypt(message_bytes: bytes, recipients: list[str]) -> tuple[bytes | None, list[str]]:
    """
    Encrypt message_bytes for all recipients (AES-256-CBC).

    Returns (encrypted_bytes, missing_emails).
    If any recipient cert is missing → (None, [missing...]).
    If encryption fails → (None, []).
    """
    cert_paths: list[Path] = []
    missing: list[str] = []

    for rcpt in recipients:
        path = smime_store.get_recipient_cert_path(rcpt)
        if path:
            cert_paths.append(path)
        else:
            missing.append(rcpt)

    if missing:
        return None, missing

    # Primary: native Python CMS — preserves every MIME part inside the CMS envelope
    try:
        result = _encrypt_cms(message_bytes, cert_paths)
        if result:
            import stats
            stats.increment("smime_encrypted")
            log.info("S/MIME encrypted (CMS) for %d recipient(s)", len(recipients))
            return result, []
    except Exception as exc:
        log.warning("CMS encrypt failed — falling back to openssl: %s", exc)

    # Fallback: openssl smime (only preserves first MIME part — CID images lost)
    return _encrypt_openssl(message_bytes, [str(p) for p in cert_paths])


def _encrypt_cms(message_bytes: bytes, cert_paths: list[Path]) -> bytes | None:
    """
    Build a CMS EnvelopedData that wraps ALL MIME parts intact.

    The entire MIME byte stream (multipart/related with CID images included) is
    encrypted as a single octet blob. Openssl smime -encrypt only took the first
    MIME part, silently dropping inline image parts. This implementation feeds the
    raw bytes to AES-256-CBC directly — nothing is stripped.
    """
    from cryptography import x509 as _x509
    from cryptography.hazmat.primitives.asymmetric import padding as _apad
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7 as _PKCS7
    from asn1crypto import cms as _cms, core as _core, x509 as _asn1_x509

    # ── 1. AES-256-CBC session key + IV ─────────────────────────────────────
    session_key = os.urandom(32)
    iv = os.urandom(16)

    padder = _PKCS7(128).padder()
    padded = padder.update(message_bytes) + padder.finalize()
    cipher = Cipher(algorithms.AES(session_key), modes.CBC(iv))
    enc = cipher.encryptor()
    enc_content = enc.update(padded) + enc.finalize()

    # ── 2. KeyTransRecipientInfo per recipient (RSA PKCS#1 v1.5) ────────────
    ri_list = []
    for cert_path in cert_paths:
        der = _pem_to_der(cert_path.read_bytes())

        crypt_cert = _x509.load_der_x509_certificate(der)
        pub_key = crypt_cert.public_key()
        if not isinstance(pub_key, RSAPublicKey):
            raise TypeError(
                f"Non-RSA recipient cert not supported in CMS encrypt: {cert_path.name}"
            )
        enc_key = pub_key.encrypt(session_key, _apad.PKCS1v15())

        asn1_cert = _asn1_x509.Certificate.load(der)
        tbs = asn1_cert["tbs_certificate"]

        ktri = _cms.KeyTransRecipientInfo({
            "version": _cms.CMSVersion(0),
            "rid": _cms.RecipientIdentifier(
                name="issuer_and_serial_number",
                value=_cms.IssuerAndSerialNumber({
                    "issuer": tbs["issuer"],
                    "serial_number": tbs["serial_number"],
                }),
            ),
            "key_encryption_algorithm": _cms.KeyEncryptionAlgorithm({
                "algorithm": _cms.KeyEncryptionAlgorithmId("rsa"),
            }),
            "encrypted_key": _core.OctetString(enc_key),
        })
        ri_list.append(_cms.RecipientInfo(name="ktri", value=ktri))

    # ── 3. EncryptedContentInfo ──────────────────────────────────────────────
    eci = _cms.EncryptedContentInfo({
        "content_type": _cms.ContentType("data"),
        "content_encryption_algorithm": _cms.EncryptionAlgorithm({
            "algorithm": _cms.EncryptionAlgorithmId("aes256_cbc"),
            "parameters": _core.OctetString(iv),
        }),
        "encrypted_content": _core.OctetString(enc_content),
    })

    # ── 4. EnvelopedData ─────────────────────────────────────────────────────
    env = _cms.EnvelopedData({
        "version": _cms.CMSVersion(0),
        "recipient_infos": _cms.RecipientInfos(ri_list),
        "encrypted_content_info": eci,
    })

    # ── 5. ContentInfo — manual DER (avoids asn1crypto OID-keyed Any quirks) ─
    env_der = env.dump()
    body = _OID_ENVELOPED_DATA + b"\xa0" + _der_len(len(env_der)) + env_der
    ci_der = b"\x30" + _der_len(len(body)) + body

    # ── 6. MIME wrapper ──────────────────────────────────────────────────────
    b64 = base64.encodebytes(ci_der).decode("ascii")
    return (
        b"MIME-Version: 1.0\r\n"
        b'Content-Disposition: attachment; filename="smime.p7m"\r\n'
        b'Content-Type: application/pkcs7-mime;'
        b' smime-type=enveloped-data; name="smime.p7m"\r\n'
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n"
        + b64.encode("ascii")
    )


def _encrypt_openssl(
    message_bytes: bytes, cert_path_strs: list[str]
) -> tuple[bytes | None, list[str]]:
    """Fallback via openssl smime -encrypt. Only preserves first MIME part."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".eml", delete=False) as tmp:
            tmp.write(message_bytes)
            tmp_path = Path(tmp.name)

        result = subprocess.run(
            ["openssl", "smime", "-encrypt", "-aes256",
             "-in", str(tmp_path)] + cert_path_strs,
            capture_output=True,
            timeout=15,
        )
        tmp_path.unlink(missing_ok=True)

        if result.returncode != 0:
            log.error("openssl smime -encrypt failed: %s",
                      result.stderr.decode(errors="replace")[:400])
            return None, []

        import stats
        stats.increment("smime_encrypted")
        log.info("S/MIME encrypted (openssl fallback) for %d recipient(s)",
                 len(cert_path_strs))
        out = result.stdout.replace(b"application/x-pkcs7-mime",
                                    b"application/pkcs7-mime")
        return out, []

    except Exception as exc:
        log.error("S/MIME openssl encryption error: %s", exc)
        return None, []
