"""
S/MIME certificate store — per-user cert + private key management.

Storage layout (new):
    /app/data/smime/{email}/certs/{slot_id}/cert.pem
    /app/data/smime/{email}/certs/{slot_id}/key.pem
    /app/data/smime/{email}/default           ← active slot_id

Legacy layout (auto-migrated on first access):
    /app/data/smime/{email}/cert.pem
    /app/data/smime/{email}/key.pem
"""

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, pkcs12, BestAvailableEncryption,
    load_pem_private_key,
)

log = logging.getLogger(__name__)
SMIME_DIR = Path("/app/data/smime")
RECIPIENT_DIR = Path("/app/data/smime/recipients")


def _key_encryption():
    import settings_store, config
    if settings_store.get("SMIME_KEY_ENCRYPT") is False:
        return NoEncryption()
    pw = settings_store.get("SMIME_KEY_PASSWORD") or config.SMIME_KEY_PASSWORD or ""
    if pw:
        return BestAvailableEncryption(pw.encode())
    return NoEncryption()


def reencrypt_all_keys(old_password: str = "") -> list:
    """Re-encrypt all stored private keys with current _key_encryption().

    Called after a key-password change so existing keys stay usable.
    Tries old_password first, then falls back to no password (handles
    previously unencrypted keys). Returns list of paths that failed.
    """
    new_enc = _key_encryption()
    failed = []
    for kp in SMIME_DIR.rglob("key.pem"):
        try:
            raw = kp.read_bytes()
            # Try old password, then no password (unencrypted)
            key = None
            for pw in ([old_password.encode()] if old_password else []) + [None]:
                try:
                    key = load_pem_private_key(raw, password=pw)
                    break
                except Exception:
                    continue
            if key is None:
                raise ValueError("Kein passendes Passwort gefunden")
            kp.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, new_enc))
            log.info("Re-encrypted %s", kp)
        except Exception as exc:
            log.error("Re-encrypt failed for %s: %s", kp, exc)
            failed.append(str(kp))
    return failed


def _get_expiry(cert) -> datetime:
    try:
        return cert.not_valid_after_utc
    except AttributeError:
        return cert.not_valid_after.replace(tzinfo=timezone.utc)


def _friendly_subject(cert: x509.Certificate) -> str:
    from cryptography.x509.oid import NameOID
    for oid in (NameOID.COMMON_NAME, NameOID.EMAIL_ADDRESS):
        attrs = cert.subject.get_attributes_for_oid(oid)
        if attrs:
            return attrs[0].value
    return cert.subject.rfc4514_string()


def _friendly_issuer(cert: x509.Certificate) -> str:
    """CN of the issuing CA — e.g. 'CASTLE Client Certificate Authority', not
    the full DN (which can include an internal node code that looks like a
    meaningless serial to a human reader)."""
    from cryptography.x509.oid import NameOID
    attrs = cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
    if attrs:
        return attrs[0].value
    return cert.issuer.rfc4514_string()


def _cert_info(cert: x509.Certificate, email: str) -> dict:
    now = datetime.now(timezone.utc)
    expiry = _get_expiry(cert)
    days_left = (expiry - now).days
    return {
        "email": email,
        "subject": _friendly_subject(cert),
        "issuer": _friendly_issuer(cert),
        "expiry": expiry.strftime("%d.%m.%Y"),
        "days_left": days_left,
        "expired": days_left < 0,
        "warning": 0 <= days_left < 30,
    }


def _slot_id(cert: x509.Certificate) -> str:
    """Stable 16-char hex ID derived from cert SHA-256 fingerprint."""
    return cert.fingerprint(hashes.SHA256()).hex()[:16]


def _migrate_legacy(email: str) -> None:
    """Move old cert.pem/key.pem in user dir into new certs/{slot}/ structure."""
    user_dir = SMIME_DIR / email.lower().strip()
    cert_path = user_dir / "cert.pem"
    key_path = user_dir / "key.pem"
    if not cert_path.exists():
        return
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        slot = _slot_id(cert)
        slot_dir = user_dir / "certs" / slot
        slot_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cert_path, slot_dir / "cert.pem")
        if key_path.exists():
            shutil.copy2(key_path, slot_dir / "key.pem")
        (user_dir / "default").write_text(slot)
        cert_path.unlink()
        if key_path.exists():
            key_path.unlink()
        log.info("Migrated legacy S/MIME cert for %s → slot %s", email, slot)
    except Exception as exc:
        log.warning("Legacy S/MIME cert migration failed for %s: %s", email, exc)


