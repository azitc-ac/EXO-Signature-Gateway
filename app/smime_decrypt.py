"""S/MIME decryption via openssl smime -decrypt."""
import logging
import subprocess

import config
import smime_store

log = logging.getLogger(__name__)


def decrypt(message_bytes: bytes, recipient: str) -> bytes | None:
    """
    Decrypt an S/MIME enveloped-data message using the recipient's private key.
    The private key must exist in the signing cert store (same cert used for outbound signing).
    Returns the decrypted inner MIME bytes, or None on failure.
    """
    paths = smime_store.get_signing_paths(recipient, allow_backup=True)
    if not paths:
        log.warning("S/MIME decrypt: no private key for %s", recipient)
        return None

    cert_path, key_path = paths
    try:
        cmd = [
            "openssl", "smime", "-decrypt",
            "-recip", str(cert_path),
            "-inkey", str(key_path),
        ]
        if config.SMIME_KEY_PASSWORD:
            cmd += ["-passin", f"pass:{config.SMIME_KEY_PASSWORD}"]
        result = subprocess.run(
            cmd,
            input=message_bytes,
            capture_output=True,
            timeout=15,
        )
        if result.returncode != 0:
            log.error("openssl smime -decrypt failed for %s: %s",
                      recipient, result.stderr.decode(errors="replace")[:400])
            return None

        log.info("S/MIME decrypted for recipient=%s", recipient)
        return result.stdout

    except Exception as exc:
        log.error("S/MIME decryption error for %s: %s", recipient, exc)
        return None
