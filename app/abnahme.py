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


# `deckt_nicht` („Nicht geprüft: …“ in der Oberfläche) NUR, wenn im Einzelfall
# etwas nicht prüfbar war oder eine echte, benennbare Lücke bleibt — nie als
# Dauerfloskel („zeigt erst der Betrieb“). Am 09.11.2026 standen elf davon da,
# drei längst behoben; die Abnahme las sich wie eine Mängelliste ohne Aussicht.
# tests/test_abnahme_ton.py.
from texte import zahl as _zahl, wort as _wort   # noqa: E402  (gemeinsam, siehe texte.py)


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
    import urllib.error
    import urllib.request
    url = basis.rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:       # noqa: S310
            data = json.loads(resp.read().decode())
        return data.get("echo") == _ECHO_TOKEN
    except urllib.error.HTTPError as e:
        # /health antwortet 503, wenn der SMTP-Listener nicht bindet — das echo
        # steht auch dann im Rumpf. Für den Namensabgleich zählt nur das Token.
        try:
            return json.loads(e.read().decode()).get("echo") == _ECHO_TOKEN
        except Exception:                                          # noqa: BLE001
            return False
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
    # Dieselbe Schwelle wie die automatische Erneuerung: Ist sie erreicht und
    # das Zertifikat noch alt, hat die Erneuerung nicht gegriffen (TLS-Vorfall
    # 30.09.). Bis v1.10.92 rechnete die Abnahme fest mit 14 Tagen, die
    # Übersicht mit LE_RENEW_DAYS — zwei Antworten auf dieselbe Frage.
    import settings_store
    schwelle = int(settings_store.get("LE_RENEW_DAYS") or 14)
    if rest <= 0:
        return _punkt("TLS-Zertifikat", OFFEN, f"abgelaufen seit {-rest} Tagen — die "
                      "Zustellung ans Gateway kann scheitern (450 4.4.317), ausgehende "
                      "Post bleibt hängen.",
                      "Einrichtung → TLS-Zertifikat: sofort erneuern.")
    if rest <= schwelle:
        return _punkt("TLS-Zertifikat", OFFEN, f"läuft in {rest} Tagen ab — die "
                      "automatische Erneuerung hätte bis hierhin greifen müssen.",
                      "Einrichtung → TLS-Zertifikat: Erneuerung prüfen.")
    return _punkt("TLS-Zertifikat", OK, f"gültig, noch {rest} Tage")


def _anmeldung() -> dict:
    import settings_store
    admins = settings_store.get("ADMIN_USERS") or []
    if admins:
        return _punkt("Anmeldung möglich", OK, f"{_zahl(len(admins), 'Entra-Konto', 'Entra-Konten')} zugelassen")
    return _punkt(
        "Anmeldung möglich", OFFEN, "Kein Entra-Konto zugelassen.",
        "Mindestens ein Konto eintragen. Der örtliche Notfallzugang bleibt "
        "bestehen, ist aber kein Dauerzustand.")


def _exchange() -> dict:
    import graph_client
    # Frisch, nicht aus dem Cache — sonst fehlt eine eben erteilte Rolle bis zum
    # Ablauf des Tokens (berechtigungen.graph_rollen, gleicher Grund).
    token = graph_client.frisches_app_token()
    if not token:
        return _punkt("Exchange erreichbar", OFFEN, "Keine Graph-Zugangsdaten.",
                      "Einrichtung durchlaufen — ohne Graph kann das Gateway "
                      "weder lesen noch zustellen.")
    # Soll aus der EINEN Liste, aus der auch der Antrag entsteht
    # (berechtigungen.status ← setup_wizard._GRAPH/_EXO_PERMISSION_SPECS). Bis
    # v1.12.42 stand hier ein fester Auszug ({Mail.Send, User.Read.All} +
    # Mail.ReadWrite + ManageAsApp): Fehlten etwa MailboxSettings.ReadWrite
    # (Abwesenheit) oder Calendars.Read, meldete der Punkt trotzdem OK.
    import berechtigungen
    liste = berechtigungen.status()
    unbekannt = sorted({b["api"] for b in liste if b["erteilt"] is None})
    if "Graph" in unbekannt:
        return _punkt("Exchange erreichbar", UNBEKANNT, "Zugangsdaten vorhanden",
                      "Die erteilten Berechtigungen ließen sich aus dem Token nicht lesen.")
    fehlt = sorted({b["name"] for b in liste if b["erteilt"] is False and b["benoetigt"]})
    exo_hinweis = ("Exchange-Rollen — kein Exchange-Token erhalten."
                   if "Exchange" in unbekannt else "")
    if fehlt:
        return _punkt(
            "Exchange erreichbar", OFFEN,
            f"Zugangsdaten vorhanden, aber Berechtigung fehlt: {', '.join(fehlt)}.",
            "Einrichtung → „Fehlende Berechtigungen erteilen“ (Admin-Anmeldung).")
    return _punkt(
        "Exchange erreichbar", OK,
        f"Zugangsdaten und alle {sum(1 for b in liste if b['benoetigt'])} benötigten "
        "Graph- und Exchange-Berechtigungen vorhanden",
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
    return _punkt("Postfächer aktiviert", OK, _zahl(len(aktiv), "Postfach", "Postfächer"))


def _rueckweg() -> dict:
    import settings_store
    modus = settings_store.reinject_mode()   # normalisiert die Altnamen imap/smtp587 → graph_plus
    if modus == "smtp":
        ziel = (settings_store.get("EXO_SMARTHOST") or "").strip()
        if not ziel:
            return _punkt("Post geht an Exchange zurück", OFFEN,
                          "Modus SMTP, aber kein Smarthost hinterlegt.",
                          "Einrichtung: Exchange-Connector anlegen.")
        return _punkt("Post geht an Exchange zurück", OK,
                      f"per SMTP-Smarthost ({ziel})")
    if modus == "graph_plus":
        # Graph++ = Graph als Hauptweg; IMAP nur für eingehende S/MIME-Post,
        # 587 nur für gemischte Empfänger.
        return _punkt(
            "Post geht an Exchange zurück", OK,
            "per Graph (sendMail); IMAP nur für eingehende S/MIME-Post, 587 nur für gemischte Empfänger")
    return _punkt("Post geht an Exchange zurück", OK, "per Graph (sendMail)")


def _vorlage_standard_doppelt() -> dict:
    """Heißt eine eigene Vorlage „Standard“? Seit v1.12.113 zeigt die Oberfläche
    die Standardvorlage (intern „default“) als „Standard“ — eine eigene Vorlage
    gleichen Namens stünde dann zweimal ununterscheidbar in jeder Auswahl. Neu
    anlegen lässt sich der Name nicht mehr (signature_engine.name_reserviert);
    diese Prüfung findet den Bestand. Nur lokal."""
    import os
    import config
    name = "Keine eigene Vorlage namens „Standard“"
    try:
        dateien = os.listdir(config.TEMPLATE_DIR)
    except OSError:
        return _punkt(name, ENTFAELLT, "Vorlagenverzeichnis nicht lesbar.")
    treffer = sorted(d[:-5] for d in dateien
                     if d.lower().endswith(".html") and d[:-5].lower() == "standard")
    if not treffer:
        return _punkt(name, OK, "Der Name „Standard“ gehört allein der Standardvorlage.")
    return _punkt(name, OFFEN,
                  f"Die eigene Vorlage „{treffer[0]}“ heißt so wie die Standardvorlage "
                  "und ist in den Auswahllisten nicht von ihr zu unterscheiden.",
                  "Signaturen → Vorlage öffnen → „Umbenennen“ (z. B. in „Standard-eigen“).")


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
                  f"{_zahl(len(vorhanden), 'Vorlage', 'Vorlagen')}: {', '.join(sorted(vorhanden))}"[:200])


def _anmeldenachweis() -> dict:
    """Meldet sich das Gateway mit dem Zertifikat an — und ist kein Client
    Secret mehr übrig? Bis v1.10.79 legte jede erneute Anmeldung in Schritt 5
    ein weiteres Secret mit Laufzeit bis 2099 an."""
    import graph_client
    import setup_wizard
    name = "Anmeldung der App per Zertifikat"
    if graph_client.zertifikat_nachweis() is None:
        return _punkt(name, OFFEN, "Kein lesbares Auth-Zertifikat (auth.pfx).",
                      "Einrichtung → Entra App-Registrierung → „Erneut anmelden“.")
    if setup_wizard.secret_vorhanden():
        return _punkt(name, OFFEN, "Es ist noch ein Client Secret hinterlegt.",
                      "Einrichtung → Entra App-Registrierung → „Erneut anmelden“. Danach "
                      "löscht das Gateway die Secrets, sobald Entra das Zertifikat "
                      "annimmt (einige Minuten). Steht CLIENT_SECRET in der .env, dort "
                      "entfernen.")
    return _punkt(name, OK, "Zertifikat, kein Secret in den Einstellungen")


