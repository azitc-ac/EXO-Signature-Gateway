"""S/MIME decryption — local key (openssl) or Azure Key Vault (RSA1_5 + symmetric)."""
import base64
import email as _email_mod
import email.policy
import logging
import subprocess

import config
import smime_store

log = logging.getLogger(__name__)


def _decrypt_local(message_bytes: bytes, recipient: str) -> bytes | None:
    """Decrypt using a local key file (key.pem or key.pem.bak)."""
    paths = smime_store.get_signing_paths(recipient, allow_backup=True)
    if not paths:
        return None
    cert_path, key_path = paths
    import settings_store as _ss
    password = (config.SMIME_KEY_PASSWORD
                or _ss.get("SMIME_KEY_PASSWORD") or "")
    cmd = [
        "openssl", "smime", "-decrypt",
        "-recip", str(cert_path),
        "-inkey", str(key_path),
        "-passin", f"pass:{password}",   # always explicit — avoids interactive prompt
    ]
    try:
        result = subprocess.run(cmd, input=message_bytes, capture_output=True, timeout=15)
        if result.returncode != 0:
            log.error("openssl smime -decrypt failed for %s: %s",
                      recipient, result.stderr.decode(errors="replace")[:400])
            return None
        log.info("S/MIME decrypted (local key) for recipient=%s", recipient)
        return result.stdout
    except Exception as exc:
        log.error("S/MIME local decrypt error for %s: %s", recipient, exc)
        return None


