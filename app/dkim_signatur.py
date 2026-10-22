"""DKIM-Signatur für den Direktversand — im Gateway, nicht im Postfix.

Warum im Gateway: EIN Ort für Schlüssel (`data/dkim/`, Rechte 600), dieselbe
Verwaltung wie für alle anderen Geheimnisse, und das Signieren ist in Python
testbar. Ein Milter im Postfix-Container wäre eine zweite Konfiguration und ein
zweiter Schlüsselort.

Wann signiert wird: NUR beim Direktversand (`reinject._relay_to_target` mit Ziel
`direkt`). Auf dem Weg über Exchange Online signiert Exchange selbst.

Mit welcher Domäne: mit der Domäne des `From:`-Kopfs, wenn für sie ein Schlüssel
hinterlegt ist. Das ist die Domäne, auf die DMARC die Signatur ausrichtet; eine
Signatur auf die Umschlag-Absenderdomäne hülfe DMARC nicht, wenn sie abweicht.
Ohne Schlüssel geht die Mail unsigniert hinaus — mit Warnung im Protokoll.

⚠️ Verwaltung (`schluessel_erzeugen`, `entfernen`) schreibt über
`settings_store.update()` — NUR aus dem Web-Prozess aufrufen.
"""
from __future__ import annotations

import base64
import email
import email.utils
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import config
import secure_io
import settings_store

log = logging.getLogger(__name__)

_DOMAENE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_SELEKTOR = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

# Signierte Kopfzeilen: was ein Empfänger zur Darstellung und Zuordnung nutzt.
# `From` MUSS dabei sein (RFC 6376 §5.4). Fehlende Kopfzeilen sind unschädlich.
_KOPFZEILEN = [b"from", b"to", b"cc", b"subject", b"date", b"message-id",
               b"reply-to", b"in-reply-to", b"references", b"mime-version",
               b"content-type", b"content-transfer-encoding"]


def schluessel_verzeichnis() -> Path:
    return Path(config.DATA_DIR) / "dkim"


def _domaenen() -> dict:
    d = settings_store.get("DKIM_DOMAINS")
    return d if isinstance(d, dict) else {}


def _schluesselpfad(domain: str, selektor: str) -> Path:
    return schluessel_verzeichnis() / f"{domain}.{selektor}.pem"


def _normalisieren(domain: str) -> str:
    domain = (domain or "").strip().lower().lstrip("@").rstrip(".")
    if not _DOMAENE.match(domain):
        raise ValueError("Bitte eine gültige Domäne angeben (z. B. example.com)")
    return domain


def _oeffentlich_b64(privat_pem: bytes) -> str:
    from cryptography.hazmat.primitives import serialization
    schluessel = serialization.load_pem_private_key(privat_pem, password=None)
    der = schluessel.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(der).decode("ascii")


def dns_eintrag(domain: str) -> dict | None:
    """Der DNS-Eintrag, den der Betreiber für eine Domäne setzen muss."""
    domain = (domain or "").strip().lower()
    selektor = _domaenen().get(domain)
    if not selektor:
        return None
    try:
        pem = _schluesselpfad(domain, selektor).read_bytes()
    except OSError:
        return None
    return {"domain": domain, "selektor": selektor,
            "name": f"{selektor}._domainkey.{domain}",
            "wert": f"v=DKIM1; k=rsa; p={_oeffentlich_b64(pem)}"}


def liste() -> list[dict]:
    """Alle Domänen mit Schlüssel — für die Oberfläche (ohne privaten Schlüssel)."""
    out = []
    for domain in sorted(_domaenen()):
        eintrag = dns_eintrag(domain)
        out.append(eintrag or {"domain": domain, "selektor": _domaenen()[domain],
                               "name": "", "wert": "", "fehlt": True})
    return out


def schluessel_erzeugen(domain: str, selektor: str = "") -> dict:
    """Neuen RSA-2048-Schlüssel für eine Domäne anlegen (Selektor-Vorgabe
    `gwJJJJMM`).

    Signiert wird ab sofort mit diesem Schlüssel. Hat die Domäne schon einen,
    wird abgelehnt: Ein stiller Wechsel signierte sofort mit einem Schlüssel,
    der noch nicht im DNS steht — jede Mail fiele bis dahin durch DKIM.
    Ein lückenloser Wechsel (zwei Selektoren parallel) ist noch nicht gebaut.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    domain = _normalisieren(domain)
    if domain in _domaenen():
        raise ValueError(f"Für {domain} gibt es schon einen Schlüssel. Zum Wechsel "
                         "erst entfernen — bis der neue Eintrag im DNS steht, "
                         "fällt die Signatur dann durch")
    selektor = (selektor or "").strip().lower() or \
        "gw" + datetime.now(timezone.utc).strftime("%Y%m")
    if not _SELEKTOR.match(selektor):
        raise ValueError("Selektor: a–z, 0–9 und -, höchstens 63 Zeichen")
    schluessel = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = schluessel.private_bytes(serialization.Encoding.PEM,
                                   serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption())
    secure_io.ensure_dir(schluessel_verzeichnis())
    secure_io.write_secret_bytes(_schluesselpfad(domain, selektor), pem)
    d = dict(_domaenen())
    d[domain] = selektor
    settings_store.update({"DKIM_DOMAINS": d})
    return dns_eintrag(domain) or {}


def entfernen(domain: str) -> bool:
    """Domäne aus der Signatur nehmen und ALLE ihre Schlüsseldateien löschen."""
    domain = (domain or "").strip().lower()
    d = dict(_domaenen())
    if domain not in d:
        return False
    del d[domain]
    settings_store.update({"DKIM_DOMAINS": d})
    for p in schluessel_verzeichnis().glob(f"{domain}.*.pem"):
        try:
            p.unlink()
        except OSError as exc:
            log.warning("DKIM-Schlüssel %s ließ sich nicht löschen: %s", p.name, exc)
    return True


def _from_domain(content: bytes) -> str:
    msg = email.message_from_bytes(content)
    adr = email.utils.parseaddr(msg.get("From", ""))[1].strip().lower()
    return adr.rsplit("@", 1)[-1] if "@" in adr else ""


def signieren(content: bytes) -> tuple[bytes, str]:
    """Mail signieren, wenn für die From-Domäne ein Schlüssel da ist.

    Rückgabe (Inhalt, Ergebnis) mit Ergebnis `signiert:<domain>`, `ohne_schluessel`
    oder `fehler`. Scheitert das Signieren, geht die Mail UNSIGNIERT weiter:
    Zustellen ohne Signatur ist besser als gar nicht — sichtbar gemacht wird es
    über das Ergebnis und das Protokoll.
    """
    import dkim
    domain = _from_domain(content)
    selektor = _domaenen().get(domain)
    if not selektor:
        log.warning("Direktversand ohne DKIM: für die Absenderdomäne %r ist kein "
                    "Schlüssel hinterlegt", domain or "(leer)")
        return content, "ohne_schluessel"
    try:
        pem = _schluesselpfad(domain, selektor).read_bytes()
        kopf = dkim.sign(content, selektor.encode(), domain.encode(), pem,
                         canonicalize=(b"relaxed", b"relaxed"),
                         include_headers=_KOPFZEILEN)
    except Exception as exc:                                  # noqa: BLE001
        log.error("DKIM-Signatur für %s fehlgeschlagen — Mail geht unsigniert: %s",
                  domain, exc)
        return content, "fehler"
    return kopf + content, f"signiert:{domain}"
