"""PROXY-Protokoll an Port 25: die echte Absenderadresse hinter einer Firewall.

ANLASS (2026-11-11)
-------------------
Ein Gateway läuft beim Kunden im eigenen Netz. Die Firewall leitet Port 25
weiter und schreibt dabei die Quelladresse auf ihre eigene um (Source-NAT).
Für das Gateway kam Exchange Online damit aus 10.99.0.1 statt aus Microsofts
Adressbereichen — die Quell-IP-Prüfung wies jede Mail des ersten aktivierten
Postfachs mit 554 ab („nicht in der Geräteliste“).

Das PROXY-Protokoll (HAProxy, v1 Text / v2 binär) ist der Standardweg, die
ursprüngliche Adresse mitzugeben: Die vorgeschaltete Komponente setzt vor die
SMTP-Sitzung eine Zeile mit Quelle und Ziel. aiosmtpd liest sie, sobald
`_proxy_timeout` gesetzt ist.

⚠️ NUR von eingetragenen Adressen. Wer den Vorspann von beliebigen Quellen
annähme, ließe jeden seine Absenderadresse frei wählen — und damit die
Quell-IP-Prüfung umgehen. Umgekehrt muss eine eingetragene Adresse den
Vorspann IMMER senden; ohne ihn schließt aiosmtpd die Verbindung. Geräte im
eigenen Netz, die direkt einliefern, sind davon nicht berührt.

Leere Liste = aus (Vorgabe). Die Einstellung wirkt bei der nächsten
Verbindung, ohne Neustart.
"""
from __future__ import annotations

import ipaddress
import logging

import settings_store

log = logging.getLogger(__name__)

SCHLUESSEL = "SMTP_PROXY_QUELLEN"
WARTEZEIT_S = 10.0          # so lange wartet der Listener auf den Vorspann

_stand: tuple[tuple, list] = ((), [])     # (Rohwert, Netze) — Zwischenspeicher


def normalisieren(werte) -> list[str]:
    """Eingabe (Liste oder Text mit Komma/Leerzeichen/Zeilen) → geprüfte Netze.

    Wirft ValueError mit der ersten ungültigen Angabe — eine vertippte Adresse
    soll beim Speichern auffallen, nicht später als stiller Ausfall.
    """
    if isinstance(werte, str):
        teile = werte.replace(",", " ").split()
    else:
        teile = [str(w).strip() for w in (werte or []) if str(w).strip()]
    netze = []
    for t in teile:
        try:
            netze.append(str(ipaddress.ip_network(t, strict=False)))
        except ValueError:
            raise ValueError(f"Keine gültige Adresse oder Netz: {t}") from None
    return list(dict.fromkeys(netze))


def _netze() -> list:
    global _stand
    roh = tuple(settings_store.get(SCHLUESSEL) or [])
    if roh != _stand[0]:
        netze = []
        for t in roh:
            try:
                netze.append(ipaddress.ip_network(t, strict=False))
            except ValueError:
                log.warning("%s: ungültiger Eintrag %r übergangen", SCHLUESSEL, t)
        _stand = (roh, netze)
    return _stand[1]


def ist_proxy(ip: str) -> bool:
    """Muss eine Verbindung von `ip` mit dem PROXY-Vorspann beginnen?"""
    if not ip:
        return False
    try:
        adr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(adr in n for n in _netze())


def echte_quelle(proxy_data) -> tuple[str, int] | None:
    """(IP, Port) des ursprünglichen Absenders aus dem Vorspann, oder None.

    None auch für den v2-Befehl LOCAL (Zustandsprüfung des Proxys ohne
    weitergeleitete Verbindung): Ohne Ursprungsadresse gibt es nichts zu prüfen,
    und die Sitzung darf nicht unter der Adresse des Proxys weiterlaufen.
    """
    if not proxy_data or not getattr(proxy_data, "valid", False):
        return None
    src = getattr(proxy_data, "src_addr", None)
    if src is None:
        return None
    try:
        ip = str(ipaddress.ip_address(str(src)))
    except ValueError:
        return None
    return ip, int(getattr(proxy_data, "src_port", 0) or 0)
