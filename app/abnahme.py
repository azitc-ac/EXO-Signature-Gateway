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


def _aussenadresse() -> dict:
    import aussenadresse
    import settings_store
    basis = aussenadresse.konfiguriert()
    if not basis:
        return _punkt(
            "Von aussen erreichbar", OFFEN,
            "Weder eine Aussenadresse noch ein öffentlicher Name ist hinterlegt.",
            "Unter Einrichtung den öffentlichen Namen eintragen — sonst zeigen "
            "Anmelde-Rückadressen und Links in Mails ins Leere.")
    domain = (settings_store.get("LE_DOMAIN") or "").strip()
    return _punkt(
        "Von aussen erreichbar", OK, basis,
        deckt_nicht="Ob der Name von aussen wirklich auf dieses Gateway zeigt, "
                    "sieht man von hier nicht — das entscheidet das DNS."
        if not domain else "")


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


def _exchange() -> dict:
    import graph_client
    if not graph_client._acquire_token():
        return _punkt("Exchange erreichbar", OFFEN, "Keine Graph-Zugangsdaten.",
                      "Einrichtung durchlaufen — ohne Graph kann das Gateway "
                      "weder lesen noch zustellen.")
    return _punkt("Exchange erreichbar", OK, "Zugangsdaten vorhanden",
                  deckt_nicht="Geprüft ist nur, dass ein Token ausgestellt "
                              "wird — nicht jede einzelne Berechtigung.")


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
            return _punkt("Rückweg an Exchange", OFFEN,
                          "Modus SMTP, aber kein Smarthost hinterlegt.",
                          "Einrichtung: Exchange-Connector anlegen.")
        return _punkt("Rückweg an Exchange", OK, f"SMTP über {ziel}",
                      deckt_nicht="Ob ausgehender Port 25 offen ist, zeigt erst "
                                  "die erste Zustellung.")
    return _punkt("Rückweg an Exchange", OK, f"Modus {modus}",
                  deckt_nicht="Ob die nötigen Anwendungsberechtigungen "
                              "vollständig erteilt sind, zeigt erst der Betrieb.")


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
              _postfaecher, _rueckweg, _benachrichtigung)


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