async def _decrypt_keyvault(message_bytes: bytes, recipient: str) -> bytes | None:
    """
    Decrypt S/MIME EnvelopedData via Azure Key Vault.

    Flow:
      1. Parse the MIME message → extract DER-encoded CMS ContentInfo
      2. Find the KeyTransRecipientInfo matching our cert (by serial number)
      3. Call KV POST /keys/{name}/decrypt with RSA1_5 → get session key
      4. Decrypt the content with AES-CBC or 3DES-CBC using the session key
    """
    import httpx
    from asn1crypto import cms as _cms
    from cryptography import x509 as _x509
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7 as _PKCS7Padding
    import keyvault as _kv

    # ── 1. Extract DER payload from MIME message ─────────────────────────────
    try:
        msg = _email_mod.message_from_bytes(message_bytes, policy=_email_mod.policy.compat32)
        der = msg.get_payload(decode=True)
        if not der:
            raw_payload = msg.get_payload()
            if isinstance(raw_payload, str):
                # Strip PEM armour if present
                lines = [l for l in raw_payload.splitlines()
                         if l and not l.startswith("-----")]
                der = base64.b64decode("".join(lines))
        if not der:
            log.error("KV decrypt: cannot extract PKCS7 payload for %s", recipient)
            return None
    except Exception as exc:
        log.error("KV decrypt: MIME parse failed for %s: %s", recipient, exc)
        return None

    # ── 2. Parse CMS EnvelopedData ───────────────────────────────────────────
    try:
        ci = _cms.ContentInfo.load(der)
        if ci["content_type"].native != "enveloped_data":
            log.error("KV decrypt: not enveloped_data for %s", recipient)
            return None
        env = ci["content"]
    except Exception as exc:
        log.error("KV decrypt: CMS parse failed for %s: %s", recipient, exc)
        return None

    # ── 3. Load our cert to match RecipientInfo ──────────────────────────────
    paths = smime_store.get_signing_paths(recipient, allow_backup=True)
    if not paths:
        log.error("KV decrypt: no cert for %s", recipient)
        return None
    cert_path, _ = paths
    try:
        our_cert = _x509.load_pem_x509_certificate(cert_path.read_bytes())
        our_serial = our_cert.serial_number
    except Exception as exc:
        log.error("KV decrypt: cert load failed for %s: %s", recipient, exc)
        return None

    # ── 4. Find matching KeyTransRecipientInfo ───────────────────────────────
    encrypted_key: bytes | None = None
    for ri in env["recipient_infos"]:
        if ri.name != "ktri":
            continue
        ktri = ri.chosen
        rid = ktri["rid"]
        try:
            if rid.name == "issuer_and_serial_number":
                serial = rid.chosen["serial_number"].native
                if serial == our_serial:
                    encrypted_key = ktri["encrypted_key"].native
                    break
            elif rid.name == "subject_key_identifier":
                # Match by SubjectKeyIdentifier — compare hex
                ski_bytes = bytes(rid.chosen)
                try:
                    ext = our_cert.extensions.get_extension_for_class(
                        _x509.SubjectKeyIdentifier)
                    if ext.value.digest == ski_bytes:
                        encrypted_key = ktri["encrypted_key"].native
                        break
                except Exception:
                    pass
        except Exception:
            continue

    if not encrypted_key:
        log.error("KV decrypt: no matching RecipientInfo (serial=%s) for %s",
                  our_serial, recipient)
        return None

    # ── 5. KV RSA1_5 decrypt → session key ──────────────────────────────────
    try:
        kv_token = await _kv._get_kv_token()
        if not kv_token:
            log.error("KV decrypt: cannot get KV token for %s", recipient)
            return None
        key_name = _kv._email_to_key_name(recipient)
        enc_b64 = base64.urlsafe_b64encode(encrypted_key).rstrip(b"=").decode()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_kv.vault_url()}/keys/{key_name}/decrypt?api-version=7.4",
                headers={"Authorization": f"Bearer {kv_token}",
                         "Content-Type": "application/json"},
                json={"alg": "RSA1_5", "value": enc_b64},
            )
        if resp.status_code == 403 and "KeyOperationForbidden" in resp.text:
            log.warning("KV decrypt: key missing decrypt op for %s — patching key_ops", recipient)
            patched = await _kv.patch_key_ops(recipient)
            if patched:
                async with httpx.AsyncClient(timeout=15) as client2:
                    resp = await client2.post(
                        f"{_kv.vault_url()}/keys/{key_name}/decrypt?api-version=7.4",
                        headers={"Authorization": f"Bearer {kv_token}",
                                 "Content-Type": "application/json"},
                        json={"alg": "RSA1_5", "value": enc_b64},
                    )
        if resp.status_code != 200:
            log.error("KV decrypt: RSA1_5 failed HTTP %s for %s: %s",
                      resp.status_code, recipient, resp.text[:300])
            return None
        result_b64 = resp.json()["value"]
        pad = (4 - len(result_b64) % 4) % 4
        session_key = base64.urlsafe_b64decode(result_b64 + "=" * pad)
    except Exception as exc:
        log.error("KV decrypt: session key decrypt failed for %s: %s", recipient, exc)
        return None

    # ── 6. Symmetric decrypt ─────────────────────────────────────────────────
    try:
        eci = env["encrypted_content_info"]
        algo = eci["content_encryption_algorithm"]
        algo_name = algo["algorithm"].native or ""

        encrypted_content = eci["encrypted_content"].native
        if not encrypted_content:
            log.error("KV decrypt: no encrypted content for %s", recipient)
            return None

        # Extract IV from algorithm parameters (OctetString for CBC modes)
        params = algo["parameters"]
        iv: bytes = bytes(params) if params is not None else b"\x00" * 16

        algo_lower = algo_name.lower()
        if "aes" in algo_lower:
            cipher: Cipher = Cipher(algorithms.AES(session_key), modes.CBC(iv))
            block_bits = 128
        elif "des" in algo_lower:
            cipher = Cipher(algorithms.TripleDES(session_key), modes.CBC(iv))
            block_bits = 64
        else:
            log.error("KV decrypt: unsupported cipher '%s' for %s", algo_name, recipient)
            return None

        decryptor = cipher.decryptor()
        padded = decryptor.update(encrypted_content) + decryptor.finalize()

        unpadder = _PKCS7Padding(block_bits).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()

        log.info("S/MIME decrypted (Key Vault) for recipient=%s", recipient)
        return plaintext

    except Exception as exc:
        log.error("KV decrypt: symmetric decrypt failed for %s: %s", recipient, exc)
        return None


async def decrypt(message_bytes: bytes, recipient: str) -> bytes | None:
    """
    Decrypt an S/MIME enveloped-data message for recipient.

    Priority:
      1. Azure Key Vault (RSA1_5) — primary when KV is configured
      2. Local key file (key.pem or key.pem.bak) — fallback if KV unreachable
    """
    import keyvault as _kv
    if _kv.is_configured():
        result = await _decrypt_keyvault(message_bytes, recipient)
        if result is not None:
            return result
        # KV failed — fall back to local backup if available
        local = _decrypt_local(message_bytes, recipient)
        if local is not None:
            log.warning("S/MIME decrypt: KV failed for %s, used local backup key", recipient)
            return local
        log.warning("S/MIME decrypt: KV failed and no local key for %s", recipient)
        return None

    # No KV — local only
    result = _decrypt_local(message_bytes, recipient)
    if result is None:
        log.warning("S/MIME decrypt: no local key and Key Vault not configured for %s", recipient)
    return result
