"""Ist diese Installation betriebsbereit? — die Abnahme.

ANLASS (2026-08-25)
-------------------
Vorbereitung für den halbautomatischen Aufbau einer Gateway-VM. Bevor
irgendetwas automatisiert wird, muss feststehen, WANN eine Installation fertig
ist — sonst automatisiert man ins Blaue und weiss am Ende nicht, ob das
Ergebnis taugt.

WAS DIESE PRÜFUNG ANDERS MACHT
------------------------------
Es gibt bereits zwei Sorten von Prüfungen, und beide beantworten die Frage
nicht:

  `setup_wizard.verify_*`   prüft EINZELSCHRITTE der Einrichtung — ist der
                            Connector da, sind die Regeln angelegt. Wer alle
                            Häkchen hat, weiss trotzdem nicht, ob Post
                            durchläuft.
  `health_check`            prüft JE POSTFACH — Zertifikat, Schlüssel,
                            Vorlage. Sagt nichts über die Anlage als Ganzes.

Hier steht die Anlage im Mittelpunkt, und zwar entlang der ZUSAGEN des
Produkts: erreichbar, anmeldbar, Post kommt an, Post wird bearbeitet, Post geht
zurück, Verwaltung wird benachrichtigt.

⚠️ EINE ABNAHME, DIE ALLES GRÜN MELDET, IST WERTLOS
Jeder Punkt sagt deshalb, worauf er sich stützt, und nennt ausdrücklich, was er
NICHT abdeckt. Ein `unbekannt` ist ein ehrlicheres Ergebnis als ein `ok`, das
nur bedeutet, dass niemand hingesehen hat.

Was diese Prüfung grundsätzlich nicht kann: bestätigen, dass eine echte
Nachricht signiert beim Empfänger ankommt. Dafür braucht es einen Versand — der
gehört in die spätere Etappe, die eine Testnachricht durchschickt.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

OK = "ok"
OFFEN = "offen"
UNBEKANNT = "unbekannt"          # nicht prüfbar — ausdrücklich kein „ok"
ENTFAELLT = "entfaellt"          # in dieser Betriebsart ohne Bedeutung


def _punkt(name: str, zustand: str, befund: str = "", zu_tun: str = "",
           deckt_nicht: str = "") -> dict:
    return {"name": name, "zustand": zustand, "befund": befund,
            "zu_tun": zu_tun, "deckt_nicht": deckt_nicht}


import secrets as _secrets

# Einmal je Prozess erzeugtes Token. Wird über /health ausgeliefert; die
# Abnahme ruft den öffentlichen Namen auf und prüft, ob GENAU dieses Token
# zurückkommt — damit ist die Bindung „Name → diese Instanz" bewiesen, ohne die
# eigene öffentliche IP zu kennen (umgeht das IMDS-Loch bei Standard-SKU-IPs).
_ECHO_TOKEN = _secrets.token_hex(16)


def echo_token() -> str:
    return _ECHO_TOKEN


def _oeffentliche_ip() -> str | None:
    """Öffentliche IP dieses Gateways, best effort über Azure IMDS.

    ⚠️ Bei Standard-SKU-Public-IPs liefert IMDS dieses Feld leer — deshalb ist
    das nur der erste billige Versuch, nicht der Beweis."""
    import azure_imds
    return azure_imds.public_ip()


def _oeffentliche_ip_extern() -> str | None:
    """Öffentliche IP über einen externen Echo-Dienst — greift, wo IMDS leer
    bleibt (Standard-SKU). Kurzer Timeout, mehrere Dienste als Rückfall."""
    import urllib.request
    for url in ("https://api.ipify.org", "https://checkip.amazonaws.com",
                "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:   # noqa: S310
                ip = resp.read().decode().strip()
            if ip and ("." in ip or ":" in ip):
                return ip
        except Exception:                                          # noqa: BLE001
            continue
    return None


def _self_fetch_bestaetigt(basis: str) -> bool:
    """Ruft den öffentlichen Namen auf und prüft das Echo-Token.

    Kehrt der eigene Token zurück, führt der Name nachweislich zu DIESER
    Instanz. Schlägt der Abruf fehl (Hairpin-NAT, Cert noch nicht gültig),
    heißt das NICHT „falsch" — dann greift der IP-Vergleich als Rückfall."""
    import json
    import urllib.request
    url = basis.rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:       # noqa: S310
            data = json.loads(resp.read().decode())
        return data.get("echo") == _ECHO_TOKEN
    except Exception:                                              # noqa: BLE001
        return False