def _smime_schluessel() -> dict:
    """Liegen S/MIME-Privatschlüssel im Klartext auf der Platte?

    Gemessen an den DATEIEN, nicht an der Einstellung: Ein gesetztes Passwort
    sagt nichts über Schlüssel, die vorher abgelegt wurden. Schlüssel in Azure
    Key Vault liegen gar nicht hier und zählen deshalb nicht."""
    import settings_store
    import smime_store
    name = "S/MIME-Schlüssel verschlüsselt abgelegt"
    roh = smime_store.unverschluesselte_schluessel()
    if not roh:
        return _punkt(name, OK, "Kein Privatschlüssel liegt im Klartext.")
    anzahl = f"{_zahl(len(roh), 'Schlüsseldatei liegt', 'Schlüsseldateien liegen')} im Klartext."
    weg = "Einstellungen → S/MIME → Schlüsselablage: "
    if settings_store.get("SMIME_KEY_ENCRYPT") is False:
        return _punkt(name, OFFEN, anzahl + " Die Verschlüsselung ist abgeschaltet.",
                      weg + "„Private Keys verschlüsseln“ einschalten und ein Passwort setzen.")
    if smime_store.schluessel_passwort():
        return _punkt(name, OFFEN, anzahl + " Ein Passwort ist gesetzt.",
                      "Gateway neu starten — beim Start werden sie verschlüsselt.")
    return _punkt(
        name, OFFEN, anzahl + " Es ist kein Passwort gesetzt.",
        weg + "Passwort setzen; das Gateway verschlüsselt dann alle Schlüssel neu. "
        "Notiere es außerhalb des Gateways. Es schützt Schlüsseldateien, die "
        "einzeln abhandenkommen, nicht ein vollständiges Backup (das die "
        "Einstellungen mit enthält). Am stärksten schützt Azure Key Vault: "
        "Dort verlassen die Schlüssel den Tresor nicht.")


def _admin_passwort() -> dict:
    """Gilt für die Weboberfläche noch das Auslieferungs-Kennwort?"""
    from webui.hilfen import _password_change_required
    name = "Admin-Kennwort geändert"
    if not _password_change_required():
        return _punkt(name, OK, "Ein eigenes Kennwort ist gesetzt.")
    return _punkt(name, OFFEN,
                  "Für die Weboberfläche gilt noch das Auslieferungs-Kennwort.",
                  "Einrichtung → Schritt 1: ein eigenes Kennwort setzen.")


def _tls_rueckweg() -> dict:
    """Kann der Rückweg das TLS-Zertifikat der Gegenstelle prüfen (v1.10.112)?

    Offen, solange ein Weiterleitungsziel die TLS-Einstellung nur aus der
    Migration hat (`tls_uebernommen`, ohne Prüfung) oder der Exchange-Smarthost
    als IP-Adresse eingetragen ist — dann scheitert die Hostnamenprüfung.
    """
    import config
    import domain_routing
    import settings_store
    import tls_ausgehend
    name = "TLS-Rückweg mit Zertifikatsprüfung"
    offen = []
    smarthost = config.EXO_SMARTHOST or settings_store.get("EXO_SMARTHOST") or ""
    if smarthost and tls_ausgehend.ist_ip(smarthost):
        offen.append(f"Der Exchange-Smarthost ist als IP-Adresse eingetragen ({smarthost}); "
                     "das Zertifikat lautet auf einen Namen, die Zustellung über SMTP scheitert")
    uebernommen = sorted(zid for zid, t in domain_routing.ziele().items()
                         if isinstance(t, dict) and t.get("tls_uebernommen"))
    if uebernommen:
        offen.append((_wort(len(uebernommen), "Weiterleitungsziel ", "Weiterleitungsziele "))
                     + ", ".join(uebernommen)
                     + _wort(len(uebernommen), " läuft", " laufen")
                     + " noch ohne Zertifikatsprüfung, wie vor dem Update")
    if not offen:
        return _punkt(name, OK, "Ausgehende TLS-Verbindungen prüfen das Zertifikat, "
                      "oder die Ausnahme ist bewusst gewählt.")
    return _punkt(name, OFFEN, "; ".join(offen) + ".",
                  "SMTP-Relay → Weiterleitungsziele: je Ziel „Zertifikat prüfen“ wählen "
                  "(an, wenn der Host ein Name mit gültigem Zertifikat ist) und speichern. "
                  "Smarthost: als Namen *.mail.protection.outlook.com eintragen.")


def _smime_regeln_stand() -> dict:
    """Sind die S/MIME-Eingangsregeln nach dem aktuellen Soll geschrieben?

    Seit v1.12.34 nimmt „SMIME Signed Inbound“ Kalenderelemente aus
    (regel_soll.SMIME_KALENDER_AUSNAHME). Bestehende Regeln bekommen das nur,
    wenn die Einrichtung sie neu schreibt. Liest nur den gemerkten Stand
    (regel_soll.SMIME_REGELN_STAND), fragt Exchange nicht ab — den Live-Abgleich
    macht tools/tenant_soll_check.py mit demselben Soll."""
    import regel_soll
    import settings_store
    name = "S/MIME-Eingangsregeln aktuell"
    if not settings_store.get("SMIME_RULES_CREATED"):
        return _punkt(name, ENTFAELLT, "S/MIME-Eingangsregeln nicht eingerichtet.")
    stand = settings_store.get("_SMIME_REGELN_STAND") or 1
    if stand >= regel_soll.SMIME_REGELN_STAND:
        return _punkt(name, OK, "Regeln nach aktuellem Soll geschrieben.")
    return _punkt(name, OFFEN,
                  "Die Regel „SMIME Signed Inbound“ nimmt Kalenderelemente noch nicht aus: "
                  "signierte Einladungen externer Absender laufen übers Gateway.",
                  "Einrichtung → S/MIME – Inbound Transport-Regeln → "
                  "„Transport-Regeln neu erstellen“.")


def _direktversand_probe() -> dict:
    """Kommt Post über den Direktversand an (v1.12.31)? Nur lokal: liest den
    Stand der täglichen Probe, keine Netzabfrage. Dieselbe Bewertung wie
    Tagesbericht und Oberfläche (direktversand_probe.bewertung) — „eingerichtet“
    allein ist kein „ok“."""
    import direktversand
    import direktversand_probe
    name = "Direktversand-Probe"
    if not direktversand.aktiv():
        return _punkt(name, OK, "Direktversand ist aus — keine Probe nötig.")
    if not direktversand_probe.postfach():
        return _punkt(name, OFFEN,
                      "Der Direktversand ist an, aber kein Probe-Postfach eingetragen. "
                      "Ob Post über den eigenen Weg ankommt (und nicht im Junk-Ordner), prüft niemand.",
                      "Einstellungen → Mail-Routing → Selbst zustellen: ein internes Postfach "
                      "als Probe-Postfach eintragen (z. B. ein geteiltes). Ist der Zugriff der "
                      "App per RBAC for Applications eingegrenzt, muss es im Bereich liegen.")
    ampel, text = direktversand_probe.bewertung()
    if ampel == "ok":
        return _punkt(name, OK, text)
    # „gesendet“/„noch keine Probe“ sind noch kein Nachweis — unbekannt, nicht ok.
    return _punkt(name, OFFEN if ampel == "fehler" or "Junk" in text else UNBEKANNT, text,
                  "Ergebnis der Probe unter Einstellungen → Mail-Routing ansehen; "
                  "„Probe jetzt“ startet sie sofort.")


def _dmarc_berichte() -> dict:
    """Werden die DMARC-Sammelberichte gelesen, und gibt es Befunde? Nur lokal
    (Stand des letzten Abrufs), dieselbe Bewertung wie Tagesbericht und
    Oberfläche (dmarc_berichte.bewertung). Ohne eingetragenes Ziel: ok — die
    Auswertung ist freiwillig."""
    import dmarc_berichte
    name = "DMARC-Berichte"
    ampel, text = dmarc_berichte.bewertung()
    if ampel == "keine":
        return _punkt(name, OK, "Auswertung nicht eingerichtet (freiwillig).")
    if ampel == "ok":
        return _punkt(name, OK, text)
    # Noch kein Abruf: unbekannt. Befunde oder ein gescheiterter Abruf: offen.
    stufe = UNBEKANNT if "noch kein Abruf" in text else OFFEN
    return _punkt(name, stufe, text,
                  "Einstellungen → DMARC: Befunde und Abrufstand ansehen; „Jetzt abrufen“ "
                  "liest sofort. Bei einer Microsoft-365-Gruppe muss Group-Conversation.Read.All "
                  "erteilt sein — mit Postfach-Eingrenzung an der DMARC-Lese-App („Fehlende "
                  "Berechtigungen erteilen“ legt sie an).")


