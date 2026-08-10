"""Helfer, die mehr als ein Routenmodul braucht.

ABGRENZUNG ZU `deps.py`
-----------------------
`deps.py` ist das FUNDAMENT: Anmeldung, Vorlagenverzeichnis, Protokollkanal —
Dinge, die jedes Routenmodul braucht und ohne die keines lauffähig ist.

Hier liegt etwas anderes: einzelne Hilfsfunktionen, die zufällig von zwei
Stellen gebraucht werden. Sie sind kein Fundament, und sie in `deps.py` zu
legen würde dessen Zweck verwässern — nach ein paar Umbauten wäre `deps.py`
der Ort, an dem alles landet, was nirgends sonst passt.

WARUM ES DIESE DATEI GIBT
-------------------------
Beim Herauslösen des Add-in-Moduls blieb eine Umkehrung stehen: `app.py`
importierte `_addin_base_url` aus `routen/addin.py`, weil die
Einstellungsseite es braucht. Ringförmig war das nicht, aber die Richtung war
falsch — `app.py` bindet Routenmodule ein, es holt nichts aus ihnen.

Beim Herauslösen von S/MIME stand derselbe Fall ein zweites Mal an
(`_cert_expiry`, gebraucht von der Übersichtsseite in `app.py` und der
S/MIME-Seite). Ein zweiter Einzelfall ist keiner mehr, sondern ein Muster —
deshalb ein Ort dafür statt einer zweiten Ausnahme.

Die Richtung bleibt damit durchgehend eindeutig:

    app.py  ──bindet ein──>  routen/*.py  ──importieren──>  deps.py, hilfen.py

⚠️ Wie `deps.py`: von hier aus KEIN Import aus `webui.app` oder `webui.routen`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request

import config
import settings_store


def _cert_expiry() -> str:
    """Restlaufzeit des TLS-Zertifikats des SMTP-Lauschers, als fertiges HTML.

    Gebraucht von der Übersichtsseite und der S/MIME-Seite. Gemeint ist das
    Transportzertifikat aus `SMTP_TLS_CERT` — NICHT ein S/MIME-Zertifikat.
    """
    cert_path = Path(config.SMTP_TLS_CERT)
    if not cert_path.exists():
        return "kein Zertifikat"
    try:
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes(), default_backend())
        expires = cert.not_valid_after_utc
        days = (expires - datetime.now(timezone.utc)).days
        color = "red" if days < 14 else "orange" if days < 30 else "green"
        return f'<span style="color:{color}">{expires.strftime("%d.%m.%Y")} (noch {days} Tage)</span>'
    except Exception as exc:
        return f"Fehler: {exc}"


def _addin_base_url(request: Request) -> str:
    """Öffentliche Basis-Adresse für das Add-in-Manifest.

    Rangfolge: 1) Einstellung ADDIN_BASE_URL  2) X-Forwarded-Host  3) Anfrage.

    Gebraucht vom Add-in-Modul (Manifest) und von der Einstellungsseite in
    `app.py`, die dem Betreiber die Adresse anzeigt.
    """
    explicit = (settings_store.get("ADDIN_BASE_URL") or "").rstrip("/")
    if explicit:
        return explicit
    fwd_host  = request.headers.get("x-forwarded-host") or request.headers.get("x-original-host")
    fwd_proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    if fwd_host:
        return f"{fwd_proto}://{fwd_host.split(':')[0]}"
    return f"{request.url.scheme}://{request.url.netloc}"