# ── Multi-cert API ────────────────────────────────────────────────────────────

def list_user_certs(email: str) -> list[dict]:
    """Return all cert slots for a user; each dict includes slot_id and is_default."""
    email = email.lower().strip()
    user_dir = SMIME_DIR / email
    if not user_dir.exists():
        return []
    if (user_dir / "cert.pem").exists() and not (user_dir / "certs").exists():
        _migrate_legacy(email)
    certs_dir = user_dir / "certs"
    if not certs_dir.exists():
        return []
    default_file = user_dir / "default"
    default = default_file.read_text().strip() if default_file.exists() else ""
    result = []
    for slot_dir in sorted(certs_dir.iterdir()):
        if not slot_dir.is_dir():
            continue
        cert_path = slot_dir / "cert.pem"
        if not cert_path.exists():
            continue
        try:
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            info = _cert_info(cert, email)
            info["slot_id"] = slot_dir.name
            info["is_default"] = (slot_dir.name == default)
            info["has_local_key"] = (slot_dir / "key.pem").exists()
            info["has_kv_backup"] = (slot_dir / "key.pem.bak").exists()
            result.append(info)
        except Exception as exc:
            result.append({
                "email": email, "slot_id": slot_dir.name, "error": str(exc),
                "subject": "–", "expiry": "–", "days_left": 0,
                "expired": True, "warning": False, "is_default": (slot_dir.name == default),
                "has_local_key": (slot_dir / "key.pem").exists(),
                "has_kv_backup": (slot_dir / "key.pem.bak").exists(),
            })
    # Ensure exactly one default is set
    if result and not any(c.get("is_default") for c in result):
        result[0]["is_default"] = True
        (user_dir / "default").write_text(result[0]["slot_id"])
    return result


def store_p12_slot(email: str, p12_bytes: bytes, password: str = "") -> dict:
    """Import a PKCS12 bundle as a new cert slot. Returns cert info dict with slot_id."""
    email = email.lower().strip()
    pw = password.encode() if password else None
    try:
        private_key, cert, _chain = pkcs12.load_key_and_certificates(p12_bytes, pw)
    except Exception as exc:
        raise ValueError(f"Ungültiges PKCS12 oder falsches Passwort: {exc}") from exc
    if not private_key or not cert:
        raise ValueError("PKCS12 muss privaten Schlüssel und Zertifikat enthalten")
    slot = _slot_id(cert)
    user_dir = SMIME_DIR / email
    if (user_dir / "cert.pem").exists() and not (user_dir / "certs").exists():
        _migrate_legacy(email)
    slot_dir = user_dir / "certs" / slot
    slot_dir.mkdir(parents=True, exist_ok=True)
    (slot_dir / "cert.pem").write_bytes(cert.public_bytes(Encoding.PEM))
    (slot_dir / "key.pem").write_bytes(
        private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, _key_encryption())
    )
    default_file = user_dir / "default"
    if not default_file.exists():
        default_file.write_text(slot)
    log.info("S/MIME cert stored for %s — slot %s — %s", email, slot, cert.subject.rfc4514_string())
    info = _cert_info(cert, email)
    info["slot_id"] = slot
    info["is_default"] = (default_file.read_text().strip() == slot)
    return info


def set_default_slot(email: str, slot_id: str) -> None:
    """Set the default signing cert slot for a user."""
    email = email.lower().strip()
    slot_dir = SMIME_DIR / email / "certs" / slot_id
    if not (slot_dir / "cert.pem").exists():
        raise ValueError(f"Slot {slot_id} nicht gefunden für {email}")
    (SMIME_DIR / email / "default").write_text(slot_id)
    log.info("Default S/MIME cert → slot %s for %s", slot_id, email)