def _wartende_zertifikate() -> dict:
    """Warten eingesammelte Empfängerzertifikate auf eine Entscheidung?

    Solange eines wartet, wird an diesen Partner nicht verschlüsselt. Seit
    v1.10.110 landen dort auch Zertifikate aus dem Bestand, die der Nachlauf
    (`smime_harvest.bestand_nachpruefen`) beanstandet hat.
    """
    import cert_wartestand
    name = "Keine Empfängerzertifikate in Warteschlange"
    wartend = cert_wartestand.liste()
    if not wartend:
        return _punkt(name, OK, "Kein Zertifikat wartet auf eine Entscheidung.")
    return _punkt(name, OFFEN,
                  f"{_zahl(len(wartend), 'Empfängerzertifikat wartet', 'Empfängerzertifikate warten')} auf eine Entscheidung; "
                  "an diese Partner wird bis dahin nicht verschlüsselt.",
                  "S/MIME → „Zertifikate, die auf Freigabe warten“: je Zeile den Grund "
                  "prüfen, freigeben oder verwerfen.")


def _erneuerung_unentschieden() -> dict:
    """Gibt es Postfächer mit selbst bestellendem Bezugsweg, bei denen „Automatisch
    erneuern“ nie entschieden wurde (Schlüssel fehlt)? Bis v1.12.114 setzten
    Sammel- und Auto-Bestellung den Haken nicht — der tägliche Lauf erneuerte sie
    nie. Seit v1.12.115 setzen sie ihn; den Bestand still umzustellen hieße,
    ungefragt (womöglich kostenpflichtige) Verlängerungen einzuschalten. Erledigt
    durch Speichern der Einstellung des Postfachs (an ODER aus). Nur lokal."""
    import ca_backends
    import settings_store
    name = "Automatische Erneuerung für alle bestellten Postfächer entschieden"
    offen = []
    for adresse, cfg in sorted((settings_store.get("CA_USER_CONFIG") or {}).items()):
        # Entschieden ist auch ein Altbestand mit „notify_user“: der Scheduler
        # leitet auto_renew daraus ab (scheduler._send_user_renewal_notification).
        if not isinstance(cfg, dict) or "auto_renew" in cfg or "notify_user" in cfg \
                or not cfg.get("backend"):
            continue
        try:
            if ca_backends.get_backend(cfg["backend"]).can_auto_renew():
                offen.append(adresse)
        except Exception:                                   # noqa: BLE001
            continue
    if not offen:
        return _punkt(name, OK, "Für jedes Postfach mit bestellendem Bezugsweg ist entschieden.")
    return _punkt(name, OFFEN,
                  f"{_zahl(len(offen), 'Postfach', 'Postfächer')} ohne Entscheidung — sie werden nicht "
                  f"automatisch erneuert: {', '.join(offen[:5])}" + (" …" if len(offen) > 5 else ""),
                  "S/MIME → beim Postfach „Automatisch erneuern“ setzen oder bewusst leer "
                  "lassen und speichern.")


def _ablauf_ohne_erneuerung() -> dict:
    """Läuft ein eigenes Signaturzertifikat ab (Warnfrist CERT_WARN_DAYS), dessen
    automatische Erneuerung BEWUSST abgewählt ist (auto_renew False)? Bis
    v1.12.145 galt das als „entschieden“ (_erneuerung_unentschieden) und war nur
    am Ablauf-Band zu sehen. Nach dem Ablauf geht die Post unsigniert
    (smime_signer.nur_abgelaufen). Ab 30 Tagen nach dem Ablauf nicht mehr
    gemeldet — dann ist das Weglassen offenkundig gewollt. Nur lokal."""
    import settings_store
    import smime_store
    name = "Kein abgewähltes Zertifikat läuft unbemerkt ab"
    warn = int(settings_store.get("CERT_WARN_DAYS") or 14)
    cfg_alle = settings_store.get("CA_USER_CONFIG") or {}
    offen = []
    for c in smime_store.list_certs():
        cfg = cfg_alle.get(c.get("email", "")) or {}
        tage = c.get("days_left", 999)
        if c.get("error") or not isinstance(cfg, dict) or cfg.get("auto_renew") is not False:
            continue
        if -30 <= tage <= warn:
            offen.append(f"{c['email']} ({'abgelaufen' if tage < 0 else _zahl(tage, 'Tag', 'Tage')})")
    if not offen:
        return _punkt(name, OK, "Kein Zertifikat mit abgewählter Erneuerung läuft in der Warnfrist ab.")
    return _punkt(name, OFFEN,
                  f"{_zahl(len(offen), 'Zertifikat läuft', 'Zertifikate laufen')} ab und "
                  f"{_wort(len(offen), 'wird', 'werden')} nicht automatisch erneuert: "
                  + ", ".join(offen[:5]) + (" …" if len(offen) > 5 else "")
                  + ". Nach dem Ablauf gehen Mails dieser Postfächer unsigniert hinaus.",
                  "S/MIME → beim Postfach „Jetzt erneuern“ oder „Automatisch erneuern“ setzen. "
                  "Ist das Weglassen gewollt, erledigt sich der Punkt 30 Tage nach dem Ablauf.")


def _bestand_durchsicht() -> dict:
    """Liegen Empfängerzertifikate von vor v1.10.115 ohne bekannten Aussteller
    im Bestand? Sie konnten ungeprüft hineingekommen sein; die Nachprüfung
    (`smime_harvest.bestand_nachpruefen`) legt sie zur Durchsicht vor."""
    import smime_harvest
    name = "Empfängerzertifikate ohne bekannten Aussteller durchgesehen"
    offen = smime_harvest.durchsicht()
    if not offen:
        return _punkt(name, OK, "Kein Bestandszertifikat wartet auf Durchsicht.")
    return _punkt(name, OFFEN,
                  f"{_zahl(len(offen), 'Empfängerzertifikat', 'Empfängerzertifikate')} im Bestand {_wort(len(offen), 'führt', 'führen')} auf keinen bekannten "
                  "Aussteller und stammen aus der Zeit vor der Signaturprüfung; an diese "
                  "Partner wird weiterhin damit verschlüsselt.",
                  "S/MIME → „Zertifikate zur Durchsicht“: je Zeile behalten oder entfernen, "
                  "im Zweifel beim Partner nachfragen.")


# Ab so vielen geprüften Mails ohne einen Fall „ohne Kennung“ gilt die
# Beobachtung als ausreichend, um die strenge Prüfung zu empfehlen.
TENANT_BEOBACHTUNG_MIN = 200


def _tenant_herkunft() -> dict:
    """Weist das Gateway Post aus dem Exchange-Weg ohne Tenant-Kennung ab?

    Die Herkunftsprüfung (handler.py, Tenant-Gate) weist eine FREMDE Kennung
    ab, nimmt eine FEHLENDE aber an — eine Vorgabe, die Mailfluss vor Strenge
    stellt. Ob bei diesem Tenant Post ohne Kennung kommt, messen die Zähler
    (stats tenant_ohne_kopf, Bezugsgröße tenant_geprueft)."""
    import settings_store
    import stats
    name = "Herkunftsprüfung streng (Tenant-Kennung Pflicht)"
    if settings_store.get("RELAY_TENANT_CHECK") is False:
        return _punkt(name, OFFEN, "Die Herkunftsprüfung ist in der Konfigurationsdatei "
                      "abgeschaltet (RELAY_TENANT_CHECK).",
                      "Nur für ein bewusst mehrmandantiges Relay so lassen.")
    if settings_store.get("RELAY_TENANT_STRENG") is True:
        return _punkt(name, OK, "Post aus dem Exchange-Weg ohne Tenant-Kennung wird abgewiesen.")
    gesamt = stats.get()
    geprueft, ohne = gesamt.get("tenant_geprueft", 0), gesamt.get("tenant_ohne_kopf", 0)
    if ohne:
        return _punkt(name, OFFEN,
                      f"{ohne} von {geprueft + ohne} Mails aus dem Exchange-Weg kamen ohne "
                      "Tenant-Kennung und wurden angenommen.",
                      "Im Protokoll nach „keine CrossTenant-Id“ suchen und die Quelle klären; "
                      "erst danach unter Erweitert → Quell-IP-Allowlist „Post ohne "
                      "Tenant-Kennung abweisen“ einschalten.")
    if geprueft < TENANT_BEOBACHTUNG_MIN:
        return _punkt(name, OK, f"Beobachtung läuft: {geprueft} Mails geprüft, keine ohne "
                      f"Kennung. Empfehlung folgt ab {TENANT_BEOBACHTUNG_MIN}.")
    return _punkt(name, OFFEN,
                  f"{geprueft} Mails geprüft, keine ohne Tenant-Kennung — die strenge "
                  "Prüfung kann eingeschaltet werden.",
                  "Erweitert → Quell-IP-Allowlist → „Post ohne Tenant-Kennung abweisen“.")