def _dns_adressen(name: str) -> list[str]:
    """Alle IPs, auf die ein Name auflöst — leere Liste, wenn er nicht auflöst."""
    import socket
    try:
        infos = socket.getaddrinfo(name, None)
    except OSError:
        return []
    return sorted({i[4][0] for i in infos})


def _aussenadresse() -> dict:
    import aussenadresse
    from urllib.parse import urlparse
    basis = aussenadresse.konfiguriert()
    if not basis:
        return _punkt(
            "Von außen erreichbar", OFFEN,
            "Weder eine Außenadresse noch ein öffentlicher Name ist hinterlegt.",
            "Unter Einrichtung den öffentlichen Namen eintragen — sonst zeigen "
            "Anmelde-Rückadressen und Links in Mails ins Leere.")
    host = urlparse(basis).hostname or basis
    adressen = _dns_adressen(host)
    if not adressen:
        return _punkt(
            "Von außen erreichbar", OFFEN,
            f"{basis} — der Name löst nicht auf.",
            f"Einen DNS-Eintrag für {host} auf die öffentliche IP dieses "
            "Gateways setzen; sonst ist es von außen nicht erreichbar.")
    # Stärkster Beweis: über den öffentlichen Namen zurück zu dieser Instanz.
    if _self_fetch_bestaetigt(basis):
        return _punkt("Von außen erreichbar", OK,
                      f"{host}: über den öffentlichen Namen erreichbar und führt "
                      "nachweislich zu diesem Gateway")
    # Sonst: öffentliche IP ermitteln (IMDS, dann externer Echo) und vergleichen.
    eigene = _oeffentliche_ip() or _oeffentliche_ip_extern()
    if eigene and eigene in adressen:
        return _punkt("Von außen erreichbar", OK,
                      f"{host} → {eigene} (zeigt auf dieses Gateway)")
    if eigene:
        return _punkt(
            "Von außen erreichbar", OFFEN,
            f"{host} zeigt auf {', '.join(adressen)}, dieses Gateway hat aber "
            f"{eigene}.",
            "Den DNS-Eintrag auf die öffentliche IP dieses Gateways umstellen.")
    return _punkt(
        "Von außen erreichbar", OK, f"{host} → {', '.join(adressen)}",
        deckt_nicht="Ob diese Adresse zu genau diesem Gateway gehört, ließ sich "
                    "nicht bestätigen (Selbstabruf und öffentliche IP nicht "
                    "möglich).")


def _tls() -> dict:
    from pathlib import Path
    import config
    cert = Path(config.SMTP_TLS_CERT)
    if not cert.exists():
        return _punkt("TLS-Zertifikat", OFFEN, "Keine Zertifikatsdatei vorhanden.",
                      "Unter Erweitert ein Zertifikat über Let's Encrypt beziehen.")
    try:
        from cryptography import x509
        from datetime import datetime, timezone
        c = x509.load_pem_x509_certificate(cert.read_bytes())
        rest = (c.not_valid_after_utc - datetime.now(timezone.utc)).days
    except Exception as exc:
        return _punkt("TLS-Zertifikat", UNBEKANNT, f"nicht lesbar: {exc}"[:120],
                      "Datei prüfen — ein unlesbares Zertifikat bedeutet keine "
                      "gesicherte Verbindung.")
    if rest < 0:
        return _punkt("TLS-Zertifikat", OFFEN, f"abgelaufen seit {-rest} Tagen",
                      "Erneuern, sonst lehnt Exchange die Verbindung ab.")
    if rest < 14:
        return _punkt("TLS-Zertifikat", OFFEN, f"läuft in {rest} Tagen ab",
                      "Erneuerung anstossen oder die automatische prüfen.")
    return _punkt("TLS-Zertifikat", OK, f"gültig, noch {rest} Tage")


def _anmeldung() -> dict:
    import settings_store
    admins = settings_store.get("ADMIN_USERS") or []
    if admins:
        return _punkt("Anmeldung möglich", OK, f"{len(admins)} Entra-Konto(en) zugelassen",
                      deckt_nicht="Ob die Anmeldung durchläuft, zeigt erst ein "
                                  "Versuch — bedingter Zugriff und gesperrte "
                                  "Konten sind von hier nicht sichtbar.")
    return _punkt(
        "Anmeldung möglich", OFFEN, "Kein Entra-Konto zugelassen.",
        "Mindestens ein Konto eintragen. Der örtliche Notfallzugang bleibt "
        "bestehen, ist aber kein Dauerzustand.")


