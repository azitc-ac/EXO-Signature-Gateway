"""
S/MIME digital signing via openssl smime subprocess.

Produces multipart/signed output (detached signature, clear-text readable).
openssl is available in the container image (installed with certbot dependencies).
"""

import email as _email_mod
import logging
import subprocess
import tempfile
from pathlib import Path

import config
import loop_detector
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

        cmd = [
            "openssl", "smime", "-sign",
            "-in", tmp_path,
            "-signer", str(cert_path),
            "-inkey", str(key_path),
            "-md", "sha256",
            "-outform", "SMIME",
        ]
        if config.SMIME_KEY_PASSWORD:
            cmd += ["-passin", f"pass:{config.SMIME_KEY_PASSWORD}"]
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=15,
        )
        Path(tmp_path).unlink(missing_ok=True)

        if result.returncode != 0:
            log.error("openssl smime sign failed for %s: %s",
                      sender, result.stderr.decode(errors="replace"))
            return None

        # openssl puts envelope headers (To, From, Subject, …) only inside the
        # signed content, not on the outer multipart/signed wrapper. Graph API
        # needs them on the outer message to route the mail correctly.
        # Re-serialise via Python email library to add missing headers, then
        # normalise to CRLF (openssl outputs bare LF on Linux).
        original = _email_mod.message_from_bytes(message_bytes)
        signed_msg = _email_mod.message_from_bytes(result.stdout)
        for hdr in ("From", "To", "Cc", "Subject", "Date", "Message-ID",
                    "Reply-To", "In-Reply-To", "References"):
            if not signed_msg.get(hdr) and original.get(hdr):
                signed_msg[hdr] = original[hdr]

        # Mark the outer wrapper so the EXO transport rule recognises this as
        # already processed and does not route it back through our connector.
        loop_detector.mark_as_signed(signed_msg)

        out = signed_msg.as_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        # Normalize legacy x- MIME type names to RFC 3851/5751 names
        out = out.replace(b"x-pkcs7-signature", b"pkcs7-signature")

        import stats
        stats.increment("smime_signed")
        log.info("S/MIME signed for sender=%s", sender)
        return out

    except Exception as exc:
        log.error("S/MIME signing error for %s: %s", sender, exc)
        return None
