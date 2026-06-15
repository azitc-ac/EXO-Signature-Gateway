"""
S/MIME certificate store — per-user cert + private key management.

Storage: /app/data/smime/{email}/cert.pem  (public certificate)
         /app/data/smime/{email}/key.pem   (private key, unencrypted for auto-signing)

Import via PKCS12 (.p12/.pfx) upload which contains both cert and key.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, pkcs12,
)

log = logging.getLogger(__name__)
SMIME_DIR = Path("/app/data/smime")


def _get_expiry(cert) -> datetime:
    """Compatible with cryptography 41 (naive) and 42+ (aware)."""
    try:
        return cert.not_valid_after_utc
    except AttributeError:
        return cert.not_valid_after.replace(tzinfo=timezone.utc)


def _cert_info(cert: x509.Certificate, email: str) -> dict:
    now = datetime.now(timezone.utc)
    expiry = _get_expiry(cert)
    days_left = (expiry - now).days
    return {
        "email": email,
        "subject": cert.subject.rfc4514_string(),
        "expiry": expiry.strftime("%d.%m.%Y"),
        "days_left": days_left,
        "expired": days_left < 0,
        "warning": 0 <= days_left < 30,
    }


def store_p12(email: str, p12_bytes: bytes, password: str = "") -> dict:
    """
    Import a PKCS12 bundle for a user.
    Extracts cert + key and stores as unencrypted PEM files.
    Returns cert info dict. Raises ValueError on bad input.
    """
    pw = password.encode() if password else None
    try:
        private_key, cert, _chain = pkcs12.load_key_and_certificates(p12_bytes, pw)
    except Exception as exc:
        raise ValueError(f"Ungültiges PKCS12 oder falsches Passwort: {exc}") from exc

    if not private_key or not cert:
        raise ValueError("PKCS12 muss privaten Schlüssel und Zertifikat enthalten")

    user_dir = SMIME_DIR / email.lower().strip()
    user_dir.mkdir(parents=True, exist_ok=True)

    (user_dir / "cert.pem").write_bytes(cert.public_bytes(Encoding.PEM))
    (user_dir / "key.pem").write_bytes(
        private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )
    log.info("S/MIME cert stored for %s — %s", email, cert.subject.rfc4514_string())
    return _cert_info(cert, email)


def get_signing_paths(email: str) -> tuple[Path, Path] | None:
    """Return (cert_path, key_path) or None if no cert exists for this email."""
    user_dir = SMIME_DIR / email.lower().strip()
    cert_path = user_dir / "cert.pem"
    key_path = user_dir / "key.pem"
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path
    return None


def list_certs() -> list[dict]:
    """Return info dicts for all users that have a certificate."""
    result = []
    if not SMIME_DIR.exists():
        return result
    for user_dir in sorted(SMIME_DIR.iterdir()):
        cert_path = user_dir / "cert.pem"
        if not cert_path.exists():
            continue
        try:
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            result.append(_cert_info(cert, user_dir.name))
        except Exception as exc:
            result.append({"email": user_dir.name, "error": str(exc),
                           "subject": "–", "expiry": "–", "days_left": 0,
                           "expired": True, "warning": False})
    return result


def delete_cert(email: str) -> None:
    user_dir = SMIME_DIR / email.lower().strip()
    for name in ("cert.pem", "key.pem"):
        p = user_dir / name
        if p.exists():
            p.unlink()
    if user_dir.exists() and not any(user_dir.iterdir()):
        user_dir.rmdir()
    log.info("S/MIME cert deleted for %s", email)