def _exo_erkennung() -> dict:
    """Woran wird Exchange Online am Eingang erkannt, und wie oft am Zertifikat?
    (exo_erkennung.py). Die Zahl steht immer da — fällt der Anteil mit
    Zertifikat plötzlich, hat Microsoft das Zertifikat gewechselt.

    Grenze: In der Stufe „eins“ wird das Zertifikat nur angefordert, wo die
    Adresse nicht reicht — wie viele Verbindungen eines vorlegen WÜRDEN, zeigt
    die Zahl dort nicht. Bewusst kein fester deckt_nicht-Text (test_abnahme_ton)."""
    import exo_erkennung
    import stats
    name = "Exchange Online wird am Eingang erkannt"
    g = stats.get()
    ok, mit, nur = (g.get(k, 0) for k in ("exo_erkannt", "exo_per_zertifikat",
                                          "exo_nur_zertifikat"))
    stufe = {"eins": "Adresse oder Zertifikat", "beide": "Adresse und Zertifikat",
             "adresse": "nur Adresse"}[exo_erkennung.stufe()]
    ab = exo_erkennung.abweisungen_gemeldet()
    if ab:
        return _punkt(name, OFFEN,
                      f"{_zahl(ab['anzahl'], 'Mail', 'Mails')} aus dem eigenen Exchange Online in den "
                      f"letzten 24 Stunden abgewiesen, zuletzt von {ab['ip']} (Stufe: {stufe}).",
                      "Erweitert → SMTP-Quell-IP-Allowlist: Stufe „Adresse oder Zertifikat“ "
                      "wählen; läuft das Gateway hinter einer Firewall mit Source-NAT, dort "
                      "die Weiterleitung von Port 25 prüfen.")
    return _punkt(name, OK, f"Stufe: {stufe}. {ok} Exchange-Mails angenommen; davon {mit} mit "
                  f"geprüftem Zertifikat, {nur} über das Zertifikat, obwohl die Absenderadresse "
                  "nicht von Microsoft war (etwa hinter einer Firewall mit NAT).")


def _quell_ip_liste() -> dict:
    """Ist die Liste der Exchange-Adressbereiche geladen? Leer lässt die
    Quell-IP-Prüfung alles durch (fail-safe, smtp_acl.is_allowed)."""
    import settings_store
    import smtp_acl
    name = "Quell-IP-Allowlist geladen"
    if settings_store.get("SMTP_SOURCE_ACL_ENABLED") is False:
        return _punkt(name, OFFEN, "Die Quell-IP-Allowlist ist ausgeschaltet; jede Adresse "
                      "darf an Port 25 einliefern.", "Erweitert → Quell-IP-Allowlist einschalten.")
    n = smtp_acl.range_count()
    if not n:
        return _punkt(name, OFFEN, "Keine Exchange-Adressbereiche geladen — bis dahin lässt "
                      "die Prüfung jede Quelle durch.",
                      "Erweitert → Quell-IP-Allowlist → „Jetzt aktualisieren“; scheitert das, "
                      "den Abruf von endpoints.office.com prüfen.")
    # Alter mitprüfen: Der Abruf läuft alle 12 h. Scheitert er dauerhaft, blieb
    # bis v1.12.42 eine beliebig alte Liste „OK“ — neue Microsoft-Bereiche
    # fehlen dann, und Exchange wird an Port 25 abgewiesen.
    import time
    stand = smtp_acl.stand_ts()
    if not stand:
        return _punkt(name, UNBEKANNT, f"{n} Exchange-Adressbereiche geladen, Abrufzeit unbekannt.",
                      "Erweitert → Quell-IP-Allowlist → „Jetzt aktualisieren“.")
    tage = (time.time() - stand) / 86400
    datum = time.strftime("%d.%m.%Y %H:%M", time.localtime(stand))
    if tage > 3:
        return _punkt(name, OFFEN, f"{n} Exchange-Adressbereiche, zuletzt abgerufen am {datum} "
                      f"({int(tage)} Tage) — der Abruf alle 12 Stunden scheitert.",
                      "Erweitert → Quell-IP-Allowlist → „Jetzt aktualisieren“; scheitert das, "
                      "den Abruf von endpoints.office.com prüfen.")
    return _punkt(name, OK, f"{n} Exchange-Adressbereiche geladen (Stand {datum}).")


def _csp_berichte() -> dict:
    """Würde die geplante CSP etwas brechen? Grundlage, um sie von
    „nur berichtend" auf „durchgesetzt" umzustellen (csp_berichte.py)."""
    import csp_berichte
    name = "Keine Verstöße gegen die geplante Content-Security-Policy"
    arten = csp_berichte.liste()
    if not arten:
        return _punkt(name, OK, "Seit v1.11.7 hat kein Browser einen Verstoß gemeldet.")
    beispiele = "; ".join(f"{e['direktive']} {e['quelle']} auf {e['seite']} ({e['anzahl']}×)"
                          for e in arten[:3])
    return _punkt(name, OFFEN,
                  f"{_zahl(len(arten), 'Art', 'Arten')} von Verstößen gemeldet, u. a. {beispiele}.",
                  "Nichts zu tun für den Betrieb — die Richtlinie ist nur berichtend. "
                  "Die Liste ist die Arbeitsgrundlage, bevor sie durchgesetzt wird.")


def _fair_use() -> dict:
    """Mehr Postfächer aktiviert, als Lizenz oder Freigrenze erlauben?"""
    import license as _lic
    name = "Postfächer innerhalb der Lizenz"
    st = _lic.fair_use_state()
    grenze = "das lizenzierte Limit" if st["licensed"] else "die Freigrenze"
    if not st["exceeded"]:
        return _punkt(name, OK, f"{st['count']} Postfächer aktiviert"
                      + (f", {grenze} liegt bei {st['limit']}." if st["limit"] else "."))
    return _punkt(name, OFFEN,
                  f"{st['count']} Postfächer sind für Signatur/S/MIME aktiviert — "
                  f"{grenze} ({st['limit']}) ist überschritten.",
                  "Lizenz unter Einstellungen → Anbindung & Lizenzen beziehen "
                  "oder per Mail an support@zarenko.net.")


def _vertragsbedingungen() -> dict:
    """Ist einer geänderten Fassung der Vertragsbedingungen zuzustimmen?

    Nur Dokumenten, denen schon einmal zugestimmt wurde — durch die
    Erstzustimmung führen die Gates (Ziffer 13.3 der Nutzungsbedingungen)."""
    import legal_consent
    name = "Geänderten Vertragsbedingungen zustimmen"
    offen = legal_consent.offene_fassungen()
    if not offen:
        return _punkt(name, OK, "Allen geltenden Fassungen ist zugestimmt.")
    betroffen = ", ".join(f"{d['label_de']} (Fassung {d['version']})" for d in offen)
    return _punkt(
        name, OFFEN,
        f"Betroffen: {betroffen}. Bis zur Zustimmung gilt die bisherige Fassung "
        "fort; neue kostenpflichtige Vorgänge sind so lange nicht möglich. "
        "Der Mailfluss läuft unverändert weiter.",
        "Die geänderten Texte ansehen und zustimmen.")


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
            "Einstellungen → Benachrichtigungen: Absender und Empfänger eintragen, "
            "damit Warnungen wie ein ablaufendes Zertifikat nicht unbemerkt bleiben.")
    return _punkt("Verwaltung wird benachrichtigt", OK,
                  f"{len(empf)} Empfänger über {absender}")