def _token_rollen(token: str) -> set[str] | None:
    """Die `roles`-Ansprüche (Anwendungsberechtigungen) aus einem JWT.

    Ohne Signaturprüfung — es geht nur darum, WELCHE Rollen erteilt sind, nicht
    um Vertrauen in das Token (das kommt direkt von MSAL). `None`, wenn das
    Token nicht als JWT lesbar ist."""
    import base64
    import json
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:                                          # noqa: BLE001
        return None
    roles = data.get("roles")
    if not isinstance(roles, list):
        return set()
    return {str(r) for r in roles}


def _exo_rollen() -> set[str] | None:
    """Anwendungsrollen im EXO-Token (Audience outlook.office365.com).

    `Exchange.ManageAsApp` ist die eigentlich kritische Verwaltungsberechtigung
    (Connector, Verteilerliste, IMAP, Message-Trace) und steht NICHT im
    Graph-Token. Wir holen das EXO-Token über dieselbe MSAL-Maschinerie, die
    keyvault/smtp_submit für Nicht-Graph-Scopes nutzen. `None`, wenn kein Token
    zu beschaffen ist."""
    import graph_client
    app = graph_client._get_msal_app()
    if app is None:
        return None
    try:
        result = app.acquire_token_for_client(
            scopes=["https://outlook.office365.com/.default"])
    except Exception:                                          # noqa: BLE001
        return None
    tok = result.get("access_token") if isinstance(result, dict) else None
    if not tok:
        return None
    return _token_rollen(tok)


def _exchange() -> dict:
    import graph_client
    token = graph_client._acquire_token()
    if not token:
        return _punkt("Exchange erreichbar", OFFEN, "Keine Graph-Zugangsdaten.",
                      "Einrichtung durchlaufen — ohne Graph kann das Gateway "
                      "weder lesen noch zustellen.")
    rollen = _token_rollen(token)
    if rollen is None:
        return _punkt("Exchange erreichbar", OK, "Zugangsdaten vorhanden",
                      deckt_nicht="Die erteilten Berechtigungen ließen sich aus "
                                  "dem Token nicht lesen.")
    erwartet = {"Mail.Send", "User.Read.All"}
    # Lesen und das Nachbessern der gesendeten Kopie deckt Mail.ReadWrite mit ab.
    if "Mail.ReadWrite" not in rollen and "Mail.Read" not in rollen:
        erwartet.add("Mail.ReadWrite")
    fehlt = sorted(r for r in erwartet if r not in rollen)

    # Exchange.ManageAsApp steht im separaten EXO-Token — dort nachsehen.
    exo = _exo_rollen()
    exo_hinweis = ""
    if exo is not None and "Exchange.ManageAsApp" not in exo:
        fehlt.append("Exchange.ManageAsApp")
    elif exo is None:
        exo_hinweis = ("Exchange.ManageAsApp ließ sich nicht prüfen "
                       "(kein EXO-Token) — greift erst im Betrieb.")

    if fehlt:
        return _punkt(
            "Exchange erreichbar", OFFEN,
            f"Zugangsdaten vorhanden, aber Berechtigung fehlt: {', '.join(sorted(set(fehlt)))}.",
            "Im Entra Admin Center die Administratorzustimmung für die fehlenden "
            "Anwendungsberechtigungen erteilen.")
    return _punkt(
        "Exchange erreichbar", OK,
        f"Zugangsdaten, Graph- und Exchange-Berechtigungen vorhanden",
        deckt_nicht=exo_hinweis)


def _postfaecher() -> dict:
    import settings_store
    cfg = settings_store.get("MAILBOX_CONFIG") or {}
    aktiv = [k for k, v in cfg.items() if v.get("sig") or v.get("smime")]
    if not aktiv:
        return _punkt(
            "Postfächer aktiviert", OFFEN, "Kein Postfach aktiviert.",
            "Ohne aktiviertes Postfach läuft ALLE Post unverändert durch — das "
            "Gateway tut dann nichts.")
    return _punkt("Postfächer aktiviert", OK, f"{len(aktiv)} Postfach/Postfächer")


