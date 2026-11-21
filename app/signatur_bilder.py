"""Eingebettete Bilder einer Add-in-Signatur als Inline-Anhang statt Data-URI.

ANLASS (20.11.2026)
-------------------
Outlooks `Body.setSignatureAsync` nimmt höchstens 30.000 Zeichen — das ganze
HTML, Base64-Bilder eingeschlossen (MS Learn, Office.Body). Ein ordentliches
Logo verbraucht davon schnell den größten Teil; das Verfassen-Add-in fügte die
Signatur dann nicht ein („zu groß – das Gateway fügt sie beim Versand an“), und
Kunden verkleinerten ihre Bilder, bis die Qualität litt.

Microsofts eigener Weg (Beispiel „outlook-set-signature“, Seite „Add and remove
attachments“): das Bild mit `addFileAttachmentFromBase64Async(…, {isInline:
true})` anhängen und im HTML mit `<img src="cid:Name">` darauf verweisen. Dann
zählt für die Grenze nur noch das schlanke HTML.

Diese Datei zerlegt die Signatur dafür: HTML mit `cid:gwsig-…`-Verweisen und
die Bilder (Name, Typ, Base64). Der Name hängt am Inhalt — dasselbe Bild hat
immer denselben Namen; das Add-in hängt es so nicht doppelt an.

Ersetzt das Gateway beim Versand eine Add-in-Signatur
(`mail_processor.addin_signatur_entfernen`), räumt es Bildteile mit diesem
Präfix weg, auf die kein HTML mehr verweist (`verwaiste_entfernen`).
"""
from __future__ import annotations

import email.message
import hashlib
import re

PRAEFIX = "gwsig-"
_ENDUNG = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/gif": "gif"}
_DATA_SRC = re.compile(
    r"""(src\s*=\s*)(["'])data:(image/(?:png|jpe?g|gif));base64,([A-Za-z0-9+/=\s]+)\2""",
    re.IGNORECASE)


def auslagern(html: str) -> tuple[str, list[dict]]:
    """(HTML mit cid-Verweisen, [{name, typ, base64}]) — jedes Bild einmal."""
    bilder: dict[str, dict] = {}

    def ersetzen(m: re.Match) -> str:
        typ = m.group(3).lower().replace("image/jpg", "image/jpeg")
        b64 = re.sub(r"\s+", "", m.group(4))
        name = f"{PRAEFIX}{hashlib.sha256(b64.encode()).hexdigest()[:12]}.{_ENDUNG.get(typ, 'png')}"
        bilder.setdefault(name, {"name": name, "typ": typ, "base64": b64})
        return f"{m.group(1)}{m.group(2)}cid:{name}{m.group(2)}"

    return _DATA_SRC.sub(ersetzen, html or ""), list(bilder.values())


def _dateiname(teil: email.message.Message) -> str:
    return (teil.get_filename() or teil.get_param("name") or "").strip()


def _verweise(msg: email.message.Message) -> str:
    """Alle HTML-Teile zusammen, kleingeschrieben — wonach cid-Verweise gesucht werden."""
    texte = []
    for teil in msg.walk():
        if teil.get_content_type() == "text/html" and not teil.is_multipart():
            roh = teil.get_payload(decode=True) or b""
            texte.append(roh.decode(teil.get_content_charset() or "utf-8", errors="replace"))
    return "\n".join(texte).lower()


def verwaiste_entfernen(msg: email.message.Message) -> int:
    """Bildteile des Add-ins (Dateiname gwsig-…), auf die kein HTML mehr verweist,
    aus der Nachricht nehmen. Liefert die Zahl der entfernten Teile."""
    html = _verweise(msg)
    entfernt = 0
    for eltern in [t for t in msg.walk() if t.is_multipart()]:
        behalten = []
        for kind in eltern.get_payload():
            name = _dateiname(kind)
            if not kind.is_multipart() and name.lower().startswith(PRAEFIX):
                cid = (kind.get("Content-ID") or "").strip().strip("<>").strip().lower()
                if not ((cid and f"cid:{cid}" in html) or f"cid:{name.lower()}" in html):
                    entfernt += 1
                    continue
            behalten.append(kind)
        if len(behalten) != len(eltern.get_payload()):
            eltern.set_payload(behalten)
    return entfernt
