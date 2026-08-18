"""Bestätigung der E-Mail-Adresse bei der Zertifizierungsstelle — automatisch.

Nach einer Bestellung schickt die Zertifizierungsstelle eine Mail an das
Postfach; erst der Klick darin löst die Ausstellung aus. Für einzelne Postfächer
ist das zumutbar. Bei zwanzig oder hundert nicht: Dort wartet man auf zwanzig
oder hundert Menschen, und jede Bestellung bleibt so lange offen.

Das Gateway kann diesen Klick übernehmen — es hat Zugriff auf das Postfach und
weist damit dieselbe Kontrolle nach, die die Bestätigung belegen soll. Bei
CASTLE ist genau das der reguläre Weg (RFC 8823 automatisiert ihn vollständig).

⚠️ WAS DABEI ENTFÄLLT: die Absicht der Zertifizierungsstelle, dass ein MENSCH
zustimmt. Deshalb ist die Automatik opt-in je Postfach und nirgends Vorgabe.

⚠️ NACHGEBAUT, NICHT DOKUMENTIERT. Die Partner-API von Certum bietet für die
E-Mail-Verifizierung nichts an (geprüft). Der hier verwendete Aufruf stammt aus
dem Kundenportal:

    POST {backend.uri}/domain-verification?verificationId=…&partnerId=…

`backend.uri` wird NICHT fest eingetragen, sondern zur Laufzeit aus derselben
Konfigurationsdatei gelesen, die auch das Portal lädt. Ein Wechsel der
Backend-Adresse bricht die Automatik dadurch nicht. Ändert Certum den Aufruf
selbst, schlägt sie fehl — sichtbar, mit Vermerk, und der Klick bleibt als
Rückfallebene bestehen.
"""
import logging
import re
from urllib.parse import parse_qs, urlparse

import httpx

log = logging.getLogger(__name__)

# Nur diese Zertifizierungsstellen kennen wir; für andere wird nichts versucht.
_UNTERSTUETZT = ("certmanager.certum.pl", "certmanager.test.certum.pl")

_KONFIG_PFAD = "/assets/config/config.release.json"
_ZEITLIMIT = 20.0


class NichtUnterstuetzt(Exception):
    """Der Bestätigungsweg dieser Zertifizierungsstelle ist nicht bekannt."""


def _teile(link: str) -> tuple[str, str, str]:
    """(Basis, verificationId, partnerId) aus dem Link der Bestätigungsmail."""
    u = urlparse(link)
    if u.hostname not in _UNTERSTUETZT:
        raise NichtUnterstuetzt(f"Unbekannte Adresse: {u.hostname}")
    q = parse_qs(u.query)
    vid = (q.get("verificationId") or [""])[0]
    pid = (q.get("partnerId") or [""])[0]
    if not vid or not pid:
        raise NichtUnterstuetzt("Im Link fehlen verificationId oder partnerId")
    return f"{u.scheme}://{u.hostname}", vid, pid


async def _backend_adresse(basis: str) -> str:
    """Adresse der Schnittstelle — aus der Konfiguration des Portals.

    Fest eingetragen wäre sie ein Wartungsfall bei jedem Serverwechsel; das
    Portal selbst liest sie ebenfalls hier."""
    async with httpx.AsyncClient(timeout=_ZEITLIMIT) as c:
        r = await c.get(basis + _KONFIG_PFAD)
    r.raise_for_status()
    uri = ((r.json().get("backend") or {}).get("uri") or "").rstrip("/")
    if not uri.startswith("https://"):
        raise NichtUnterstuetzt(f"Unbrauchbare Backend-Adresse: {uri!r}")
    return uri


async def bestaetigen(link: str) -> tuple[bool, str]:
    """Die Bestätigung auslösen. Liefert (ok, Meldung).

    Wirft nicht — der Aufrufer steht mitten in einem Bestellvorgang und soll
    daran nicht scheitern. Ein Fehlschlag bedeutet: Der Mensch klickt eben
    selbst, der Link steht ohnehin in der Oberfläche.
    """
    try:
        basis, vid, pid = _teile(link)
        ziel = await _backend_adresse(basis) + "/domain-verification"
        async with httpx.AsyncClient(timeout=_ZEITLIMIT) as c:
            r = await c.post(ziel, params={"verificationId": vid, "partnerId": pid})
    except NichtUnterstuetzt as exc:
        return False, str(exc)
    except Exception as exc:
        log.warning("Automatische Bestätigung fehlgeschlagen: %s", exc)
        return False, f"Aufruf fehlgeschlagen: {exc}"

    # 200 und 202 gelten beide: Die Stelle nimmt die Bestätigung teils sofort
    # an, teils zur Bearbeitung entgegen.
    if r.status_code in (200, 202, 204):
        log.info("Bestätigung bei der Zertifizierungsstelle ausgelöst (HTTP %s)", r.status_code)
        return True, f"bestätigt (HTTP {r.status_code})"
    return False, f"abgelehnt (HTTP {r.status_code})"
