"""TLS für ausgehende Verbindungen — ein Weg, ab Werk mit Zertifikatsprüfung.

ANLASS (v1.10.112)
------------------
Der Rückweg an den Exchange-Smarthost, die Weiterleitung an eigene Ziele und
die Unzustellbarkeitsmeldung bauten ihren TLS-Kontext je von Hand, alle drei mit
`CERT_NONE` und ohne Hostnamenprüfung. Die IMAP-Ablage rief `IMAP4_SSL` ohne
`ssl_context` auf; dessen Vorgabe (`ssl._create_stdlib_context`) prüft ebenfalls
nichts — und über diese Verbindung geht ein OAuth-Bearer-Token. Gegen einen
Mittelsmann auf dem Weg schützte TLS damit nicht.

REGEL
-----
* `kontext()` prüft Zertifikat und Hostnamen, ausser der Aufrufer schaltet das
  ausdrücklich ab. Abschalten gibt es nur für eigene Weiterleitungsziele (je
  Ziel einstellbar, Vorgabe: prüfen) und für die Testmail an den eigenen
  Listener auf 127.0.0.1.
* Nirgends sonst ein `CERT_NONE`, kein `starttls()` ohne `context=`, kein
  `IMAP4_SSL` ohne `ssl_context=` — erzwungen von `tests/test_tls_ausgehend.py`.

⚠️ Mit Prüfung muss der Host ein NAME sein, auf den das Zertifikat lautet. Ein
Smarthost oder Ziel als IP-Adresse scheitert dann an der Hostnamenprüfung.
`abnahme._tls_rueckweg` zeigt das an.
"""
from __future__ import annotations

import ipaddress
import logging
import ssl
from pathlib import Path

import config

log = logging.getLogger(__name__)


def kontext(pruefen: bool = True, client_zert: bool = False) -> ssl.SSLContext:
    """Client-Kontext. *client_zert* legt das Gateway-Zertifikat vor (Exchange
    erkennt den Connector daran, `TlsSenderCertificateName`)."""
    ctx = ssl.create_default_context()
    if not pruefen:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if client_zert:
        cert, key = Path(config.SMTP_TLS_CERT), Path(config.SMTP_TLS_KEY)
        if cert.exists() and key.exists():
            try:
                ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
            except Exception as exc:                          # noqa: BLE001
                log.debug("Client-Zertifikat für TLS nicht ladbar: %s", exc)
    return ctx


def ist_ip(host: str) -> bool:
    try:
        ipaddress.ip_address((host or "").strip().strip("[]"))
        return True
    except ValueError:
        return False