def _rueckweg() -> dict:
    import settings_store
    modus = (settings_store.get("REINJECT_MODE") or "smtp").strip()
    if modus == "smtp587":
        modus = "imap"           # Altname
    if modus == "smtp":
        ziel = (settings_store.get("EXO_SMARTHOST") or "").strip()
        if not ziel:
            return _punkt("Post geht an Exchange zurück", OFFEN,
                          "Modus SMTP, aber kein Smarthost hinterlegt.",
                          "Einrichtung: Exchange-Connector anlegen.")
        return _punkt("Post geht an Exchange zurück", OK,
                      f"per SMTP-Smarthost ({ziel})",
                      deckt_nicht="Ob ausgehender Port 25 offen ist, zeigt erst "
                                  "die erste Zustellung.")
    if modus == "imap":
        # IMAP ist nur der S/MIME-Inbound-Sonderfall; der eigentliche Rückweg
        # an Exchange läuft auch hier über Graph (sendMail).
        return _punkt(
            "Post geht an Exchange zurück", OK,
            "per Graph (sendMail); IMAP nur für eingehende S/MIME-Post",
            deckt_nicht="Ob die nötigen Anwendungsberechtigungen vollständig "
                        "erteilt sind, zeigt erst der Betrieb.")
    return _punkt("Post geht an Exchange zurück", OK, "per Graph (sendMail)",
                  deckt_nicht="Ob die nötigen Anwendungsberechtigungen "
                              "vollständig erteilt sind, zeigt erst der Betrieb.")


def _signaturvorlage() -> dict:
    """Gibt es überhaupt eine Signaturvorlage mit Inhalt?

    Ein *Signature* Gateway ohne Vorlage hängt eine LEERE Signatur an und meldet
    trotzdem „verarbeitet". Ohne diesen Punkt wäre „bereit" für das Kernfeature
    trügerisch."""
    import os
    import config
    import signature_engine
    vorhanden = []
    for name in signature_engine.list_templates("signatur"):
        html_datei, _ = signature_engine._resolve_template_names(name)
        pfad = os.path.join(config.TEMPLATE_DIR, html_datei)
        try:
            if os.path.getsize(pfad) > 0:
                vorhanden.append(name)
        except OSError:
            pass
    if not vorhanden:
        return _punkt(
            "Signaturvorlage vorhanden", OFFEN,
            "Keine Signaturvorlage mit Inhalt — jede Mail liefe mit LEERER "
            "Signatur durch.",
            "Unter „Signaturen“ eine Vorlage anlegen (der Baukasten bringt "
            "Beispiele mit).")
    return _punkt("Signaturvorlage vorhanden", OK,
                  f"{len(vorhanden)} Vorlage(n): {', '.join(sorted(vorhanden))}"[:200])


def _benachrichtigung() -> dict:
    import settings_store
    if settings_store.get("NOTIFICATIONS_ENABLED") is False:
        return _punkt("Verwaltung wird benachrichtigt", ENTFAELLT,
                      "Benachrichtigungen sind abgeschaltet.")
    empf = settings_store.get("NOTIFICATION_RECIPIENTS") or []
    absender = (settings_store.get("NOTIFICATION_MAILBOX") or "").strip()
    if not empf or not absender:
        return _punkt(
            "Verwaltung wird benachrichtigt", OFFEN,
            "Absender oder Empfänger fehlt.",
            "Ohne beides bleibt jede Warnung im Protokoll stehen — "
            "auch die über ein ablaufendes Zertifikat.")
    return _punkt("Verwaltung wird benachrichtigt", OK,
                  f"{len(empf)} Empfänger über {absender}")


PRUEFUNGEN = (_aussenadresse, _tls, _anmeldung, _exchange,
              _postfaecher, _signaturvorlage, _rueckweg, _benachrichtigung)


def bericht() -> dict:
    """Alle Punkte prüfen. Ein Fehler in einem Punkt kippt nicht den Rest."""
    punkte = []
    for pruefung in PRUEFUNGEN:
        try:
            punkte.append(pruefung())
        except Exception as exc:                      # noqa: BLE001
            log.warning("Abnahme: %s fehlgeschlagen: %s", pruefung.__name__, exc)
            punkte.append(_punkt(
                pruefung.__name__.lstrip("_").replace("_", " ").capitalize(),
                UNBEKANNT, str(exc)[:150],
                "Die Prüfung selbst ist gescheitert — das ist kein Freibrief."))

    offen = [p for p in punkte if p["zustand"] == OFFEN]
    unbekannt = [p for p in punkte if p["zustand"] == UNBEKANNT]
    return {
        "punkte": punkte,
        "offen": len(offen),
        "unbekannt": len(unbekannt),
        # ⚠️ `bereit` verlangt ausdrücklich AUCH null unbekannte Punkte. Eine
        # gescheiterte Prüfung als „bereit" zu zählen wäre genau die Sorte
        # Beruhigung, die eine Abnahme wertlos macht.
        "bereit": not offen and not unbekannt,
    }
