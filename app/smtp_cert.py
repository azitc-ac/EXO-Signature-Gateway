"""Separates TLS-Zertifikat NUR für den SMTP-Listener (Port 25/587).

Standardmäßig teilen sich SMTP-Listener und Web-UI EIN Zertifikat
(`config.SMTP_TLS_CERT`, von der Let's-Encrypt-Automatik auf den Gateway-Namen
verwaltet). Manche Aufbauten verlangen aber, dass der Listener einen ANDEREN
Namen präsentiert als die Web-UI — etwa hinter einem SNI-Reverse-Proxy, wo ein
EXO-Connector per TLS einen Hostnamen validiert, der nicht der Gateway-Name ist
(Hybrid-Koexistenz: EXO prüft `TlsDomain=*.example.net`, die Web-UI läuft aber
weiter auf `sig.example.org`). Dann wird hier ein eigenes Zert hinterlegt; die
Web-UI bleibt unverändert auf dem gemeinsamen Zert.

Wirkt nach einem Neustart — der Listener lädt sein Zert beim Start
(`main._build_tls_context`).
"""
import logging
from pathlib import Path

import config

log = logging.getLogger(__name__)

_DIR = Path(config.DATA_DIR) / "smtp_tls"
CERT = _DIR / "cert.pem"
KEY = _DIR / "key.pem"


def aktiv() -> bool:
    """True, wenn ein separates Listener-Zert hinterlegt ist."""
    return CERT.exists() and KEY.exists()


def pfade() -> tuple[str, str] | None:
    """(cert, key) des separaten Listener-Zerts — oder None (dann gemeinsames Zert)."""
    return (str(CERT), str(KEY)) if aktiv() else None


def _leaf(cert_pem: bytes):
    from cryptography import x509
    return x509.load_pem_x509_certificate(cert_pem)


