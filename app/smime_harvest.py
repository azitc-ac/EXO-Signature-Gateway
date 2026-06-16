"""
S/MIME public key harvesting and inbound signature stripping.

Two operating modes:
  1. Harvest-only  — called when SMIME_HARVEST_RCPT is in rcpt_tos.
                     Extracts sender cert, mail is discarded.
  2. Full-process  — called for all external multipart/signed mails arriving
                     via an EXO transport rule (RouteMessageOutboundConnector).
                     Strips the signature wrapper, harvests the cert, and
                     returns the inner MIME body + signer display name so the
                     handler can re-deliver with a subject tag.
"""
import email.utils
import logging
import subprocess
import tempfile
from email.message import Message
from pathlib import Path

import smime_store

log = logging.getLogger(__name__)


# ── Public API ─────────────────────────────────────────────────────────────────

def extract_and_store(msg: Message) -> str | None:
    """Harvest-only: extract sender cert, return sender email or None."""
    sender = _sender_email(msg)
    if not sender:
        return None
    ct = msg.get_content_type().lower()
    if ct == "multipart/signed":
        protocol = (msg.get_param("protocol") or "").lower()
        if "pkcs7" not in protocol:
            return None
        sig_bytes = _sig_bytes_from_multipart(msg)
        if sig_bytes:
            _store_cert(sig_bytes, sender)
        return sender
    if ct == "application/pkcs7-mime":
        _extract_via_openssl(msg.get_payload(decode=True), sender)
        return sender
    return None


def strip_and_harvest(msg: Message, sender: str) -> tuple[bytes | None, str | None]:
    """
    Full-process: strip S/MIME signature wrapper, harvest cert, return
    (inner_mime_bytes, signer_display_name).  Returns (None, None) on failure.
    """
    ct = msg.get_content_type().lower()
    if ct != "multipart/signed":
        return None, None
    protocol = (msg.get_param("protocol") or "").lower()
    if "pkcs7" not in protocol:
        return None, None

    parts = msg.get_payload()
    if not isinstance(parts, list) or len(parts) < 2:
        return None, None

    body_part = parts[0]
    sig_bytes = _sig_bytes_from_multipart(msg)

    signer_name: str | None = None
    if sig_bytes:
        signer_name = _signer_display_name(sig_bytes, sender)
        _store_cert(sig_bytes, sender)

    try:
        inner_bytes = body_part.as_bytes()
        return inner_bytes, signer_name
    except Exception as exc:
        log.error("S/MIME strip: could not serialize inner body for %s: %s", sender, exc)
        return None, None


# ── Internals ──────────────────────────────────────────────────────────────────

def _sender_email(msg: Message) -> str | None:
    _, addr = email.utils.parseaddr(msg.get("From", ""))
    return addr.lower().strip() or None


def _sig_bytes_from_multipart(msg: Message) -> bytes | None:
    parts = msg.get_payload()
    if not isinstance(parts, list):
        return None
    sig_part = next(
        (p for p in parts if "pkcs7" in p.get_content_type().lower()),
        None,
    )
    return sig_part.get_payload(decode=True) if sig_part else None


def _store_cert(sig_bytes: bytes, sender: str) -> None:
    try:
        from cryptography.hazmat.primitives.serialization import pkcs7 as _pkcs7
        from cryptography.hazmat.primitives.serialization import Encoding

        certs = None
        for loader in (_pkcs7.load_der_pkcs7_certificates, _pkcs7.load_pem_pkcs7_certificates):
            try:
                certs = loader(sig_bytes)
                if certs:
                    break
            except Exception:
                pass

        if certs:
            smime_store.store_recipient_cert(sender, certs[0].public_bytes(Encoding.PEM))
            return
    except Exception:
        pass
    _extract_via_openssl(sig_bytes, sender)


def _signer_display_name(sig_bytes: bytes, fallback: str) -> str:
    try:
        from cryptography.hazmat.primitives.serialization import pkcs7 as _pkcs7
        from cryptography.x509.oid import NameOID

        certs = None
        for loader in (_pkcs7.load_der_pkcs7_certificates, _pkcs7.load_pem_pkcs7_certificates):
            try:
                certs = loader(sig_bytes)
                if certs:
                    break
            except Exception:
                pass

        if certs:
            cert = certs[0]
            for oid in (NameOID.COMMON_NAME, NameOID.EMAIL_ADDRESS):
                attrs = cert.subject.get_attributes_for_oid(oid)
                if attrs:
                    return attrs[0].value
    except Exception:
        pass
    return fallback


def _extract_via_openssl(data: bytes, sender: str) -> None:
    if not data:
        return
    try:
        with tempfile.NamedTemporaryFile(suffix=".p7s", delete=False) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)
        cert_path = tmp_path.with_suffix(".pem")
        result = subprocess.run(
            ["openssl", "smime", "-verify", "-noverify",
             "-in", str(tmp_path), "-inform", "DER",
             "-signer", str(cert_path), "-out", "/dev/null"],
            capture_output=True, timeout=10,
        )
        tmp_path.unlink(missing_ok=True)
        if result.returncode == 0 and cert_path.exists():
            smime_store.store_recipient_cert(sender, cert_path.read_bytes())
        cert_path.unlink(missing_ok=True)
    except Exception as exc:
        log.warning("S/MIME openssl harvest failed for %s: %s", sender, exc)