def delete_cert_slot(email: str, slot_id: str) -> None:
    """Delete a single cert slot; picks a new default if needed."""
    email = email.lower().strip()
    user_dir = SMIME_DIR / email
    slot_dir = user_dir / "certs" / slot_id
    for name in ("cert.pem", "key.pem"):
        p = slot_dir / name
        if p.exists():
            p.unlink()
    if slot_dir.exists() and not any(slot_dir.iterdir()):
        slot_dir.rmdir()
    default_file = user_dir / "default"
    if default_file.exists() and default_file.read_text().strip() == slot_id:
        default_file.unlink()
        remaining = list_user_certs(email)
        if remaining:
            default_file.write_text(remaining[0]["slot_id"])
    log.info("S/MIME cert slot %s deleted for %s", slot_id, email)


def get_signing_cert_path(email: str) -> Path | None:
    """Return cert.pem path for the default slot (for details/export use)."""
    email_low = email.lower().strip()
    user_dir = SMIME_DIR / email_low
    certs_dir = user_dir / "certs"
    if certs_dir.exists():
        default_file = user_dir / "default"
        default = default_file.read_text().strip() if default_file.exists() else ""
        if default and (certs_dir / default / "cert.pem").exists():
            return certs_dir / default / "cert.pem"
        for slot_dir in sorted(certs_dir.iterdir()):
            p = slot_dir / "cert.pem"
            if p.exists():
                return p
    p = user_dir / "cert.pem"
    return p if p.exists() else None


def get_signing_cert_path_for_slot(email: str, slot_id: str) -> Path | None:
    p = SMIME_DIR / email.lower().strip() / "certs" / slot_id / "cert.pem"
    return p if p.exists() else None


# ── Signing paths (used by handler + signer) ──────────────────────────────────

def get_signing_paths(email: str, allow_backup: bool = False) -> tuple[Path, Path] | None:
    """Return (cert_path, key_path) for the default cert slot, or None.
    If allow_backup=True, also considers key.pem.bak when no key.pem is present."""
    email_low = email.lower().strip()
    user_dir = SMIME_DIR / email_low
    certs_dir = user_dir / "certs"
    if certs_dir.exists():
        default_file = user_dir / "default"
        default = default_file.read_text().strip() if default_file.exists() else ""
        if default:
            slot_dir = certs_dir / default
            c, k = slot_dir / "cert.pem", slot_dir / "key.pem"
            if c.exists() and k.exists():
                return c, k
            if allow_backup and c.exists() and (slot_dir / "key.pem.bak").exists():
                return c, slot_dir / "key.pem.bak"
        first_bak: tuple[Path, Path] | None = None
        for slot_dir in sorted(certs_dir.iterdir()):
            if not slot_dir.is_dir():
                continue
            c, k = slot_dir / "cert.pem", slot_dir / "key.pem"
            if c.exists() and k.exists():
                return c, k
            if allow_backup and first_bak is None and c.exists():
                bak = slot_dir / "key.pem.bak"
                if bak.exists():
                    first_bak = (c, bak)
        if first_bak:
            return first_bak
    # Legacy fallback
    c, k = user_dir / "cert.pem", user_dir / "key.pem"
    if c.exists() and k.exists():
        return c, k
    if allow_backup:
        bak = user_dir / "key.pem.bak"
        if c.exists() and bak.exists():
            return c, bak
    return None


# ── Aggregate list (dashboard / notifications) ────────────────────────────────

def list_certs() -> list[dict]:
    """Return default cert info for all users that have at least one cert."""
    result = []
    if not SMIME_DIR.exists():
        return result
    for user_dir in sorted(SMIME_DIR.iterdir()):
        if not user_dir.is_dir():
            continue
        email = user_dir.name
        if email == "recipients":
            continue
        certs = list_user_certs(email)
        if certs:
            default = next((c for c in certs if c.get("is_default")), certs[0])
            result.append(default)
        elif (user_dir / "cert.pem").exists():
            try:
                cert = x509.load_pem_x509_certificate((user_dir / "cert.pem").read_bytes())
                result.append(_cert_info(cert, email))
            except Exception as exc:
                result.append({"email": email, "error": str(exc),
                               "subject": "–", "expiry": "–", "days_left": 0,
                               "expired": True, "warning": False})
    return result