def _passt(cert_pem: bytes, key_pem: bytes) -> bool:
    """Gehört der private Schlüssel zu diesem Zertifikat? (Vergleich der
    öffentlichen Schlüssel — deckt RSA/EC/… ab)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    pub_c = _leaf(cert_pem).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    pub_k = load_pem_private_key(key_pem, password=None).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return pub_c == pub_k


def _pem_blocks(pem: bytes) -> list[bytes]:
    """Zerlegt eine PEM-Sammlung in einzelne Zertifikatsblöcke (byte-genau)."""
    begin, end = b"-----BEGIN CERTIFICATE-----", b"-----END CERTIFICATE-----"
    out, i = [], 0
    while True:
        b = pem.find(begin, i)
        if b < 0:
            break
        e = pem.find(end, b)
        if e < 0:
            break
        out.append(pem[b:e + len(end)] + b"\n")
        i = e + len(end)
    return out


def _load_cert_any(raw: bytes):
    from cryptography import x509
    try:
        return x509.load_der_x509_certificate(raw)
    except Exception:                                     # noqa: BLE001
        return x509.load_pem_x509_certificate(raw)


def _aia_ca_issuer(cert) -> str | None:
    """CA-Issuers-URL aus der AIA-Erweiterung — oder None."""
    from cryptography import x509
    from cryptography.x509.oid import ExtensionOID, AuthorityInformationAccessOID
    try:
        aia = cert.extensions.get_extension_for_oid(
            ExtensionOID.AUTHORITY_INFORMATION_ACCESS).value
    except x509.ExtensionNotFound:
        return None
    for desc in aia:
        if desc.access_method == AuthorityInformationAccessOID.CA_ISSUERS:
            try:
                return desc.access_location.value
            except Exception:                             # noqa: BLE001
                return None
    return None


def _hole_zert(url: str):
    """Lädt ein Zertifikat von einer AIA-URL (DER oder PEM). Getrennt, damit
    Tests den Netzzugriff ersetzen können."""
    import urllib.request
    with urllib.request.urlopen(url, timeout=10) as r:    # noqa: S310 — feste AIA-URLs
        return _load_cert_any(r.read())


def _fp(cert) -> bytes:
    from cryptography.hazmat.primitives import hashes
    return cert.fingerprint(hashes.SHA256())


def _mit_kette(cert_pem: bytes) -> bytes:
    """Ergänzt fehlende Zwischenzertifikate, indem der AIA-Kette (CA Issuers)
    vom Leaf aufwärts gefolgt wird, bis ein selbstsigniertes (Root) erreicht ist.

    Der Root wird NICHT mitgesendet (den hat der Client im Trust-Store — „Root =
    Client, Intermediates = Server"). Nötig, weil strenge Gegenstellen wie
    Exchange Online kein AIA-Chasing machen und die volle Kette im Handshake
    erwarten. Netz-/AIA-Fehler sind nicht fatal: dann bleibt die Kette so, wie
    sie kam (mit Warnung).
    """
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding
    blocks = _pem_blocks(cert_pem)
    if not blocks:
        return cert_pem
    seen = {_fp(x509.load_pem_x509_certificate(b)) for b in blocks}
    for _ in range(8):                                    # Schleifen-Deckel
        last = x509.load_pem_x509_certificate(blocks[-1])
        if last.issuer == last.subject:
            break                                          # schon beim Root
        url = _aia_ca_issuer(last)
        if not url:
            break
        try:
            inter = _hole_zert(url)
        except Exception as exc:                          # noqa: BLE001
            log.warning("AIA-Kettenaufbau: %s nicht ladbar (%s) — Kette bleibt, "
                        "wie sie kam", url, exc)
            break
        if inter.issuer == inter.subject:
            break                                          # Root — nicht mitsenden
        fp = _fp(inter)
        if fp in seen:
            break                                          # Zyklus-Schutz
        seen.add(fp)
        blocks.append(inter.public_bytes(Encoding.PEM))
    return b"".join(blocks)


def speichern(cert_pem: bytes, key_pem: bytes) -> None:
    """Separates Listener-Zert setzen.

    Validiert Zert UND Schlüssel und dass sie ZUSAMMENGEHÖREN, bevor gespeichert
    wird — ein kaputtes Paar liesse den Listener sonst beim nächsten Start ganz
    ohne TLS hochkommen (`_build_tls_context` gibt dann None zurück). Der
    Schlüssel wird 600 geschrieben (`secure_io`), der Ordner 700.
    """
    import secure_io
    cert_pem = (cert_pem or b"").strip() + b"\n"
    key_pem = (key_pem or b"").strip() + b"\n"
    try:
        _leaf(cert_pem)
    except Exception as exc:                              # noqa: BLE001
        raise ValueError(f"Zertifikat nicht lesbar (PEM erwartet): {exc}")
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        load_pem_private_key(key_pem, password=None)
    except Exception as exc:                              # noqa: BLE001
        raise ValueError(
            f"Privater Schlüssel nicht lesbar (unverschlüsseltes PEM erwartet): {exc}")
    if not _passt(cert_pem, key_pem):
        raise ValueError("Schlüssel und Zertifikat gehören nicht zusammen")
    cert_pem = _mit_kette(cert_pem)          # fehlende Intermediates per AIA ergänzen
    secure_io.write_secret_bytes(CERT, cert_pem)
    secure_io.write_secret_bytes(KEY, key_pem)
    log.info("Separates SMTP-Listener-Zert gesetzt: %s (%d Zert(e) in der Kette)",
             _info_dict(cert_pem).get("subject"), len(_pem_blocks(cert_pem)))


def speichern_pfx(pfx_bytes: bytes, password: str | None = None) -> None:
    """PFX/PKCS#12 (Zert + privater Schlüssel, optional Kette) entpacken und als
    separates Listener-Zert setzen. Passwort optional (leer/None = ohne).

    Entpackt zu PEM und geht durch `speichern()` — damit greift dieselbe
    Validierung (Zert+Schlüssel gehören zusammen) und Ablage (Key 600).
    """
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PrivateFormat, NoEncryption, pkcs12)
    pw = (password or "").encode() or None
    try:
        key, cert, chain = pkcs12.load_key_and_certificates(pfx_bytes, pw)
    except Exception as exc:                              # noqa: BLE001
        raise ValueError(
            f"PFX nicht lesbar — falsches Passwort oder kein PKCS#12? ({exc})")
    if key is None or cert is None:
        raise ValueError("PFX enthält kein Zertifikat mit privatem Schlüssel")
    cert_pem = cert.public_bytes(Encoding.PEM)
    for c in (chain or []):
        cert_pem += c.public_bytes(Encoding.PEM)         # Kette anhängen
    key_pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    speichern(cert_pem, key_pem)


def entfernen() -> bool:
    """Separates Zert löschen → Listener nutzt wieder das gemeinsame Zert."""
    weg = False
    for p in (CERT, KEY):
        if p.exists():
            p.unlink()
            weg = True
    if weg:
        log.info("Separates SMTP-Listener-Zert entfernt — Rückfall aufs gemeinsame Zert")
    return weg


def _info_dict(cert_pem: bytes) -> dict:
    from cryptography import x509
    cert = _leaf(cert_pem)
    try:
        san = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        san = []
    # not_valid_after_utc gibt es erst ab cryptography 42; älter → not_valid_after.
    na = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
    return {"subject": cert.subject.rfc4514_string(), "san": san,
            "not_after": na.isoformat()}


def info() -> dict:
    """Stand des EFFEKTIVEN Listener-Zerts: das separate, falls gesetzt, sonst das
    gemeinsame. `override`=True zeigt an, dass ein separates aktiv ist."""
    override = aktiv()
    quelle = CERT if override else Path(config.SMTP_TLS_CERT)
    d = {"override": override, "vorhanden": quelle.exists(),
         "gemeinsam_pfad": config.SMTP_TLS_CERT}
    if quelle.exists():
        try:
            d.update(_info_dict(quelle.read_bytes()))
        except Exception as exc:                          # noqa: BLE001
            d["fehler"] = str(exc)
    return d
