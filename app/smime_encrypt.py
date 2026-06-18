"""S/MIME encryption via openssl smime -encrypt."""
import logging
import subprocess
import tempfile
from pathlib import Path

import smime_store

log = logging.getLogger(__name__)


def encrypt(message_bytes: bytes, recipients: list[str]) -> tuple[bytes | None, list[str]]:
    """
    Encrypt message_bytes for all recipients using AES-256.

    Returns (encrypted_bytes, missing_emails).
    If any recipient cert is missing, returns (None, [missing...]).
    If encryption fails, returns (None, []).
    """
    cert_paths: list[str] = []
    missing: list[str] = []

    for rcpt in recipients:
        path = smime_store.get_recipient_cert_path(rcpt)
        if path:
            cert_paths.append(str(path))
        else:
            missing.append(rcpt)

    if missing:
        return None, missing

    try:
        with tempfile.NamedTemporaryFile(suffix=".eml", delete=False) as tmp:
            tmp.write(message_bytes)
            tmp_path = Path(tmp.name)

        result = subprocess.run(
            ["openssl", "smime", "-encrypt", "-aes256", "-in", str(tmp_path)] + cert_paths,
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
        log.info("S/MIME encrypted for %d recipient(s)", len(recipients))
        # OpenSSL outputs the legacy "x-pkcs7-mime" MIME type; normalize to the
        # RFC 3851/5751 name so Outlook Classic recognises it as S/MIME.
        out = result.stdout.replace(b"application/x-pkcs7-mime",
                                    b"application/pkcs7-mime")
        return out, []

    except Exception as exc:
        log.error("S/MIME encryption error: %s", exc)
        return None, []