def store_pem_slot(email: str, cert_chain_pem: bytes, key_pem: bytes) -> dict:
    """
    Import a (PEM cert chain, PEM private key) pair as a new slot.
    The leaf cert (first in chain) is stored. Used by ACME auto-renewal.
    The new cert always becomes the default for this user.
    """
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    # Extract leaf cert (first PEM block)
    pem_str = cert_chain_pem.decode(errors="replace")
    begin = pem_str.find("-----BEGIN CERTIFICATE-----")
    end = pem_str.find("-----END CERTIFICATE-----")
    if begin == -1 or end == -1:
        raise ValueError("Kein Zertifikat in der PEM-Kette gefunden")
    leaf_pem = pem_str[begin : end + len("-----END CERTIFICATE-----")].encode()

    cert = x509.load_pem_x509_certificate(leaf_pem)
    import config as _cfg
    pw = _cfg.SMIME_KEY_PASSWORD
    private_key = load_pem_private_key(key_pem, password=pw.encode() if pw else None)

    email = email.lower().strip()
    slot = _slot_id(cert)
    user_dir = SMIME_DIR / email
    if (user_dir / "cert.pem").exists() and not (user_dir / "certs").exists():
        _migrate_legacy(email)
    slot_dir = user_dir / "certs" / slot
    slot_dir.mkdir(parents=True, exist_ok=True)
    (slot_dir / "cert.pem").write_bytes(leaf_pem)
    (slot_dir / "key.pem").write_bytes(
        private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, _key_encryption())
    )
    # ACME renewals always become the new default
    (user_dir / "default").write_text(slot)
    log.info("S/MIME cert (ACME) stored for %s — slot %s", email, slot)
    info = _cert_info(cert, email)
    info["slot_id"] = slot
    info["is_default"] = True
    return info


# ── Compat wrappers (used by config export/import) ────────────────────────────

def store_p12(email: str, p12_bytes: bytes, password: str = "") -> dict:
    return store_p12_slot(email, p12_bytes, password)


def delete_cert(email: str) -> None:
    """Delete ALL cert slots for a user."""
    email_low = email.lower().strip()
    user_dir = SMIME_DIR / email_low
    certs_dir = user_dir / "certs"
    if certs_dir.exists():
        shutil.rmtree(certs_dir)
    default_file = user_dir / "default"
    if default_file.exists():
        default_file.unlink()
    for name in ("cert.pem", "key.pem"):
        p = user_dir / name
        if p.exists():
            p.unlink()
    if user_dir.exists() and not any(user_dir.iterdir()):
        user_dir.rmdir()
    log.info("All S/MIME certs deleted for %s", email_low)


# ── Recipient public key store (encryption) ───────────────────────────────────

def store_recipient_cert(email: str, cert_pem: bytes) -> dict:
    from cryptography import x509 as _x509
    cert = _x509.load_pem_x509_certificate(cert_pem)
    user_dir = RECIPIENT_DIR / email.lower().strip()
    user_dir.mkdir(parents=True, exist_ok=True)
    existing_path = user_dir / "cert.pem"
    if existing_path.exists():
        try:
            existing = _x509.load_pem_x509_certificate(existing_path.read_bytes())
            if existing.fingerprint(hashes.SHA256()) == cert.fingerprint(hashes.SHA256()):
                log.debug("Recipient S/MIME cert unchanged for %s — skipping", email)
                return _cert_info(cert, email.lower().strip())
        except Exception:
            pass  # korrupte gespeicherte Cert → überschreiben
    (user_dir / "cert.pem").write_bytes(cert_pem)
    import stats
    stats.increment("certs_harvested")
    log.info("Recipient S/MIME cert stored for %s (new/updated)", email)
    return _cert_info(cert, email.lower().strip())


def get_recipient_cert_path(email: str) -> Path | None:
    p = RECIPIENT_DIR / email.lower().strip() / "cert.pem"
    return p if p.exists() else None


def list_recipient_certs() -> list[dict]:
    result = []
    if not RECIPIENT_DIR.exists():
        return result
    for user_dir in sorted(RECIPIENT_DIR.iterdir()):
        cert_path = user_dir / "cert.pem"
        if not cert_path.exists():
            continue
        try:
            from cryptography import x509 as _x509
            cert = _x509.load_pem_x509_certificate(cert_path.read_bytes())
            result.append(_cert_info(cert, user_dir.name))
        except Exception as exc:
            result.append({"email": user_dir.name, "error": str(exc),
                           "subject": "–", "expiry": "–", "days_left": 0,
                           "expired": True, "warning": False})
    return result


