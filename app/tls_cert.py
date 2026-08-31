"""TLS-Zertifikat des Listeners bereitstellen — Import aus PFX/PKCS#12.

Der Web-/SMTP-Listener bedient TLS, sobald `cert.pem` existiert (siehe main.py).
Neben Let's Encrypt (HTTP-01, Port 80) soll ein bestehendes Zertifikat
importierbar sein — für Betreiber, die Port 80 nicht öffnen wollen, aber schon
ein passendes Zertifikat haben (auch Wildcard oder interne CA).

Geschrieben werden `cert.pem` (Leaf + Kette) und `key.pem` (Rechte 600), atomar.
Ein importiertes Zertifikat wird gegen den konfigurierten Hostnamen geprüft —
ein Zertifikat, das nicht zum Namen passt, würde beim TLS-Handshake ohnehin
abgelehnt und ist fast immer ein Versehen.
"""
from __future__ import annotations

from pathlib import Path

import config


def cert_hostnames(cert) -> list[str]:
    """Alle DNS-Namen eines Zertifikats (SAN dNSName + Common Name), klein."""
    from cryptography import x509
    from cryptography.x509.oid import ExtensionOID, NameOID

    namen: list[str] = []
    try:
        san = cert.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
        namen += san.value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        pass
    try:
        cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if cn:
            namen.append(cn[0].value)
    except Exception:                                       # noqa: BLE001
        pass

    out: list[str] = []
    for n in namen:
        n = (n or "").strip().lower()
        if n and n not in out:
            out.append(n)
    return out


def host_matches(host: str, namen: list[str]) -> bool:
    """Passt der Hostname zu einem der Zertifikatsnamen? Wildcard genau eine Ebene."""
    host = (host or "").strip().lower()
    if not host:
        return False
    for n in namen:
        if n == host:
            return True
        if n.startswith("*.") and "." in host and host.split(".", 1)[1] == n[2:]:
            return True
    return False


def _write_atomar(pfad: Path, daten: bytes, rechte: int) -> None:
    pfad.parent.mkdir(parents=True, exist_ok=True)
    tmp = pfad.with_suffix(pfad.suffix + ".tmp")
    tmp.write_bytes(daten)
    tmp.chmod(rechte)                    # Rechte VOR replace — rename erbt sie
    tmp.replace(pfad)


def install_pfx(pfx_bytes: bytes, password: str, expected_host: str = "",
                allow_mismatch: bool = False) -> dict:
    """PFX entpacken, gegen `expected_host` prüfen und als cert.pem/key.pem ablegen.

    Der Hostname-Abgleich (`host_matches`) folgt RFC 6125: ein Wildcard `*.x`
    deckt genau EINE Ebene ab (`a.x`, nicht `a.b.x`) — dieselbe Regel, nach der
    ein Browser das Zertifikat später akzeptiert oder ablehnt. Passt es nicht,
    wird der Import abgelehnt, ES SEI DENN `allow_mismatch` — dann importiert der
    Betreiber bewusst (z.B. Zugriff hinter einem Proxy oder per IP) und die
    Nichtübereinstimmung kommt als `warnung` zurück statt als Fehler.

    Wirft `ValueError` bei ungültigem PFX, fehlendem Schlüssel oder (ohne
    Übergehen) Hostname-Nichtübereinstimmung. Gibt `{hostnames, not_after,
    warnung}` zurück.
    """
    from cryptography.hazmat.primitives.serialization import (
        pkcs12, Encoding, PrivateFormat, NoEncryption)

    pw = password.encode() if password else None
    try:
        key, cert, chain = pkcs12.load_key_and_certificates(pfx_bytes, pw)
    except Exception as exc:                                # noqa: BLE001
        raise ValueError(f"PFX nicht lesbar (falsches Passwort?): {exc}") from exc
    if key is None or cert is None:
        raise ValueError("PFX enthält kein Zertifikat mit privatem Schlüssel.")

    namen = cert_hostnames(cert)
    warnung = ""
    if expected_host and not host_matches(expected_host, namen):
        meldung = (f"Zertifikat passt nicht zum Hostnamen {expected_host!r} "
                   f"(enthält: {', '.join(namen) or 'keine DNS-Namen'}).")
        if not allow_mismatch:
            raise ValueError(meldung + " Ein Browser würde es hier ebenfalls "
                             "ablehnen. Ist der Import trotzdem gewollt, die "
                             "Option 'Prüfung übergehen' aktivieren.")
        warnung = meldung

    cert_pem = cert.public_bytes(Encoding.PEM) + b"".join(
        c.public_bytes(Encoding.PEM) for c in (chain or []))
    key_pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())

    _write_atomar(Path(config.SMTP_TLS_CERT), cert_pem, 0o644)
    _write_atomar(Path(config.SMTP_TLS_KEY), key_pem, 0o600)

    return {"hostnames": namen, "warnung": warnung,
            "not_after": cert.not_valid_after_utc.isoformat()}