def _naa() -> dict:
    """Bootstrap-App für NAA vorbereitet? Trägt das Verfassen-Add-in und die
    stille Anmeldung im Self-Service-Tab. Bis v1.9.172 wurde das nur von Hand
    eingetragen — eine Neuinstallation hatte es nie, ohne dass es auffiel."""
    import settings_store
    name = "Add-in und Tab: Anmeldung ohne Fenster (NAA)"
    if not (settings_store.get("ADDIN_ENABLED") or settings_store.get("SELF_SERVICE_ENABLED")):
        return _punkt(name, ENTFAELLT, "Weder Add-in noch Self-Service eingeschaltet")
    client_id = (settings_store.get("BOOTSTRAP_CLIENT_ID") or "").strip()
    if not client_id:
        return _punkt(name, OFFEN, "Keine Bootstrap-Anwendung eingetragen.",
                      "Einrichtung (Schritt Anmeldung) durchlaufen.")
    import graph_client
    import httpx
    import setup_wizard
    token = graph_client._acquire_token()
    if not token:
        return _punkt(name, UNBEKANNT, "Keine Graph-Zugangsdaten.",
                      "Einrichtung durchlaufen.")
    r = httpx.get("https://graph.microsoft.com/v1.0/applications"
                  f"?$filter=appId eq '{client_id}'&$select=spa,api,identifierUris",
                  headers={"Authorization": f"Bearer {token}"}, timeout=10)
    r.raise_for_status()
    apps = r.json().get("value", [])
    if not apps:
        return _punkt(name, OFFEN, f"Zu {client_id} gibt es keine Registrierung.",
                      "Einrichtung (Schritt Anmeldung) erneut durchlaufen.")
    fehlt = setup_wizard.naa_luecken(apps[0], client_id)
    if fehlt:
        return _punkt(name, OFFEN, "Es fehlt: " + "; ".join(fehlt) + ".",
                      "Einstellungen → Einrichtung → „Anmeldung prüfen“ → „In Entra ergänzen“.")
    return _punkt(name, OK, "Bootstrap-App vollständig vorbereitet")


def _regel_empfaenger() -> dict:
    """Signatur-Regel ohne Empfängerbedingung? Installationen vor v1.8.64
    tragen noch SentToScope=NotInOrganization — interne Post erreicht das
    Gateway dann nie, gemischte Post nur zum Teil."""
    import regel_bedingung
    name = "Transportregel ohne Empfängerbedingung"
    p = regel_bedingung.pruefen()
    if p["zustand"] == regel_bedingung.UNBEKANNT:
        return _punkt(name, UNBEKANNT, f"Regel „{p['regel']}“ ließ sich nicht lesen.",
                      "Exchange-Zugang prüfen (Einrichtung → Connector).")
    # Das Absender-Gate liest dieselbe Abfrage mit. Bis v1.12.42 meldete es nur
    # das Band, und das nur mit eingerichtetem Bypass-Wächter (täglicher Lauf).
    if p.get("gate_offen"):
        return _punkt(name, OFFEN,
                      f"Die Regel „{p['regel']}“ ist AKTIV, aber ohne Absender-Gate "
                      "(FromMemberOf leer): JEDER interne Absender läuft durchs Gateway.",
                      "Postfächer → Speichern setzt das Gate wieder auf die Verteilerliste.")
    if p["zustand"] == regel_bedingung.BEDINGUNG:
        return _punkt(name, OFFEN,
                      f"Die Regel trägt SentToScope={p['sent_to_scope']} — interne "
                      "Post erreicht das Gateway nicht, bei gemischten Empfängern "
                      "geht der interne Teil unsigniert vorbei.",
                      "Einstellungen → Signatur → Erweiterte Einstellungen → „Empfängerbedingung entfernen“.")
    return _punkt(name, OK, f"Regel „{p['regel']}“ ohne Empfängerbedingung")


def _regel_kennung() -> dict:
    """Nehmen die Gateway-Regeln GENAU die eigene Markierung aus? Bis v1.10.43
    war sie überall `1` — Post eines anderen Gateways umging dadurch die
    eingehenden S/MIME-Regeln (kein Zertifikat übernommen, nichts entschlüsselt)."""
    import regel_kennung
    name = "Transportregeln mit eigener Loop-Kennung"
    p = regel_kennung.pruefen()
    if p["zustand"] == regel_kennung.UNBEKANNT:
        return _punkt(name, UNBEKANNT, "Die Regeln ließen sich nicht lesen.",
                      "Exchange-Zugang prüfen (Einrichtung → Connector).")
    if p["zustand"] == regel_kennung.KEINE:
        return _punkt(name, ENTFAELLT, "Noch keine Gateway-Regel angelegt.")
    falsch = [r["name"] for r in p["regeln"] if not r["ok"]]
    if falsch:
        return _punkt(name, OFFEN,
                      f"{_zahl(len(falsch), 'Regel nimmt', 'Regeln nehmen')} nicht genau die eigene Kennung "
                      f"{p['soll']} aus: {', '.join(falsch)}. Post anderer Gateways "
                      "umgeht dann die eingehenden S/MIME-Regeln.",
                      "Einstellungen → Signatur → Erweiterte Einstellungen → „Regeln prüfen“ → „Regeln angleichen“.")
    return _punkt(name, OK, f"{_zahl(len(p['regeln']), 'Regel', 'Regeln')} mit Kennung {p['soll']}")


def _regel_auto_ausnahme() -> dict:
    """Nehmen die Routing-Regeln Auto-Antworten aus? Sonst laufen die
    Abwesenheitsnotizen der Postfächer (MAIL FROM:<>) übers Gateway und
    scheitern auf dem Rückweg an externe Empfänger (5.7.64/4.4.4). Die
    Ausnahme kam mit v1.9.88 nur an neu angelegte Regeln.

    Liest nur den gemerkten Live-Befund (regel_auto_ausnahme.gemerkt) — lokal,
    damit er auch als Aufgabe taugt. Frisch wird er beim Start jeder neuen
    Fassung, nach der Connector-Einrichtung und nach „Ausnahme ergänzen“."""
    import regel_auto_ausnahme as raa
    import settings_store
    name = "Routing-Regeln nehmen Abwesenheitsnotizen und Kalender aus"
    g = raa.gemerkt()
    # Ein gemerkter Befund zählt auch, wenn der Merker der Einrichtung
    # zurückgesetzt wurde (Loop-Header geändert) — die Regeln gibt es weiter.
    if not settings_store.get("EXO_CONNECTOR_CREATED") and not g:
        return _punkt(name, ENTFAELLT, "Exchange-Connector noch nicht eingerichtet.")
    weg = "Einrichtung → Schritt 6 „EXO Outbound Connector + Transport Rule“ → „Ausnahme ergänzen“."
    if not g or g.get("zustand") == raa.UNBEKANNT:
        return _punkt(name, UNBEKANNT, "Die Regeln wurden noch nicht gelesen.",
                      "Einrichtung → Schritt 6 „EXO Outbound Connector + Transport Rule“ → „Regeln prüfen“.")
    if g["zustand"] == raa.KEINE:
        return _punkt(name, ENTFAELLT, "Keine Gateway-Routing-Regel gefunden.")
    if g["zustand"] == raa.FEHLT:
        return _punkt(name, OFFEN,
                      f"Ausnahme fehlt an: {', '.join(g.get('fehlt') or [])}. Ohne sie laufen "
                      "Abwesenheitsnotizen übers Gateway und erreichen externe Empfänger "
                      "nicht, und Besprechungsanfragen gehen den Umweg übers Gateway.", weg)
    return _punkt(name, OK, f"Ausnahme vorhanden (geprüft {g.get('geprueft', '')}).")


def _implicit_anmeldung() -> dict:
    """Trägt die Bootstrap-App die Anmeldung per Microsoft-Knopf (Web-
    Rückadresse + ID-Token-Ausgabe)? Gebraucht von SSO, Add-in-Login und
    Self-Service. Bis v1.12.42 ungeprüft (setup_wizard.implicit_luecke)."""
    import settings_store
    name = "Anmeldung per Microsoft-Knopf (Bootstrap-App)"
    if not (settings_store.get("ADMIN_USERS") or settings_store.get("ADDIN_ENABLED")
            or settings_store.get("SELF_SERVICE_ENABLED")):
        return _punkt(name, ENTFAELLT, "Weder Entra-Anmeldung, Add-in noch Self-Service eingerichtet")
    client_id = (settings_store.get("BOOTSTRAP_CLIENT_ID") or "").strip()
    if not client_id:
        return _punkt(name, OFFEN, "Keine Bootstrap-Anwendung eingetragen.",
                      "Einrichtung (Schritt Anmeldung) durchlaufen.")
    import graph_client
    import httpx
    import setup_wizard
    token = graph_client._acquire_token()
    if not token:
        return _punkt(name, UNBEKANNT, "Keine Graph-Zugangsdaten.", "Einrichtung durchlaufen.")
    r = httpx.get("https://graph.microsoft.com/v1.0/applications"
                  f"?$filter=appId eq '{client_id}'&$select=web",
                  headers={"Authorization": f"Bearer {token}"}, timeout=10)
    r.raise_for_status()
    apps = r.json().get("value", [])
    if not apps:
        return _punkt(name, OFFEN, f"Zu {client_id} gibt es keine Registrierung.",
                      "Einrichtung (Schritt Anmeldung) erneut durchlaufen.")
    fehlt = setup_wizard.implicit_luecke(apps[0])
    if fehlt:
        return _punkt(name, OFFEN, f"Es fehlt: {fehlt}. Die Anmeldung scheitert mit AADSTS700054.",
                      "Einstellungen → Einrichtung → „Anmeldung prüfen“ → „In Entra ergänzen“.")
    return _punkt(name, OK, "Web-Rückadresse und ID-Token-Ausgabe eingetragen")