def delete_recipient_cert(email: str) -> None:
    p = RECIPIENT_DIR / email.lower().strip() / "cert.pem"
    if p.exists():
        p.unlink()
    d = p.parent
    if d.exists() and not any(d.iterdir()):
        d.rmdir()
    log.info("Recipient S/MIME cert deleted for %s", email)


async def migrate_key_to_keyvault(email: str, slot_id: str | None = None) -> dict:
    """
    Import the local private key for email into Azure Key Vault,
    then remove the local key file (cert is kept for signing operations).
    If slot_id is given, migrate that specific cert slot; otherwise use the active signing slot.
    Returns {"ok": True, "key_id": "...", "slot_id": "..."} or {"ok": False, "error": "..."}.
    """
    import keyvault as _kv

    if not _kv.is_configured():
        return {"ok": False, "error": "Azure Key Vault ist nicht konfiguriert (KEYVAULT_URL fehlt)"}

    if slot_id:
        email_low = email.lower().strip()
        slot_dir = SMIME_DIR / email_low / "certs" / slot_id
        cert_path = slot_dir / "cert.pem"
        key_path = slot_dir / "key.pem"
        if not cert_path.exists():
            return {"ok": False, "error": f"Zertifikat-Slot '{slot_id}' nicht gefunden"}
        if not key_path.exists():
            return {"ok": False, "error": f"Kein lokaler Schlüssel in Slot '{slot_id}' — bereits migriert?"}
    else:
        paths = get_signing_paths(email)
        if not paths:
            return {"ok": False, "error": "Kein lokaler Schlüssel gefunden"}
        cert_path, key_path = paths
        slot_id = key_path.parent.name
    try:
        key_pem = key_path.read_bytes()
    except Exception as exc:
        return {"ok": False, "error": f"Schlüsseldatei konnte nicht gelesen werden: {exc}"}

    try:
        key_id = await _kv.import_rsa_key(email, key_pem)
    except Exception as exc:
        return {"ok": False, "error": f"Key Vault Import fehlgeschlagen: {exc}"}

    import settings_store as _ss
    fallback_mode = _ss.get("KV_KEY_MODE") != "strict"
    if fallback_mode:
        bak_path = key_path.parent / "key.pem.bak"
        try:
            bak_path.write_bytes(key_pem)
            bak_path.chmod(0o600)
            log.info("Backup key saved to %s before KV migration", bak_path)
        except Exception as exc:
            log.warning("Could not save backup key for %s slot %s: %s", email, slot_id, exc)

    try:
        key_path.unlink()
        log.info("Local key removed after KV migration for %s slot %s", email, slot_id)
    except Exception as exc:
        log.warning("Could not remove local key for %s slot %s after KV migration: %s", email, slot_id, exc)
        # Migration succeeded — don't fail over cleanup issue
    return {"ok": True, "key_id": key_id, "slot_id": slot_id, "backup_saved": fallback_mode}


def migrate_keys_encryption() -> int:
    """Re-encrypt existing unencrypted key.pem files with SMIME_KEY_PASSWORD."""
    import config
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    pw = config.SMIME_KEY_PASSWORD
    if not pw:
        return 0
    count = 0
    if not SMIME_DIR.exists():
        return 0
    for user_dir in SMIME_DIR.iterdir():
        if not user_dir.is_dir():
            continue
        # Check both legacy and new slot locations
        key_paths = [user_dir / "key.pem"]
        certs_dir = user_dir / "certs"
        if certs_dir.exists():
            key_paths += [sd / "key.pem" for sd in certs_dir.iterdir() if sd.is_dir()]
        for key_path in key_paths:
            if not key_path.exists():
                continue
            try:
                key_bytes = key_path.read_bytes()
                try:
                    private_key = load_pem_private_key(key_bytes, password=None)
                except (ValueError, TypeError):
                    continue
                key_path.write_bytes(
                    private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8,
                                              BestAvailableEncryption(pw.encode()))
                )
                log.info("Migrated key to encrypted storage: %s", key_path)
                count += 1
            except Exception as exc:
                log.warning("Failed to migrate key %s: %s", key_path, exc)
    return count
