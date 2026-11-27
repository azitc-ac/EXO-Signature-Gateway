"""Let's Encrypt DNS-01 (manuell) für das TLS-Zertifikat.

Für Betreiber ohne offenen Port 80 und ohne vorhandenes Zertifikat: Das Gateway
bestellt bei Let's Encrypt, zeigt den zu setzenden TXT-Record, und stellt nach
dessen Eintrag das Zertifikat aus — ganz ohne eingehenden Port.

Zweistufig (der Nutzer muss dazwischen den DNS-Record setzen):
  start()  → Order anlegen, TXT-Record berechnen, Zustand ablegen
  finish() → Challenge auslösen, validieren, finalisieren, Zertifikat holen

⚠️ MANUELL heißt: die ERNEUERUNG (~alle 90 Tage) muss wiederholt werden. Die
Oberfläche weist darauf hin. Der Zwischenzustand (inkl. zweier privater
Schlüssel) liegt mit Rechten 600 unter DATA_DIR/tls_dns01_pending.json.

AUTOMATISCH (seit v1.12.172, aus dem Relay 0.2.57-beta übernommen):
`automatisch()` legt den TXT-Record über einen Provider aus dem Katalog
`dns_provider` an (IONOS; Azure DNS vorbereitet), wartet, bis er auf JEDEM
autoritativen Nameserver steht, löst die Challenge aus und räumt den eigenen
Wert IMMER wieder ab (finally) — fremde TXT-Werte unter demselben Namen
bleiben. Ausstellen (Einrichtung) und Erneuern (scheduler) gehen durch DIESE
eine Funktion; der Methodenschalter `LE_METHODE` entscheidet. Mehrere Namen
und Wildcards gehen nur hier.

Der Katalog `dns_provider/` ist eine unveränderte Kopie aus dem Relay
(Apache-2.0, THIRD_PARTY_NOTICES.md). Hier steht die Kopplung an das Gateway
(Einstellungen, Zertifikat schreiben) — im Katalog selbst nie;
`tests/test_dns_katalog_gleichlauf.py` hält ihn auf dem Relay-Stand.

ABWEICHUNG VOM RELAY: Domain-Schlüssel EC P-256 (wie bisher im Gateway; Exchange
Online braucht kein RSA), Schreiben über `tls_cert._write_atomar`, und das
neue Zertifikat wirkt erst nach einem Neustart (der Listener liest es beim Start).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path

import config
import settings_store

log = logging.getLogger(__name__)

_DIR_PROD = "https://acme-v02.api.letsencrypt.org/directory"
_DIR_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"
_PENDING = Path(config.DATA_DIR) / "tls_dns01_pending.json"


def _dir(staging: bool) -> str:
    return _DIR_STAGING if staging else _DIR_PROD


def _new_ec():
    from cryptography.hazmat.primitives.asymmetric import ec
    return ec.generate_private_key(ec.SECP256R1())


def _key_to_pem(key) -> str:
    from cryptography.hazmat.primitives import serialization
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()


def _key_from_pem(pem: str):
    from cryptography.hazmat.primitives import serialization
    return serialization.load_pem_private_key(pem.encode(), password=None)


async def start(domain: str, email: str, staging: bool = False) -> dict:
    """Order anlegen und den zu setzenden TXT-Record zurückgeben.

    Rückgabe: {record_name, record_value}. Wirft ValueError bei fehlender Domain.
    """
    import acme_client
    import tls_cert
    domain = (domain or "").strip().lower()
    if not domain:
        raise ValueError("Kein Hostname angegeben.")

    account_key = _new_ec()
    client = acme_client.AcmeClient(_dir(staging), account_key)
    await client.ensure_account(email or "")
    order = await client.new_order_dns(domain)
    authz = await client.get_authorization(order["authorizations"][0])
    try:
        ch = next(c for c in authz["challenges"] if c["type"] == "dns-01")
    except StopIteration:
        raise ValueError("Let's Encrypt bietet keine DNS-01-Challenge an.")

    thumb = acme_client.jwk_thumbprint(account_key)
    key_authz = f"{ch['token']}.{thumb}"
    record_value = acme_client.b64url(hashlib.sha256(key_authz.encode()).digest())

    domain_key = _new_ec()
    pending = {
        "domain": domain,
        "staging": staging,
        "account_url": client.account_url,
        "order_url": order["order_url"],
        "finalize": order["finalize"],
        "challenge_url": ch["url"],
        "account_key_pem": _key_to_pem(account_key),
        "domain_key_pem": _key_to_pem(domain_key),
    }
    tls_cert._write_atomar(_PENDING, json.dumps(pending).encode(), 0o600)
    return {"record_name": f"_acme-challenge.{domain}", "record_value": record_value}


def pending() -> dict | None:
    try:
        return json.loads(_PENDING.read_text(encoding="utf-8"))
    except Exception:                                        # noqa: BLE001
        return None


async def finish() -> dict:
    """Challenge auslösen, validieren, Zertifikat holen und installieren.

    Rückgabe: {hostnames, not_after}. Wirft ValueError mit klarer Meldung, wenn
    die Validierung scheitert (TXT falsch/nicht propagiert)."""
    import acme_client
    p = pending()
    if not p:
        raise ValueError("Keine offene DNS-01-Bestellung — bitte zuerst starten.")

    account_key = _key_from_pem(p["account_key_pem"])
    client = acme_client.AcmeClient(_dir(p["staging"]), account_key, p["account_url"])
    await client.trigger_challenge(p["challenge_url"])
    order = await client.poll_order_status(p["order_url"])
    if order.get("status") == "invalid":
        raise ValueError("Let's Encrypt konnte den TXT-Record nicht bestätigen — "
                         "Record korrekt gesetzt und bereits propagiert?")

    domain_key = _key_from_pem(p["domain_key_pem"])
    csr_der = _build_csr(domain_key, p["domain"])
    await client.finalize(p["finalize"], csr_der)
    order = await client.poll_order_status(p["order_url"])
    if order.get("status") != "valid":
        raise ValueError(f"Bestellung nicht abgeschlossen (Status {order.get('status')}).")

    cert_pem = await client.download_certificate(order["certificate"])
    not_after = _installieren(cert_pem, domain_key)
    try:
        _PENDING.unlink()
    except OSError:
        pass
    return {"hostnames": [p["domain"]], "not_after": not_after}


def _installieren(cert_pem: bytes, domain_key) -> str:
    """Zertifikat und Schlüssel an den Listener-Pfad — EIN Weg für von Hand und
    automatisch. Merkt `LE_METHODE = dns01`, damit die Erneuerung nicht über
    certbot eine ältere Linie zurückschreibt (le_certbot-Docstring).
    Rückgabe: Ablauf (ISO) oder ""."""
    import tls_cert
    tls_cert._write_atomar(Path(config.SMTP_TLS_CERT), cert_pem, 0o644)
    tls_cert._write_atomar(Path(config.SMTP_TLS_KEY), _key_to_pem(domain_key).encode(), 0o600)
    settings_store.update({"LE_METHODE": "dns01"})
    try:
        from cryptography import x509
        return x509.load_pem_x509_certificate(cert_pem).not_valid_after_utc.isoformat()
    except Exception:                                        # noqa: BLE001
        return ""


def _build_csr(domain_key, domain: str | list[str]) -> bytes:
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    namen = [domain] if isinstance(domain, str) else list(domain)
    # CN höchstens 64 Zeichen (RFC 5280); längere Namen nur im SAN — Let's Encrypt
    # nimmt CSRs ohne CN an.
    cn = next((n for n in namen if len(n) <= 64), None)
    subjekt = [x509.NameAttribute(NameOID.COMMON_NAME, cn)] if cn else []
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name(subjekt))
           .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in namen]), critical=False)
           .sign(domain_key, hashes.SHA256()))
    return csr.public_bytes(serialization.Encoding.DER)


# ── Namen ────────────────────────────────────────────────────────────────────

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


def namen_aus(text: str) -> list[str]:
    """``gw.example.com, *.example.com`` → Liste, klein, ohne Doppelte.
    Wirft ValueError bei ungültigen Namen. Ein ``*`` nur als ganzes erstes Label."""
    namen: list[str] = []
    for roh in re.split(r"[,\s;]+", (text or "").strip().lower()):
        n = roh.strip().strip(".")
        if not n:
            continue
        rest = n[2:] if n.startswith("*.") else n
        teile = rest.split(".")
        if len(teile) < 2 or not all(_LABEL.match(t) for t in teile):
            raise ValueError(f"Ungültiger Name im Zertifikat: {roh}")
        if n not in namen:
            namen.append(n)
    return namen


# ── DNS-01 über eine DNS-API (Provider-Katalog) ──────────────────────────────

METHODEN = ("http01", "dns01")


def feld_schluessel(provider: str, feld: str) -> str:
    """Einstellungsname eines Provider-Feldes: DNS01_<PROVIDER>_<FELD>.
    EINE Bildung für settings_store (Vorgaben, Geheimnisse), Oberfläche und Ablauf."""
    return f"DNS01_{provider.upper()}_{feld.upper()}"


def katalog() -> list[dict]:
    """Der Provider-Katalog für die Oberfläche: je Provider die Felder mit ihrem
    Einstellungsnamen."""
    import dns_provider
    return [{"name": n, "titel": cls.TITEL, "vorbereitet": cls.VORBEREITET,
             "felder": [{"id": feld_schluessel(n, f.schluessel), "titel": f.titel,
                         "geheim": f.geheim, "pflicht": f.pflicht, "hinweis": f.hinweis,
                         "auswahl": list(f.auswahl)} for f in cls.FELDER]}
            for n, cls in dns_provider.PROVIDER.items()]


def geheime_felder() -> set[str]:
    return {f["id"] for p in katalog() for f in p["felder"] if f["geheim"]}


def provider_aus_einstellungen(name: str | None = None, ueberschreiben: dict | None = None):
    """Provider aus den Einstellungen bauen. `ueberschreiben` (Formularwerte für
    „Verbindung testen“) gilt vor den gespeicherten Werten; ein maskiertes oder
    leeres Geheimnis nimmt den gespeicherten Wert.

    Wirft dns_provider.DnsProviderFehler (ohne Geheimnis in der Meldung)."""
    import dns_provider
    name = (name or settings_store.get("DNS01_PROVIDER") or "").strip().lower()
    if not name:
        raise dns_provider.DnsProviderFehler("Kein DNS-Provider gewählt (DNS-01 von Hand).")
    cls = dns_provider.PROVIDER.get(name)
    if cls is None:
        raise dns_provider.DnsProviderFehler(f"Unbekannter DNS-Provider: {name}")
    ueberschreiben = ueberschreiben or {}
    werte = {}
    for f in cls.FELDER:
        k = feld_schluessel(name, f.schluessel)
        w = ueberschreiben.get(k)
        if w is None or (f.geheim and (w == settings_store.MASK or w == "")):
            w = settings_store.get(k)
        werte[f.schluessel] = w
    return dns_provider.erzeugen(name, werte)


def _zeitlimit() -> int:
    try:
        return max(30, int(settings_store.get("DNS01_WARTEN_S") or 300))
    except (TypeError, ValueError):
        return 300


async def _bestellen(client, namen: list[str]) -> dict:
    if len(namen) == 1:
        return await client.new_order_dns(namen[0])
    # Mehrere Namen in EINER Bestellung. acme_client kennt nur einen Namen
    # (Spiegeldatei des Relays) — deshalb hier direkt, wie dort.
    d = await client._fetch_directory()
    r = await client._post(d["newOrder"], {"identifiers": [{"type": "dns", "value": n} for n in namen]})
    if r.status_code != 201:
        raise RuntimeError(f"ACME new-order failed: {r.status_code} {r.text[:300]}")
    order = r.json()
    order["order_url"] = r.headers.get("Location", "")
    return order


async def _fehlergrund(client, authz_urls: list[str]) -> str:
    for url in authz_urls:
        try:
            a = await client.get_authorization(url)
        except Exception:                                    # noqa: BLE001
            continue
        for ch in a.get("challenges") or []:
            err = ch.get("error") or {}
            if err.get("detail"):
                return str(err["detail"])[:300]
    return ""


async def automatisch(namen: list[str], email: str, staging: bool = False, provider=None,
                      zeitlimit: float | None = None, intervall: float = 10.0) -> dict:
    """DNS-01 vollständig über die DNS-API: anlegen → sichtbar? → Challenge →
    aufräumen (immer) → Zertifikat holen und installieren.

    Rückgabe wie `finish()`: {hostnames, not_after, staging}. Wirft ValueError
    mit einer Meldung OHNE Geheimnis."""
    import acme_client
    import dns_provider
    from dns_provider import dnsabfrage
    if not namen:
        raise ValueError("Kein Name im Zertifikat angegeben.")
    try:
        if provider is None:
            provider = provider_aus_einstellungen()
    except dns_provider.DnsProviderFehler as exc:
        raise ValueError(str(exc)) from None
    zeitlimit = _zeitlimit() if zeitlimit is None else zeitlimit

    account_key = _new_ec()
    client = acme_client.AcmeClient(_dir(staging), account_key)
    await client.ensure_account(email or "")
    order = await _bestellen(client, namen)
    thumb = acme_client.jwk_thumbprint(account_key)

    aufgaben: list[tuple[str, str, str]] = []           # (record_name, wert, challenge_url)
    for authz_url in order["authorizations"]:
        authz = await client.get_authorization(authz_url)
        if authz.get("status") == "valid":
            continue                                     # noch gültig aus einer früheren Bestellung
        ch = next((c for c in authz.get("challenges") or [] if c.get("type") == "dns-01"), None)
        if ch is None:
            raise ValueError("Let's Encrypt bietet keine DNS-01-Challenge an.")
        ident = str(authz["identifier"]["value"]).lower()   # bei Wildcards ohne „*.“
        wert = acme_client.b64url(hashlib.sha256(f"{ch['token']}.{thumb}".encode()).digest())
        aufgaben.append((f"_acme-challenge.{ident}", wert, ch["url"]))

    angelegt: list[tuple[str, str]] = []
    try:
        for name, wert, _url in aufgaben:
            await asyncio.to_thread(provider.txt_anlegen, name, wert)
            angelegt.append((name, wert))
            log.info("DNS-01: TXT %s über %s angelegt", name, provider.TITEL)
        for name, wert, _url in aufgaben:
            try:
                ns = await asyncio.to_thread(dnsabfrage.warten, name, wert, zeitlimit, intervall)
                log.info("DNS-01: TXT %s auf allen autoritativen Nameservern sichtbar (%s)",
                         name, ", ".join(ns))
            except dnsabfrage.DnsNichtErreichbar as exc:
                # Port 53 nach außen gesperrt: nicht prüfbar. Let's Encrypt fragt selbst —
                # nach dem Zeitlimit trotzdem versuchen.
                log.warning("DNS-01: %s — Challenge wird nach Ablauf des Zeitlimits trotzdem ausgelöst", exc)
        for _name, _wert, url in aufgaben:
            await client.trigger_challenge(url)
        stand = await client.poll_order_status(order["order_url"])
        if stand.get("status") == "invalid":
            grund = await _fehlergrund(client, stand.get("authorizations") or order["authorizations"])
            raise ValueError("Let's Encrypt konnte den TXT-Record nicht bestätigen"
                             + (f": {grund}" if grund else "."))
    except dns_provider.DnsProviderFehler as exc:
        raise ValueError(provider.schwaerzen(str(exc))) from None
    except dnsabfrage.PropagationZeitueberschreitung as exc:
        raise ValueError(f"DNS-01: {exc}. Wartezeit unter Einrichtung → TLS-Zertifikat → DNS-01 erhöhen.") from None
    finally:
        for name, wert in angelegt:
            try:
                await asyncio.to_thread(provider.txt_entfernen, name, wert)
                log.info("DNS-01: TXT %s wieder entfernt", name)
            except Exception as exc:                         # noqa: BLE001
                log.error("DNS-01: TXT %s NICHT entfernt — bitte von Hand löschen: %s",
                          name, provider.schwaerzen(str(exc)))

    domain_key = _new_ec()
    await client.finalize(order["finalize"], _build_csr(domain_key, namen))
    stand = await client.poll_order_status(order["order_url"])
    if stand.get("status") != "valid":
        raise ValueError(f"Bestellung nicht abgeschlossen (Status {stand.get('status')}).")
    cert_pem = await client.download_certificate(stand["certificate"])
    return {"hostnames": list(namen), "not_after": _installieren(cert_pem, domain_key),
            "staging": bool(staging)}