def _addin_rollen() -> dict:
    """Dürfen normale Nutzer Add-ins benutzen? Fehlen die Benutzerrollen in der
    Standard-Rollenzuweisungsrichtlinie, laufen die Add-ins nur bei Admins —
    beim Einrichten fällt das nicht auf (26.10.2026: leere Richtlinie im Tenant,
    Verfassen-Add-in bei Nutzern nie gestartet)."""
    import addin_rollen
    import settings_store
    name = "Add-ins für alle Nutzer erlaubt (Benutzerrollen)"
    if not settings_store.get("ADDIN_ENABLED"):
        return _punkt(name, ENTFAELLT, "Add-in nicht eingeschaltet")
    p = addin_rollen.pruefen()
    if p["zustand"] == addin_rollen.UNBEKANNT:
        return _punkt(name, UNBEKANNT, "Rollenzuweisungsrichtlinie ließ sich nicht lesen.",
                      "Exchange-Zugang prüfen (Einrichtung → Connector).")
    if p["zustand"] == addin_rollen.FEHLT:
        return _punkt(name, OFFEN,
                      f"In „{p['richtlinie']}“ fehlt: {', '.join(p['fehlt'])}. Die Add-ins "
                      "laufen dann nur bei Admins; Nutzer sehen „Der Zugriff auf Add-Ins "
                      "wurde von Ihrer Organisation eingeschränkt“.",
                      "Exchange Admin Center → Rollen → Benutzerrollen → "
                      f"„{p['richtlinie']}“ → Andere Rollen: die fehlenden anhaken.")
    return _punkt(name, OK, f"„{p['richtlinie']}“ enthält die Rollen für Add-ins",
                  deckt_nicht="Postfächer mit einer eigenen Rollenzuweisungsrichtlinie "
                              "prüft dieser Punkt nicht.")


def _addin_fassung_art(art: str, name: str) -> dict:
    """Liefert Exchange den Postfächern noch eine ältere Fassung des
    Verfassen-Add-ins aus — und muss der Betreiber dafür etwas tun?

    Bei Event-Add-ins verlangt JEDE Manifest-Änderung eine neue Bestätigung im
    Admin Center (MS Learn, „Deploy manifest updates“); danach verteilt
    Exchange Postfach für Postfach. Die Einordnung kommt aus
    `addin_verteilung.einordnen` — derselben wie in der Postfachprüfung:
    Ist die neue Fassung schon bei einem Postfach angekommen, ist das
    Aktualisieren erledigt, und solange die Verteilung läuft, gibt es keine
    Aufgabe (bis v1.12.34 gab es eine, mit „Aktualisieren“ — obwohl nichts zu
    tun war). Liest nur das Ergebnis der letzten Postfachprüfung
    (MAILBOX_HEALTH), fragt nichts live ab; die 72-h-Frist wird hier gegen
    JETZT gerechnet, nicht gegen den Prüfzeitpunkt."""
    import addin_verteilung as av
    import health_check
    import settings_store
    soll = next((a["version"] for a in health_check._addin_soll() if a["art"] == art), "")
    if not soll:
        return _punkt(name, ENTFAELLT, "Add-in nicht eingerichtet.")
    gesundheit = settings_store.get("MAILBOX_HEALTH") or {}
    lagen: dict[str, list[str]] = {}
    for email, h in gesundheit.items():
        c = ((h or {}).get("checks") or {}).get("addin_" + art) or {} if isinstance(h, dict) else {}
        if c.get("soll") != soll:
            continue                       # nicht abgefragt, oder gegen eine ältere Soll-Fassung
        lagen.setdefault(av.einordnen(art, soll, c.get("ist") or ""), []).append(email)
    gesamt = sum(len(v) for v in lagen.values())
    if not gesamt:
        return _punkt(name, UNBEKANNT, "Noch keine Postfachprüfung gegen die aktuelle Fassung.",
                      "Postfächer → Status aktualisieren.")
    aktuell = len(lagen.get(av.AKTUELL, []))
    if lagen.get(av.ZUWEISUNG):
        z = sorted(lagen[av.ZUWEISUNG])
        return _punkt(name, OFFEN,
                      f"{_zahl(len(z), 'Postfach', 'Postfächer')} ohne neue Fassung, 72 Stunden nachdem "
                      f"sie andere erreicht hat (z. B. {z[0]}).", av.ZUWEISUNG_WEG)
    if lagen.get(av.AKTUALISIEREN):
        alt = sorted(lagen[av.AKTUALISIEREN])
        return _punkt(name, OFFEN,
                      f"Kein Postfach hat die neue Fassung {soll}; {len(alt)} mit älterer "
                      f"(z. B. {alt[0]}).", av.aktualisieren_weg(art))
    if lagen.get(av.VERTEILUNG):
        return _punkt(name, OK,
                      f"Verteilung läuft: {aktuell} von {gesamt} Postfächern aktuell. Nichts "
                      "zu tun; Exchange braucht bis zu 72 Stunden.")
    if not aktuell:
        return _punkt(name, ENTFAELLT, f"Bei keinem der {gesamt} geprüften Postfächer installiert.")
    zeiten = sorted(str((((h or {}).get("checks") or {}).get("addin_" + art) or {}).get("checked_at") or "")
                    for h in gesundheit.values() if isinstance(h, dict))
    stand = f", geprüft {zeiten[-1]}" if zeiten and zeiten[-1] else ""
    return _punkt(name, OK, f"{aktuell} von {gesamt} Postfächern aktuell "
                            f"(Stand der letzten Postfachprüfung{stand}).")


def _addin_fassung() -> dict:
    return _addin_fassung_art("verfassen", "Verfassen-Add-in aktuell")


def _addin_knopf_fassung() -> dict:
    """Wie `_addin_fassung`, für das Knopf-Add-in („Signatur und Abwesenheit“).
    Bis v1.12.85 gab es die Aufgabe nur für das Verfassen-Add-in; ein veralteter
    Knopf zeigte sich allein als Warnung in der Postfachprüfung."""
    return _addin_fassung_art("knopf", "Knopf-Add-in aktuell")


def _ohne_hybrid(name: str) -> dict | None:
    """EWS wirkt nur für Hybrid-Nutzer. Ohne den Haken „Hybrid-Nutzer einbeziehen“
    blendet Einstellungen die Karte „Exchange lokal (EWS)“ aus (seit v1.12.113) —
    eine offene EWS-Aufgabe führte dann auf eine unsichtbare Karte. Deshalb
    entfallen alle EWS-Prüfungen gemeinsam über diesen Helfer."""
    import exo_mailboxes
    if exo_mailboxes.hybrid_eingeschaltet():
        return None
    return _punkt(name, ENTFAELLT, "Hybrid-Nutzer nicht einbezogen (Einstellungen → Postfächer).")


