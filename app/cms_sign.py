"""
CMS/PKCS#7 SignedData builder for Key Vault-backed S/MIME signing.

Produces multipart/signed output (RFC 5751) compatible with
`openssl smime -sign -outform SMIME`, but calls Azure Key Vault Sign API
instead of using a local private key.

The signed content is the full original message_bytes (normalised to CRLF),
which becomes part 1 of the multipart/signed wrapper — matching openssl
behaviour for `openssl smime -sign -in original.eml`.
"""

import base64
import email as _email_mod
import email.policy
import hashlib
import logging
import re
import secrets
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def _crlf(data: bytes) -> bytes:
    """Normalise line endings to CRLF."""
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


# ── ASN.1 / DER helpers (no asn1crypto dependency for small primitives) ───────

def _der_length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    encoded = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(encoded)]) + encoded


def _tlv(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _der_length(len(content)) + content


def _seq(content: bytes) -> bytes:
    return _tlv(0x30, content)   # SEQUENCE


def _set(content: bytes) -> bytes:
    return _tlv(0x31, content)   # SET


def _oid(encoded: bytes) -> bytes:
    return _tlv(0x06, encoded)   # OBJECT IDENTIFIER


def _integer(n: int) -> bytes:
    raw = n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")
    if raw[0] & 0x80:           # ensure positive
        raw = b"\x00" + raw
    return _tlv(0x02, raw)      # INTEGER


def _octet_string(data: bytes) -> bytes:
    return _tlv(0x04, data)     # OCTET STRING


def _bit_string(data: bytes) -> bytes:
    return _tlv(0x03, b"\x00" + data)   # BIT STRING (0 unused bits)


def _context(tag: int, content: bytes, implicit: bool = True) -> bytes:
    """Context-tagged TLV — implicit [tag] by default."""
    t = 0xA0 | tag if not implicit else (0xA0 | tag)
    return _tlv(t, content)


def _utctime(dt: datetime) -> bytes:
    s = dt.strftime("%y%m%d%H%M%SZ").encode()
    return _tlv(0x17, s)        # UTCTime


def _printable_string(s: str) -> bytes:
    return _tlv(0x13, s.encode("ascii"))


def _utf8_string(s: str) -> bytes:
    return _tlv(0x0C, s.encode("utf-8"))


# ── OID byte encodings ─────────────────────────────────────────────────────────

OID_DATA           = bytes([0x2A, 0x86, 0x48, 0x86, 0xF7, 0x0D, 0x01, 0x07, 0x01])
OID_SIGNED_DATA    = bytes([0x2A, 0x86, 0x48, 0x86, 0xF7, 0x0D, 0x01, 0x07, 0x02])
OID_SHA256         = bytes([0x60, 0x86, 0x48, 0x01, 0x86, 0xF8, 0x4D, 0x01, 0x02, 0x0A, 0x03, 0x02])
OID_SHA256_ALT     = bytes([0x60, 0x86, 0x48, 0x01, 0x65, 0x03, 0x04, 0x02, 0x01])
OID_RSA            = bytes([0x2A, 0x86, 0x48, 0x86, 0xF7, 0x0D, 0x01, 0x01, 0x01])
OID_RSAPKCS1V15    = bytes([0x2A, 0x86, 0x48, 0x86, 0xF7, 0x0D, 0x01, 0x01, 0x0B])  # sha256WithRSAEncryption
OID_ECDSA_SHA256   = bytes([0x2A, 0x86, 0x48, 0xCE, 0x3D, 0x04, 0x03, 0x02])         # ecdsa-with-SHA256
OID_CONTENT_TYPE   = bytes([0x2A, 0x86, 0x48, 0x86, 0xF7, 0x0D, 0x01, 0x09, 0x03])
OID_SIGNING_TIME   = bytes([0x2A, 0x86, 0x48, 0x86, 0xF7, 0x0D, 0x01, 0x09, 0x05])
OID_MESSAGE_DIGEST = bytes([0x2A, 0x86, 0x48, 0x86, 0xF7, 0x0D, 0x01, 0x09, 0x04])
OID_SMIME_CAP      = bytes([0x2A, 0x86, 0x48, 0x86, 0xF7, 0x0D, 0x01, 0x09, 0x0F])

# SHA-256 AlgorithmIdentifier (used many places)
def _sha256_alg_id() -> bytes:
    return _seq(_oid(OID_SHA256_ALT) + _tlv(0x05, b""))  # SHA256 + NULL


def _rsa_alg_id() -> bytes:
    return _seq(_oid(OID_RSAPKCS1V15) + _tlv(0x05, b""))  # sha256WithRSAEncryption + NULL


def _ecdsa_sha256_alg_id() -> bytes:
    # RFC 5480: ecdsa-with-SHA256 has absent (not NULL) parameters
    return _seq(_oid(OID_ECDSA_SHA256))


# ── Certificate parsing ────────────────────────────────────────────────────────

def _parse_cert_der(cert_pem: bytes) -> bytes:
    """Extract DER bytes from PEM certificate."""
    b64 = re.search(
        rb"-----BEGIN CERTIFICATE-----\s*(.*?)\s*-----END CERTIFICATE-----",
        cert_pem, re.DOTALL)
    if not b64:
        raise ValueError("Kein PEM-Zertifikat gefunden")
    return base64.b64decode(b64.group(1))


def _get_cert_issuer_serial(cert_der: bytes) -> tuple[bytes, bytes]:
    """
    Extract raw TBSCertificate issuer sequence and serial integer bytes from DER.
    We need them to build IssuerAndSerialNumber in the SignerInfo.
    """
    from cryptography import x509
    cert = x509.load_der_x509_certificate(cert_der)
    # issuer — get raw DER bytes of the Name sequence
    issuer_der = cert.issuer.public_bytes()
    serial = cert.serial_number
    return issuer_der, serial


def _get_cert_key_type(cert_der: bytes) -> str:
    """Return 'EC' or 'RSA' based on the certificate's public key type."""
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import ec
    cert = x509.load_der_x509_certificate(cert_der)
    return "EC" if isinstance(cert.public_key(), ec.EllipticCurvePublicKey) else "RSA"


def _ec_raw_to_der_sig(raw_sig: bytes) -> bytes:
    """
    Convert Key Vault ES256 raw signature (64 bytes r||s for P-256) to
    DER-encoded ECDSASignature ::= SEQUENCE { r INTEGER, s INTEGER }.
    """
    half = len(raw_sig) // 2
    r = int.from_bytes(raw_sig[:half], "big")
    s = int.from_bytes(raw_sig[half:], "big")
    return _seq(_integer(r) + _integer(s))


# ── Signed Attributes ─────────────────────────────────────────────────────────

def _build_signed_attrs(content_type_oid: bytes, msg_digest: bytes,
                         signing_time: datetime) -> bytes:
    """
    Build the SignedAttributes SET as a DER-encoded SET (tag 0x31).
    This is what is hashed to produce digest_to_sign.
    When stored in SignerInfo it gets tag 0xA0 (implicit [0]).
    """
    # Attribute: contentType
    ct_attr = _seq(
        _oid(OID_CONTENT_TYPE) +
        _set(_oid(content_type_oid))
    )
    # Attribute: signingTime
    st_attr = _seq(
        _oid(OID_SIGNING_TIME) +
        _set(_utctime(signing_time))
    )
    # Attribute: messageDigest
    md_attr = _seq(
        _oid(OID_MESSAGE_DIGEST) +
        _set(_octet_string(msg_digest))
    )
    # SET OF Attribute — order matters for DER: sort by DER encoding
    attrs = sorted([ct_attr, st_attr, md_attr])
    return _set(b"".join(attrs))


def _signed_attrs_as_implicit(signed_attrs_set: bytes) -> bytes:
    """
    Re-tag signed_attrs SET (0x31...) as IMPLICIT [0] (0xA0...)
    for embedding in SignerInfo structure.
    """
    # Replace the leading 0x31 tag with 0xA0
    if signed_attrs_set[0:1] != b"\x31":
        raise ValueError("Expected SET tag 0x31")
    return b"\xA0" + signed_attrs_set[1:]


# ── Full PKCS#7 SignedData builder ────────────────────────────────────────────

def _build_pkcs7(
    cert_der: bytes,
    signed_attrs_set: bytes,   # DER-encoded SET (tag 0x31) — used to build SignerInfo
    signature_bytes: bytes,    # signature from Key Vault (RSA: raw PKCS#1; EC: DER SEQUENCE)
    key_type: str = "RSA",     # "RSA" or "EC"
) -> bytes:
    """
    Build a DER-encoded PKCS#7 / CMS SignedData structure with detached content.
    Returns the raw DER bytes (for base64 encoding into the smime.p7s attachment).
    """
    issuer_der, serial = _get_cert_issuer_serial(cert_der)

    # IssuerAndSerialNumber
    issuer_and_serial = _seq(issuer_der + _integer(serial))

    # SignerIdentifier: [0] IssuerAndSerialNumber (version v1)
    signer_id = issuer_and_serial  # for v1, this is the direct value

    # SignedAttributes in SignerInfo use [0] IMPLICIT tag
    signed_attrs_implicit = _signed_attrs_as_implicit(signed_attrs_set)

    sig_alg_id = _ecdsa_sha256_alg_id() if key_type == "EC" else _rsa_alg_id()

    # SignerInfo ::= SEQUENCE {
    #   version CMSVersion,
    #   sid SignerIdentifier,
    #   digestAlgorithm DigestAlgorithmIdentifier,
    #   signedAttrs [0] IMPLICIT SignedAttributes OPTIONAL,
    #   signatureAlgorithm SignatureAlgorithmIdentifier,
    #   signature SignatureValue,
    #   unsignedAttrs [1] IMPLICIT UnsignedAttributes OPTIONAL }
    signer_info = _seq(
        _integer(1) +                  # version: v1
        signer_id +                    # IssuerAndSerialNumber
        _sha256_alg_id() +             # digestAlgorithm
        signed_attrs_implicit +        # signedAttrs [0]
        sig_alg_id +                   # signatureAlgorithm (RSA or ECDSA)
        _octet_string(signature_bytes)  # signature
    )

    # SignedData ::= SEQUENCE {
    #   version CMSVersion,
    #   digestAlgorithms DigestAlgorithmIdentifiers,
    #   encapContentInfo EncapsulatedContentInfo,
    #   certificates [0] IMPLICIT CertificateSet OPTIONAL,
    #   crls [1] IMPLICIT RevocationInfoChoices OPTIONAL,
    #   signerInfos SignerInfos }
    encap_content_info = _seq(_oid(OID_DATA))  # detached: no content eContent

    digest_algorithms = _set(_sha256_alg_id())

    # certificates [0] IMPLICIT — wrap cert_der in [0]
    certificates = _tlv(0xA0, cert_der)

    signer_infos = _set(signer_info)

    signed_data_content = (
        _integer(1) +              # version: v1
        digest_algorithms +
        encap_content_info +
        certificates +
        signer_infos
    )
    signed_data = _seq(signed_data_content)

    # ContentInfo ::= SEQUENCE { contentType, content [0] EXPLICIT }
    content_info = _seq(
        _oid(OID_SIGNED_DATA) +
        _tlv(0xA0, signed_data)    # [0] EXPLICIT
    )
    return content_info


# ── Public API ────────────────────────────────────────────────────────────────

async def sign_smime_keyvault(
    message_bytes: bytes,
    sender_email: str,
    cert_pem: bytes,
) -> bytes | None:
    """
    Sign an email message using Azure Key Vault.
    Returns S/MIME multipart/signed bytes or None on error.

    message_bytes: the full RFC 2822 message (as would be passed to openssl smime -sign)
    sender_email:  used to look up the key in Key Vault
    cert_pem:      PEM-encoded signer certificate
    """
    import keyvault
    import loop_detector

    try:
        cert_der = _parse_cert_der(cert_pem)
    except Exception as exc:
        log.error("cms_sign: cannot parse cert for %s: %s", sender_email, exc)
        return None

    # Normalise content to CRLF — this is what gets embedded as part 1
    content_to_sign = _crlf(message_bytes)

    # 1. SHA-256 digest of the body content
    msg_digest = hashlib.sha256(content_to_sign).digest()

    # 2. Build signed attributes SET
    signing_time = datetime.now(timezone.utc)
    signed_attrs_set = _build_signed_attrs(OID_DATA, msg_digest, signing_time)

    # 3. SHA-256 of the signed attributes SET (this is what Key Vault signs)
    digest_to_sign = hashlib.sha256(signed_attrs_set).digest()

    # 4. Call Key Vault Sign API — algorithm depends on key type (RSA→RS256, EC→ES256)
    key_type = _get_cert_key_type(cert_der)
    kv_algo = "ES256" if key_type == "EC" else "RS256"
    try:
        kv_sig = await keyvault.sign(sender_email, digest_to_sign, algorithm=kv_algo)
    except Exception as exc:
        log.error("cms_sign: Key Vault sign failed for %s: %s", sender_email, exc)
        return None
    if key_type == "EC":
        kv_sig = _ec_raw_to_der_sig(kv_sig)

    # 5. Build PKCS#7 DER structure
    try:
        pkcs7_der = _build_pkcs7(cert_der, signed_attrs_set, kv_sig, key_type=key_type)
    except Exception as exc:
        log.error("cms_sign: PKCS#7 build failed for %s: %s", sender_email, exc)
        return None

    # 6. Base64-encode the DER
    pkcs7_b64 = base64.encodebytes(pkcs7_der).decode("ascii")

    # 7. Build multipart/signed MIME wrapper
    boundary = "----=_Part_" + secrets.token_hex(10)

    # Extract envelope headers from original message for the outer wrapper
    original = _email_mod.message_from_bytes(message_bytes)

    # Build the outer multipart/signed message
    outer_lines = []
    outer_lines.append(f"MIME-Version: 1.0")
    outer_lines.append(
        f'Content-Type: multipart/signed; protocol="application/pkcs7-signature"; '
        f'micalg=sha-256; boundary="{boundary}"'
    )
    # Copy envelope headers from original message to outer wrapper
    for hdr in ("From", "To", "Cc", "Subject", "Date", "Message-ID",
                "Reply-To", "In-Reply-To", "References"):
        val = original.get(hdr)
        if val:
            outer_lines.append(f"{hdr}: {val}")

    outer_lines.append("")  # blank line = end of headers

    # Part 1: the signed content (original message bytes verbatim)
    outer_lines.append(f"--{boundary}")

    result = "\r\n".join(outer_lines).encode("utf-8")
    result += b"\r\n"  # CRLF terminating the --boundary line (required by MIME spec)
    result += content_to_sign
    result += b"\r\n"

    # Part 2: the signature
    sig_part = (
        f"--{boundary}\r\n"
        f"Content-Type: application/pkcs7-signature; name=\"smime.p7s\"\r\n"
        f"Content-Transfer-Encoding: base64\r\n"
        f"Content-Disposition: attachment; filename=\"smime.p7s\"\r\n"
        f"\r\n"
        + pkcs7_b64 +
        f"\r\n--{boundary}--\r\n"
    )
    result += sig_part.encode("ascii")

    # 8. Normalise CRLF (base64.encodebytes uses bare \n), fix pkcs7 name, inject loop header.
    # Do NOT re-parse through Python's email library — it re-serialises multipart/signed
    # and can silently drop or mangle Part 1 (the signed body content).
    result = result.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    result = result.replace(b"x-pkcs7-signature", b"pkcs7-signature")
    result = loop_detector.mark_as_signed_bytes(result)
    return result
