"""Exchange Online an Port 25 erkennen: an der Absenderadresse, am Client-Zertifikat oder an beidem.

ANLASS (11.11.2026)
-------------------
Ein lokal betriebenes Kunden-Gateway stand hinter einer Firewall, die bei der
Weiterleitung von Port 25 die Absenderadresse auf ihre eigene umschreibt
(Source-NAT). Die Quell-IP-Prüfung (`smtp_acl`) sah Exchange Online dann als
internes Gerät und wies jede Mail ab.

Am selben Tag belegt: Exchange Online legt beim Einliefern ein Client-Zertifikat
vor — `CN=mail.protection.outlook.com`, Aussteller DigiCert SHA2 Secure Server
CA, dasselbe (Serial/SHA-1), das ein Exchange vor Ort im Hybrid-Empfang
anfordert und prüft (`TlsDomainCapabilities mail.protection.outlook.com:
AcceptCloudServicesMail`). Am Raspi-Gateway erfolgreich geprüft, sobald die
Wurzel „DigiCert Global Root CA“ bekannt war (smtp_clientzert_wurzeln.pem).

STUFEN (`EXO_ERKENNUNG`)
------------------------
  eins     Adresse ODER Zertifikat genügt (Vorgabe)
  beide    Adresse UND Zertifikat (streng)
  adresse  nur die Adresse (Verhalten bis v1.11.23)

⚠️ Ein angefordertes Client-Zertifikat, das sich nicht prüfen lässt, BRICHT den
TLS-Aufbau ab (Pythons ssl kennt kein „anfordern, aber nur bewerten“). Deshalb
wird es in der Vorgabe NUR angefordert, wo die Adresse allein nicht reicht:
Exchange aus Microsofts Bereichen, Relay-Geräte und vorgelagerte Gateways
bekommen die Verbindung wie bisher. Die Vorgabe kann so keine Verbindung kosten,
die vorher durchging. Erst „beide“ verlangt es von allen Exchange-Verbindungen.

Erkannt wird am NAMEN und an der KETTE (OpenSSL prüft die Kette gegen certifi +
smtp_clientzert_wurzeln.pem), nie an einem einzelnen Zertifikat: Microsoft
wechselt es spätestens zum Ablauf (03.05.2027). Die Tenant-Kennung im Kopf
(handler: Tenant-Gate) bleibt in jeder Stufe Pflicht — das Zertifikat sagt
„aus Exchange Online“, nicht „aus diesem Tenant“.
"""
from __future__ import annotations

import logging

import settings_store

log = logging.getLogger(__name__)

SCHLUESSEL = "EXO_ERKENNUNG"
STUFEN = ("eins", "beide", "adresse")
VORGABE = "eins"
EXO_NAME = "mail.protection.outlook.com"


def stufe() -> str:
    wert = str(settings_store.get(SCHLUESSEL) or VORGABE).strip().lower()
    return wert if wert in STUFEN else VORGABE


def _adresse_reicht(ip: str) -> bool:
    import smtp_acl
    return bool(ip) and smtp_acl.is_allowed(ip)


def _anderer_weg(ip: str) -> bool:
    """Relay-Gerät oder vorgelagertes Gateway — für die kein Zertifikat."""
    if not ip:
        return False
    try:
        import smtp_relay
        return bool(smtp_relay.ist_relay_quelle(ip) or smtp_relay.ist_vorgelagertes_gateway(ip))
    except Exception:                                    # noqa: BLE001
        return False


def zertifikat_anfordern(ip: str) -> bool:
    """Soll für diese Verbindung beim STARTTLS ein Client-Zertifikat angefordert werden?"""
    s = stufe()
    if s == "adresse" or _anderer_weg(ip):
        return False
    if s == "beide":
        return True
    return not _adresse_reicht(ip)


def zertifikat_von_exo(peercert) -> bool:
    """Ist das (von OpenSSL bereits gegen die Wurzeln geprüfte) Zertifikat das von
    Exchange Online? Exakter Name, kein Platzhalter — `*.mail.protection.outlook.com`
    steht zwar auch im Zertifikat, ist aber nicht der Name, auf den Exchange prüft."""
    if not peercert:
        return False
    sans = {v.lower() for k, v in peercert.get("subjectAltName", ()) if k == "DNS"}
    if EXO_NAME in sans:
        return True
    for rdn in peercert.get("subject", ()):
        for k, v in rdn:
            if k == "commonName" and v.lower() == EXO_NAME:
                return True
    return False


def _peercert(session):
    extra = getattr(session, "ssl", None) or {}
    try:
        return extra.get("peercert")
    except AttributeError:
        return None


def pruefe(ip: str, session) -> tuple[bool, str]:
    """Ist diese Verbindung Exchange Online? → (zugelassen, Merkmal für das Protokoll)."""
    s = stufe()
    adresse = _adresse_reicht(ip)
    zert = zertifikat_von_exo(_peercert(session))
    if s == "adresse":
        ok = adresse
    elif s == "beide":
        ok = adresse and zert
    else:
        ok = adresse or zert
    merkmal = "+".join(m for m, ja in (("Adresse", adresse), ("Zertifikat", zert)) if ja) or "keins"
    _zaehlen(ok, adresse, zert)
    return ok, merkmal


def _zaehlen(ok: bool, adresse: bool, zert: bool) -> None:
    try:
        import stats
        if ok:
            stats.increment("exo_erkannt")
            if zert:
                stats.increment("exo_per_zertifikat")
            if zert and not adresse:
                stats.increment("exo_nur_zertifikat")
    except Exception:                                    # noqa: BLE001
        pass
