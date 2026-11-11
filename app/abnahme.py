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
        return _punkt("Anmeldung möglich", OK, f"{len(admins)} Entra-Konto(en) zugelassen")
    return _punkt(
        "Anmeldung möglich", OFFEN, "Kein Entra-Konto zugelassen.",
        "Mindestens ein Konto eintragen. Der örtliche Notfallzugang bleibt "
        "bestehen, ist aber kein Dauerzustand.")


# Rollen aus dem Token lesen: EINE Quelle in berechtigungen.py (auch für den
# Status im Einrichtungsassistenten und auf der Abwesenheits-Seite).
def _token_rollen(token: str) -> set[str] | None:
    import berechtigungen
    return berechtigungen.token_rollen(token)


def _exo_rollen() -> set[str] | None:
    import berechtigungen
    return berechtigungen.exo_rollen()


def _exchange() -> dict:
    import graph_client
    # Frisch, nicht aus dem Cache — sonst fehlt eine eben erteilte Rolle bis zum
    # Ablauf des Tokens (berechtigungen.graph_rollen, gleicher Grund).
    token = graph_client.frisches_app_token()
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
        exo_hinweis = "Exchange.ManageAsApp — kein Exchange-Token erhalten."

    if fehlt:
        return _punkt(
            "Exchange erreichbar", OFFEN,
            f"Zugangsdaten vorhanden, aber Berechtigung fehlt: {', '.join(sorted(set(fehlt)))}.",
            "Im Entra Admin Center die Administratorzustimmung für die fehlenden "
            "Anwendungsberechtigungen erteilen.")
    return _punkt(
        "Exchange erreichbar", OK,
        "Zugangsdaten, Graph- und Exchange-Berechtigungen vorhanden",
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
    anzahl = f"{len(roh)} Schlüsseldatei(en) liegen im Klartext."
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
        offen.append("Weiterleitungsziel(e) " + ", ".join(uebernommen)
                     + " laufen noch ohne Zertifikatsprüfung, wie vor dem Update")
    if not offen:
        return _punkt(name, OK, "Ausgehende TLS-Verbindungen prüfen das Zertifikat, "
                      "oder die Ausnahme ist bewusst gewählt.")
    return _punkt(name, OFFEN, "; ".join(offen) + ".",
                  "SMTP-Relay → Weiterleitungsziele: je Ziel „Zertifikat prüfen“ wählen "
                  "(an, wenn der Host ein Name mit gültigem Zertifikat ist) und speichern. "
                  "Smarthost: als Namen *.mail.protection.outlook.com eintragen.")


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
                  f"{len(wartend)} Empfängerzertifikat(e) warten auf eine Entscheidung; "
                  "an diese Partner wird bis dahin nicht verschlüsselt.",
                  "S/MIME → „Zertifikate, die auf Freigabe warten“: je Zeile den Grund "
                  "prüfen, freigeben oder verwerfen.")


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
                  f"{len(offen)} Empfängerzertifikat(e) im Bestand führen auf keinen bekannten "
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
    return _punkt(name, OK, f"{n} Exchange-Adressbereiche geladen.")


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
                      f"{len(falsch)} Regel(n) nehmen nicht genau die eigene Kennung "
                      f"{p['soll']} aus: {', '.join(falsch)}. Post anderer Gateways "
                      "umgeht dann die eingehenden S/MIME-Regeln.",
                      "Einstellungen → Signatur → Erweiterte Einstellungen → „Regeln prüfen“ → „Regeln angleichen“.")
    return _punkt(name, OK, f"{len(p['regeln'])} Regel(n) mit Kennung {p['soll']}")


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


PRUEFUNGEN = (_aussenadresse, _tls, _anmeldung, _naa, _exchange,
              _postfaecher, _signaturvorlage, _rueckweg, _regel_empfaenger, _regel_kennung,
              _addin_rollen, _benachrichtigung, _anmeldenachweis,
              _smime_schluessel, _vertragsbedingungen,
              _admin_passwort, _fair_use, _wartende_zertifikate, _tls_rueckweg,
              _bestand_durchsicht, _tenant_herkunft, _quell_ip_liste)


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
