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


# Platzhalter-Kennwörter, die als unsicher gelten und einen Wechsel erzwingen:
#   "admin"    = einheitlicher Auslieferungs-Standard (config.py)
#   "changeme" = Alt-Platzhalter früherer azure-vm-setup.ps1-VMs (.env). Neue
#                Installationen setzen ihn nicht mehr (überall admin), er bleibt
#                hier aber als Schutz für Bestands-VMs, die ihn noch tragen.
# Alle MÜSSEN hier stehen, sonst meldet der Einrichtungsassistent Schritt 1
# (Kennwort ändern) fälschlich als erledigt, solange noch ein Platzhalter aktiv ist.
_DEFAULT_PASSWORDS = {"admin", "changeme", ""}


def _password_change_required() -> bool:
    """True, solange noch ein Vorgabe-/Platzhalterkennwort gilt.

    Gebraucht vom Einrichtungsassistenten (Schritt 1) und von der
    Übersichtsseite in `app.py`, die denselben Hinweis als Banner zeigt.
    """
    stored_hash = settings_store.get("ADMIN_PASSWORD_HASH") or ""
    if stored_hash:
        return False  # wurde geändert
    # Rückfall auf die Umgebungsvariable — Wechsel verlangen, wenn Platzhalter
    return config.WEBUI_PASSWORD in _DEFAULT_PASSWORDS


def _build_redirect_uri(sso: bool = False) -> str:
    """Rückadresse für den Anmeldeablauf.

    SSO: ADDIN_BASE_URL (kanonische Außenadresse, ohne Port) hat Vorrang vor
    PUBLIC_HOSTNAME. Einrichtung: immer localhost — native/Desktop-Apps dürfen
    in jedem Tenant HTTP auf localhost verwenden. Der Browser landet danach auf
    localhost (Verbindung scheitert), der Betreiber kopiert die Adresse aus der
    Adressleiste in den Assistenten.

    Gebraucht vom Einrichtungsmodul und von den Anmelderouten in `app.py`.
    """
    if sso:
        # Öffentlich wird HTTPS auf 443 ausgeliefert (Docker mappt 443:WEBUI_PORT).
        # WEBUI_PORT ist NUR der interne Bind-Port und darf NICHT in die öffentliche
        # Redirect-URI gelangen — sonst sendet der Wizard https://host:8080/... und es
        # gibt AADSTS50011. Für nicht-Standard-Außenports ADDIN_BASE_URL setzen.
        import aussenadresse
        external = aussenadresse.konfiguriert()
        if external:
            return f"{external}/auth/callback"
    return f"http://localhost:{config.WEBUI_PORT}/auth/callback"


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
