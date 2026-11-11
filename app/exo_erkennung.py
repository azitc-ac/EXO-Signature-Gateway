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


# ── Abgewiesene EIGENE Exchange-Post ─────────────────────────────────────────
# Eine abgewiesene Mail mit der Tenant-Kennung des EIGENEN Tenants ist mit
# Sicherheit eigene Exchange-Post — kein Scanner aus dem Internet. Das ist der
# Fall, in dem etwas zu tun ist (Firewall mit Source-NAT ohne Zertifikat,
# Microsoft hat das Zertifikat gewechselt, strenge Stufe greift zu hart). Er
# erscheint als Band auf jeder Seite (webui/deps.py) und in der Abnahme.
FENSTER_STUNDEN = 24
_MAX_EINTRAEGE = 50


def _abweisungen_datei():
    from pathlib import Path
    import config
    return Path(config.DATA_DIR) / "exo_abgewiesen.json"


def _abweisungen_laden() -> list[dict]:
    import json
    try:
        daten = json.loads(_abweisungen_datei().read_text(encoding="utf-8"))
        return [e for e in daten if isinstance(e, dict)] if isinstance(daten, list) else []
    except (OSError, ValueError):
        return []


def _im_fenster(eintraege: list[dict]) -> list[dict]:
    from datetime import datetime, timedelta, timezone
    grenze = datetime.now(timezone.utc) - timedelta(hours=FENSTER_STUNDEN)
    behalten = []
    for e in eintraege:
        try:
            if datetime.fromisoformat(e.get("ts", "")) >= grenze:
                behalten.append(e)
        except ValueError:
            pass
    return behalten


def _eigener_tenant() -> str:
    import config
    return (config.TENANT_ID or settings_store.get("TENANT_ID") or "").strip().lower()


def merke_abweisung(ip: str, raw: bytes) -> bool:
    """Eine an der Erkennung abgewiesene Mail festhalten — aber NUR, wenn sie die
    Tenant-Kennung des eigenen Tenants trägt. True, wenn festgehalten."""
    eigen = _eigener_tenant()
    if not eigen or not raw:
        return False
    try:
        from email.parser import BytesHeaderParser
        kopf = BytesHeaderParser().parsebytes(raw)
        kennung = str(kopf.get("X-MS-Exchange-CrossTenant-Id") or "").strip().lower()
    except Exception:                                    # noqa: BLE001
        return False
    if kennung != eigen:
        return False
    import json
    from datetime import datetime, timezone
    eintraege = _im_fenster(_abweisungen_laden())
    eintraege.append({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "ip": ip})
    eintraege = eintraege[-_MAX_EINTRAEGE:]
    ziel = _abweisungen_datei()
    try:
        ziel.parent.mkdir(parents=True, exist_ok=True)
        tmp = ziel.with_suffix(".tmp")
        tmp.write_text(json.dumps(eintraege), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(ziel)
    except OSError as exc:
        log.warning("Abweisung eigener Exchange-Post nicht gespeichert: %s", exc)
    try:
        import stats
        stats.increment("exo_eigene_abgewiesen")
    except Exception:                                    # noqa: BLE001
        pass
    log.warning("SMTP: Post aus dem EIGENEN Exchange Online von %s abgewiesen — "
                "Absenderadresse nicht von Microsoft und kein gültiges Zertifikat "
                "(Stufe %s). Häufigste Ursache: Firewall mit Source-NAT.", ip, stufe())
    return True


def abweisungen_gemeldet() -> dict | None:
    """{anzahl, ip, zuletzt} der abgewiesenen eigenen Exchange-Post der letzten
    FENSTER_STUNDEN — oder None. Für Band und Abnahme."""
    eintraege = _im_fenster(_abweisungen_laden())
    if not eintraege:
        return None
    letzte = eintraege[-1]
    return {"anzahl": len(eintraege), "ip": letzte.get("ip", "?"), "zuletzt": letzte.get("ts", "")}


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