def _ews_bereich() -> dict:
    """Verweigert Exchange lokal dem EWS-Dienstkonto Hybrid-Postfächer
    (ErrorImpersonateUserDenied/ErrorImpersonationDenied)?

    Das Gateway kann den Management Scope der lokalen Organisation nicht selbst
    erweitern — es spricht dort nur EWS mit dem Dienstkonto, keine
    Verwaltungsshell. Bis v1.12.85 stand die Ablehnung nur in der Prüfung des
    einzelnen Postfachs. Liest nur MAILBOX_HEALTH (`ews_code` aus
    health_check._check_ews_zugriff/_check_postfach_lesen), fragt nichts live ab."""
    import settings_store
    name = "EWS-Zugriff auf alle Hybrid-Postfächer"
    if (entfaellt := _ohne_hybrid(name)) is not None:
        return entfaellt
    if not (settings_store.get("EWS_ONPREM_URL") or "").strip():
        return _punkt(name, ENTFAELLT, "Anbindung an Exchange lokal (EWS) nicht eingerichtet.")
    gesundheit = settings_store.get("MAILBOX_HEALTH") or {}
    abgelehnt: dict[str, str] = {}
    for email, h in gesundheit.items():
        checks = (h or {}).get("checks") or {} if isinstance(h, dict) else {}
        for c in (checks.get("abwesenheit"), checks.get("postfach_lesen")):
            if isinstance(c, dict) and c.get("ews_code"):
                abgelehnt[email] = c["ews_code"]
    if not abgelehnt:
        return _punkt(name, OK, "Keine Ablehnung durch Exchange lokal in der letzten Postfachprüfung.")
    namen = sorted(abgelehnt)
    if any(v == "ErrorImpersonationDenied" for v in abgelehnt.values()):
        weg = ("In der Exchange-Verwaltungsshell dem Dienstkonto die Rolle zuweisen: "
               "New-ManagementRoleAssignment -Role ApplicationImpersonation -User <Konto> "
               "-CustomRecipientWriteScope <Scope>. Danach Postfächer → Status aktualisieren.")
    else:
        weg = ("Diese Postfächer liegen außerhalb des Management Scope des Dienstkontos. "
               "Einstellungen → Exchange lokal (EWS) → Zugriffsbereich: Einrichtung über eine "
               "Gruppe — danach nimmt das Gateway neue Hybrid-Nutzer selbst auf. Oder in der "
               "Exchange-Verwaltungsshell den Scope erweitern (Get-ManagementScope zeigt den "
               "Filter). Danach Postfächer → Status aktualisieren.")
    if len(namen) == 1:
        befund = f"Exchange lokal verweigert dem Dienstkonto das Postfach {namen[0]}."
        weg = weg.replace("Diese Postfächer liegen", "Dieses Postfach liegt", 1)
    else:
        befund = (f"Exchange lokal verweigert dem Dienstkonto {len(namen)} Postfächer "
                  f"(z. B. {', '.join(namen[:3])}).")
    return _punkt(name, OFFEN, befund, weg)


def _ews_gruppe() -> dict:
    """Entsprechen die Mitglieder der Gruppe des EWS-Dienstkontos dem Soll
    (Hybrid-Nutzer mit Signatur oder S/MIME)? Seit v1.12.92. Nur lokal: letzte
    Messung (ews_bereich.abgleichen, stündlich im Scheduler) gegen das aktuelle
    Soll — so erscheint ein frisch aktivierter Hybrid-Nutzer sofort, auch mit
    ausgeschalteter automatischer Pflege."""
    import ews_bereich
    name = "Gruppe des EWS-Dienstkontos entspricht dem Soll"
    if (entfaellt := _ohne_hybrid(name)) is not None:
        return entfaellt
    a = ews_bereich.abweichung()
    gruppe = ews_bereich.gruppe_name()
    weg = ("Einstellungen → Exchange lokal (EWS) → Zugriffsbereich → „Jetzt abgleichen“ "
           "(oder „Mitglieder automatisch pflegen“ einschalten).")
    if a["zustand"] == "entfaellt":
        return _punkt(name, ENTFAELLT, "Zugriffsbereich nicht über eine Gruppe eingerichtet.")
    if a["zustand"] == "unbekannt":
        return _punkt(name, UNBEKANNT, a.get("grund", "Noch nicht abgeglichen."), weg)
    if a["zustand"] == "ok":
        return _punkt(name, OK, f"Gruppe {gruppe} entspricht dem Soll.")
    teile = []
    if a["fehlen"]:
        teile.append(f"{len(a['fehlen'])} Hybrid-Postfächer fehlen (z. B. {', '.join(a['fehlen'][:3])})")
    if a["zuviel"]:
        teile.append(f"{len(a['zuviel'])} Mitglieder ohne Signatur/S/MIME (z. B. "
                     f"{', '.join(a['zuviel'][:3])})")
    if a["nicht_gefunden"]:
        teile.append(f"{len(a['nicht_gefunden'])} Adressen ohne Benutzerobjekt im AD "
                     f"(z. B. {', '.join(sorted(a['nicht_gefunden'])[:3])})")
    if a["fehler"]:
        teile.append(f"{len(a['fehler'])} Änderungen gescheitert ({a['fehler'][0].get('detail', '')[:80]})")
    return _punkt(name, OFFEN, f"Gruppe {gruppe}: " + "; ".join(teile) + ".", weg)


def _ews_kontowechsel() -> dict:
    """Wartet ein neu angelegtes EWS-Dienstkonto auf die Exchange-Befehle und
    „Prüfen“ (ews_bereich.ausstehend)? Seit v1.12.96 wird es erst nach
    bestandener Prüfung wirksam — bis dahin ist der Wechsel offen. Nur lokal."""
    import ews_bereich
    name = "Kein Wechsel des EWS-Dienstkontos offen"
    if (entfaellt := _ohne_hybrid(name)) is not None:
        return entfaellt
    neu = ews_bereich.ausstehend()
    if not neu:
        return _punkt(name, ENTFAELLT if not ews_bereich.eingerichtet() else OK,
                      "Kein neues Dienstkonto ausstehend.")
    alt = ews_bereich.wirksames_konto() or "—"
    return _punkt(name, OFFEN,
                  f"Neues Dienstkonto {neu} wartet auf die Exchange-Befehle und „Prüfen“ — bis "
                  f"dahin arbeitet das Gateway mit {alt}.",
                  "Einstellungen → Exchange lokal (EWS) → Zugriffsbereich: Scope und "
                  "Rollenzuweisung in der Exchange-Verwaltungsshell ausführen, danach „Prüfen“ — "
                  "oder „Ausstehendes Konto verwerfen“.")


def _dynamische_gruppen_stand() -> dict:
    """Ist der Entra-Schnappschuss der dynamischen Gruppen frisch (≤ 24 h)? Nur
    lokal: liest Datei und Stand, fragt Graph nicht. Ein alter Stand leert keine
    Gruppe — aber Mitglieder folgen dann nicht mehr den Attributen in Entra."""
    import dynamische_gruppen as dg
    name = "Entra-Schnappschuss der dynamischen Gruppen aktuell"
    if not dg.hat_dynamische():
        return _punkt(name, ENTFAELLT, "Keine dynamische Gruppe angelegt.")
    st = dg.status()
    weg = ("Postfächer → Interne Gruppen → Regel einer dynamischen Gruppe → „Entra neu einlesen“. "
           "Scheitert es, steht der Grund dort; nötig ist das Recht User.Read.All der App.")
    fehler = (st.get("letzter_fehler") or {}).get("text", "")
    alter = dg.alter_s()
    if alter is None:
        return _punkt(name, OFFEN, "Noch kein Entra-Schnappschuss eingelesen — Entra-Bedingungen "
                                   "gelten als leer." + (f" Letzter Versuch: {fehler}" if fehler else ""), weg)
    if alter > dg.ALTER_WARNUNG_S:
        return _punkt(name, OFFEN, f"Schnappschuss vom {st['stand']} ist älter als 24 Stunden — "
                                   "Mitglieder folgen Änderungen in Entra nicht mehr."
                      + (f" Letzter Versuch: {fehler}" if fehler else ""), weg)
    return _punkt(name, OK, f"Schnappschuss vom {st['stand']}: {st['zugeordnet']} Postfächer "
                            f"zugeordnet, {st['ohne_treffer']} ohne Entra-Eintrag.")


def _richtlinien_ruhend() -> dict:
    """Ruht noch eine Postfach-Wahl, die bis v1.12.98 galt? Die Umstellung auf
    zwei Ebenen (settings_store._migrate_v8_to_v9) hat sie in
    RICHTLINIE_RUHT_SEIT_UMSTELLUNG festgehalten: Seither schreibt die
    Gruppen-Regel fest. Offen, solange eine davon noch ruht — erledigt durch
    Verwerfen der Wahl, Anpassen der Regel oder Ausschalten des Schalters.
    Nur lokal."""
    import policies
    import settings_store
    name = "Keine ruhende Postfach-Wahl aus der Umstellung"
    erfasst = settings_store.get("RICHTLINIE_RUHT_SEIT_UMSTELLUNG") or {}
    if not isinstance(erfasst, dict) or not erfasst:
        return _punkt(name, ENTFAELLT, "Die Umstellung hat keine wirksame Wahl gesperrt.")
    mc = settings_store.get("MAILBOX_CONFIG") or {}
    fest = policies.festgeschrieben_schluessel([k for k in erfasst if k in mc], mc)
    offen = []
    for key, f in fest.items():
        ruht = [s for s in policies.ruhende_wahl(mc[key], f) if s in (erfasst.get(key) or [])]
        if ruht:
            offen.append(f"{mc[key].get('primary') or key} ({', '.join(ruht)})")
    if not offen:
        return _punkt(name, OK, "Alle bei der Umstellung gesperrten Wahlen sind geklärt.")
    return _punkt(name, OFFEN,
                  f"{_zahl(len(offen), 'Postfach', 'Postfächer')}: eigene Wahl galt bis v1.12.98, ruht seither "
                  f"unter einer Gruppen-Regel — {', '.join(offen[:5])}"
                  + (" …" if len(offen) > 5 else ""),
                  "Postfächer → Filter „nur ruhende Wahl“: die Wahl verwerfen (Kästchen bzw. "
                  "„Ruhende verwerfen“, dann Speichern) oder die Gruppen-Regel anpassen — oder "
                  "Einstellungen → Signatur → Erweitert: „Benutzerdefinierte Richtlinien "
                  "schreiben fest“ ausschalten.")


