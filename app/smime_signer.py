"""
S/MIME digital signing via openssl smime subprocess.

Produces multipart/signed output (detached signature, clear-text readable).
openssl is available in the container image (installed with certbot dependencies).
"""

import logging
import subprocess
import tempfile
from pathlib import Path

import smime_store

log = logging.getLogger(__name__)


def sign(message_bytes: bytes, sender: str) -> bytes | None:
    """
    Sign message_bytes with the sender's S/MIME certificate.
    Returns signed message bytes or None if no cert is available / signing fails.
    """
    paths = smime_store.get_signing_paths(sender)
    if not paths:
        return None

    cert_path, key_path = paths

    try:
        with tempfile.NamedTemporaryFile(suffix=".eml", delete=False) as tmp:
            tmp.write(message_bytes)
            tmp_path = tmp.name

        result = subprocess.run(
            [
                "openssl", "smime", "-sign",
                "-in", tmp_path,
                "-signer", str(cert_path),
                "-inkey", str(key_path),
                "-md", "sha256",
                "-outform", "SMIME",
            ],
            capture_output=True,
            timeout=15,
        )
        Path(tmp_path).unlink(missing_ok=True)

        if result.returncode != 0:
            log.error("openssl smime sign failed for %s: %s",
                      sender, result.stderr.decode(errors="replace"))
            return None

        log.info("S/MIME signed for sender=%s", sender)
        return result.stdout

    except Exception as exc:
        log.error("S/MIME signing error for %s: %s", sender, exc)
        return None
