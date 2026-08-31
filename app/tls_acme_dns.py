"""Let's Encrypt DNS-01 (manuell) für das TLS-Zertifikat.

Für Betreiber ohne offenen Port 80 und ohne vorhandenes Zertifikat: Das Gateway
bestellt bei Let's Encrypt, zeigt den zu setzenden TXT-Record, und stellt nach
dessen Eintrag das Zertifikat aus — ganz ohne eingehenden Port.

Zweistufig (der Nutzer muss dazwischen den DNS-Record setzen):
  start()  → Order anlegen, TXT-Record berechnen, Zustand ablegen
  finish() → Challenge auslösen, validieren, finalisieren, Zertifikat holen

⚠️ MANUELL heißt: die ERNEUERUNG (~alle 90 Tage) muss wiederholt werden — einen
automatischen DNS-API-Weg gibt es hier (noch) nicht. Die Oberfläche weist darauf
hin. Der Zwischenzustand (inkl. zweier privater Schlüssel) liegt mit Rechten 600
unter DATA_DIR/tls_dns01_pending.json.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import config

_DIR_PROD = "https://acme-v02.api.letsencrypt.org/directory"
_DIR_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"
_PENDING = Path(config.DATA_DIR) / "tls_dns01_pending.json"


def _dir(staging: bool) -> str:
    return _DIR_STAGING if staging else _DIR_PROD


def _new_ec():
    from cryptography.hazmat.primitives.asymmetric import ec
    return ec.generate_private_key(ec.SECP256R1())


def _key_to_pem(key) -> str:
    from cryptography.hazmat.primitives import serialization
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()


def _key_from_pem(pem: str):
    from cryptography.hazmat.primitives import serialization
    return serialization.load_pem_private_key(pem.encode(), password=None)


async def start(domain: str, email: str, staging: bool = False) -> dict:
    """Order anlegen und den zu setzenden TXT-Record zurückgeben.

    Rückgabe: {record_name, record_value}. Wirft ValueError bei fehlender Domain.
    """
    import acme_client
    import tls_cert
    domain = (domain or "").strip().lower()
    if not domain:
        raise ValueError("Kein Hostname angegeben.")

    account_key = _new_ec()
    client = acme_client.AcmeClient(_dir(staging), account_key)
    await client.ensure_account(email or "")
    order = await client.new_order_dns(domain)
    authz = await client.get_authorization(order["authorizations"][0])
    try:
        ch = next(c for c in authz["challenges"] if c["type"] == "dns-01")
    except StopIteration:
        raise ValueError("Let's Encrypt bietet keine DNS-01-Challenge an.")

    thumb = acme_client.jwk_thumbprint(account_key)
    key_authz = f"{ch['token']}.{thumb}"
    record_value = acme_client.b64url(hashlib.sha256(key_authz.encode()).digest())

    domain_key = _new_ec()
    pending = {
        "domain": domain,
        "staging": staging,
        "account_url": client.account_url,
        "order_url": order["order_url"],
        "finalize": order["finalize"],
        "challenge_url": ch["url"],
        "account_key_pem": _key_to_pem(account_key),
        "domain_key_pem": _key_to_pem(domain_key),
    }
    tls_cert._write_atomar(_PENDING, json.dumps(pending).encode(), 0o600)
    return {"record_name": f"_acme-challenge.{domain}", "record_value": record_value}


def pending() -> dict | None:
    try:
        return json.loads(_PENDING.read_text(encoding="utf-8"))
    except Exception:                                        # noqa: BLE001
        return None


async def finish() -> dict:
    """Challenge auslösen, validieren, Zertifikat holen und installieren.

    Rückgabe: {hostnames, not_after}. Wirft ValueError mit klarer Meldung, wenn
    die Validierung scheitert (TXT falsch/nicht propagiert)."""
    import acme_client
    import tls_cert
    p = pending()
    if not p:
        raise ValueError("Keine offene DNS-01-Bestellung — bitte zuerst starten.")

    account_key = _key_from_pem(p["account_key_pem"])
    client = acme_client.AcmeClient(_dir(p["staging"]), account_key, p["account_url"])
    await client.trigger_challenge(p["challenge_url"])
    order = await client.poll_order_status(p["order_url"])
    if order.get("status") == "invalid":
        raise ValueError("Let's Encrypt konnte den TXT-Record nicht bestätigen — "
                         "Record korrekt gesetzt und bereits propagiert?")

    domain_key = _key_from_pem(p["domain_key_pem"])
    csr_der = _build_csr(domain_key, p["domain"])
    await client.finalize(p["finalize"], csr_der)
    order = await client.poll_order_status(p["order_url"])
    if order.get("status") != "valid":
        raise ValueError(f"Bestellung nicht abgeschlossen (Status {order.get('status')}).")

    cert_pem = await client.download_certificate(order["certificate"])
    key_pem = _key_to_pem(domain_key).encode()
    tls_cert._write_atomar(Path(config.SMTP_TLS_CERT), cert_pem, 0o644)
    tls_cert._write_atomar(Path(config.SMTP_TLS_KEY), key_pem, 0o600)
    try:
        _PENDING.unlink()
    except OSError:
        pass

    not_after = ""
    try:
        from cryptography import x509
        not_after = x509.load_pem_x509_certificate(cert_pem).not_valid_after_utc.isoformat()
    except Exception:                                        # noqa: BLE001
        pass
    return {"hostnames": [p["domain"]], "not_after": not_after}


def _build_csr(domain_key, domain: str) -> bytes:
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)]))
           .add_extension(x509.SubjectAlternativeName([x509.DNSName(domain)]), critical=False)
           .sign(domain_key, hashes.SHA256()))
    return csr.public_bytes(serialization.Encoding.DER)