def _waechter_code() -> dict:
    """Läuft auf jedem Azure-Wächter der Function-Code, den dieses Gateway
    mitbringt? Bis v1.11.63 wurde er nur beim Anlegen hochgeladen; ein Fix am
    Wächter (etwa das Aufräumen der Exchange-Sitzung nach einem Netzaussetzer,
    v1.11.64) erreichte bestehende Wächter nie. Liest nur das Register."""
    name = "Bypass-Wächter aktuell"
    try:
        import waechter_register
        import watchdog_deploy
        soll = watchdog_deploy.code_stand()
        azure = [w for w in waechter_register.liste() if w.get("kind") == "azure"]
    except Exception as exc:                                 # noqa: BLE001
        return _punkt(name, UNBEKANNT, f"Register nicht lesbar: {exc}")
    if not azure:
        return _punkt(name, ENTFAELLT, "Kein Azure-Wächter eingerichtet.")
    # Auch die Einstellungen, die aus der Konfiguration folgen (Regelname,
    # Health-Adresse) — bis v1.12.42 nur beim Anlegen gesetzt. Ein Wächter ohne
    # gemerkten Stand gilt als veraltet: Was er hat, weiss niemand.
    einst = watchdog_deploy.einstellungen_stand()
    alt = [w.get("name") or w.get("id") for w in azure
           if (w.get("azure") or {}).get("code_stand") != soll
           or (w.get("azure") or {}).get("einstellungen_stand") != einst]
    if not alt:
        return _punkt(name, OK, f"{len(azure)} Azure-Wächter mit aktuellem Code und Einstellungen.")
    return _punkt(name, OFFEN, f"{len(alt)} Azure-Wächter mit älterem Code oder veralteten "
                               f"Einstellungen (Regelname, Health-Adresse; z. B. {alt[0]}).",
                  "Erweitert → Bypass-Wächter → beim Wächter „Code aktualisieren“ "
                  "(braucht den Azure-Login wie beim Anlegen).")


def _key_vault_zugriff() -> dict:
    """Ist ein eingetragener Key Vault auch nutzbar? Eine halbe Einrichtung
    (Adresse gespeichert, Rolle der App fehlt) fiel bis v1.12.4 nirgends auf:
    Jeder Schlüssel blieb lokal, die Postfach-Prüfung nannte das neutral
    „Lokaler Schlüssel — nicht in Key Vault“. Fragt selbst nicht beim Vault
    nach (läuft bei jedem Seitenaufbau), sondern liest den letzten Befund aus
    Signieren, Statusprüfung oder „Verbindung testen“ (keyvault._zugriff_merken)."""
    name = "Key Vault nutzbar"
    try:
        import keyvault
        import settings_store
        url = keyvault.vault_url().strip().rstrip("/") if keyvault.is_configured() else ""
    except Exception as exc:                                 # noqa: BLE001
        return _punkt(name, UNBEKANNT, f"Einstellung nicht lesbar: {exc}")
    if not url:
        return _punkt(name, ENTFAELLT, "Kein Key Vault eingetragen.")
    befund = settings_store.get("KV_ZUGRIFF") or {}
    if befund.get("url") != url:
        return _punkt(name, UNBEKANNT, "Noch nicht geprüft.",
                      "Einrichtung → Azure Key Vault → „Verbindung testen“.")
    if befund.get("zustand") == "verweigert":
        return _punkt(name, OFFEN,
                      "Key Vault eingetragen, aber die App darf ihn nicht lesen (403) — "
                      "S/MIME-Schlüssel bleiben lokal.",
                      "Einrichtung → Azure Key Vault → „Verbindung testen“, dann „Rolle "
                      "sicherstellen (Crypto Officer)“. Wird kein Vault gebraucht: Adresse "
                      "leeren und speichern.")
    return _punkt(name, OK, "Datenzugriff auf den Key Vault vorhanden.")


def _notbetrieb_lokal() -> dict:
    """Notbetrieb eingeschaltet → ist er bereit? Für Abnahme UND Sammelpunkt
    (aufgaben) — daher OHNE Netzabfrage: die Erreichbarkeit des
    Domänencontrollers und den MX prüft „Jetzt prüfen“ auf der Seite
    Notbetrieb (notbetrieb_bereitschaft). Was nur das Netz klären kann
    (UNBEKANNT), bleibt hier außen vor."""
    name = "Notbetrieb-Bereitschaft"
    import notbetrieb
    if not notbetrieb.funktion_aktiv():
        return _punkt(name, OK, "Notbetrieb ist aus — nichts vorzuhalten.")
    import notbetrieb_bereitschaft
    luecken = [q for q in notbetrieb_bereitschaft.pruefen(mit_netz=False)["punkte"] if q["zustand"] == OFFEN]
    if not luecken:
        return _punkt(name, OK, "Kopie, Rechte, Profile, Kontakte, Versand und Zertifikat bereit "
                      "(Domänencontroller und MX: Notbetrieb → „Jetzt prüfen“).")
    return _punkt(name, OFFEN, "; ".join(f"{q['name']}: {q['befund']}" for q in luecken)[:300],
                  luecken[0].get("zu_tun") or "Notbetrieb → Bereitschaft.")


def _teams_katalog_signatur() -> dict:
    """Self-Service-App im Teams-Katalog so aktuell wie das Paket? Nur der
    gemerkte Stand (teams_katalog.pruefung), kein Netz."""
    import teams_katalog
    return teams_katalog.pruefung("signatur")


def _teams_katalog_notfall() -> dict:
    """Wie _teams_katalog_signatur, für die Teams-App „Notfall-Mail“."""
    import teams_katalog
    return teams_katalog.pruefung("notfall")


PRUEFUNGEN = (_aussenadresse, _tls, _anmeldung, _naa, _exchange,
              _postfaecher, _signaturvorlage, _vorlage_standard_doppelt, _rueckweg, _regel_empfaenger, _regel_kennung,
              _regel_auto_ausnahme, _implicit_anmeldung,
              _addin_rollen, _addin_fassung, _addin_knopf_fassung, _ews_bereich, _ews_gruppe, _ews_kontowechsel, _dynamische_gruppen_stand, _richtlinien_ruhend, _waechter_code, _key_vault_zugriff, _benachrichtigung, _anmeldenachweis,
              _smime_schluessel, _vertragsbedingungen, _erneuerung_unentschieden,
              _ablauf_ohne_erneuerung,
              _admin_passwort, _fair_use, _wartende_zertifikate, _tls_rueckweg, _direktversand_probe, _dmarc_berichte,
              _smime_regeln_stand,
              _bestand_durchsicht, _tenant_herkunft, _quell_ip_liste, _exo_erkennung,
              _csp_berichte, _notbetrieb_lokal, _teams_katalog_signatur, _teams_katalog_notfall)


def _ein_punkt(pruefung) -> dict:
    try:
        return pruefung()
    except Exception as exc:                          # noqa: BLE001
        log.warning("Abnahme: %s fehlgeschlagen: %s", pruefung.__name__, exc)
        return _punkt(
            pruefung.__name__.lstrip("_").replace("_", " ").capitalize(),
            UNBEKANNT, str(exc)[:150],
            "Die Prüfung selbst ist gescheitert — das ist kein Freibrief.")


def bericht() -> dict:
    """Alle Punkte prüfen. Ein Fehler in einem Punkt kippt nicht den Rest.

    Nebeneinander (höchstens 8 zugleich), Reihenfolge wie in PRUEFUNGEN: Zwei
    Punkte bauen je eine PowerShell-Sitzung zu Exchange auf (~15 s); nacheinander
    dauerte die Abnahme auf dem Raspi 41 s (gemessen 20.11.2026)."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="abnahme") as pool:
        punkte = list(pool.map(_ein_punkt, PRUEFUNGEN))

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
