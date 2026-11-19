"""Anbindung an eine lokale Exchange-Organisation (2016/2019/SE) über EWS — optional.

WOFÜR
-----
Hybrid-Nutzer (MAILBOX_CONFIG-Eintrag mit `hybrid: True`, siehe exo_mailboxes)
haben ihr Postfach in der lokalen Organisation. Graph erreicht es nicht. Für
die Abwesenheit und den S/MIME-Zertifikatsbezug (Mail der CA lesen) spricht
das Gateway deshalb mit der lokalen Exchange per EWS — und NUR für diese
Postfächer. Die Weiche dafür steht an EINER Stelle: `zustaendig()` (genutzt von
`abwesenheit._ueber_ews()` und `postfach_weg()`). Ein EXO-Postfach geht nie
hierher.

POSTEINGANG (seit v1.12.62): `posteingang_lesen` = FindItem (Kennungen, jüngste
zuerst, Zeit- und Betreff-Filter am Server) + EIN GetItem (Betreff, Absender,
Message-ID, Text). `posteingang_als_graph` liefert die Form des Graph-Abrufs
`mailFolders/inbox/messages`; die Auswertung bleibt bei den Aufrufern
(acme_state, hub_orders). Gesendet wird über EWS nichts — Antworten eines
Hybrid-Nutzers gehen wie seine übrige Post über den Smarthost (reinject).
EINZIGE Ausnahme (seit v1.12.67): die Regelprobe (`regelprobe_senden`, nur aus
regel_probe) — sie MUSS in der lokalen Organisation entstehen, sonst prüfte sie
nicht den Weg, den die Post eines Hybrid-Nutzers nimmt. Ebenso nur für die
Regelprobe (seit v1.12.72): deren Unzustellbarkeitsmeldung lesen und per
DeleteItem (SoftDelete) entfernen (`regelprobe_ndr_suchen`/`_loeschen`).

BEANTWORTETE MAIL (seit v1.12.69): `bezug_per_kennung` für das Verfassen-Add-in
— FindItem nach der Message-ID in Posteingang + Gesendete Elemente, GetItem mit
References/In-Reply-To, Form wie Graph (Auswertung: graph_client.bezug_aus_treffern).

OHNE KONFIGURATION
------------------
`konfiguriert()` ist falsch, solange URL, Konto oder Kennwort fehlen. Dann ruft
niemand dieses Modul auf, und `requests_ntlm` wird nie geladen (Import erst in
`_senden`). Das Verhalten ist exakt wie vor der Anbindung.

PROTOKOLL (live gemessen gegen Exchange SE 15.2)
------------------------------------------------
* Anmeldung NTLM (Basic ist auf dem EWS-Verzeichnis meist aus, OAuth/HMA nicht
  vorausgesetzt). Das Dienstkonto braucht die RBAC-Rolle
  ApplicationImpersonation, empfohlen eingeschränkt per Management Scope.
* SOAP-Kopf: `RequestServerVersion Exchange2016` + `ExchangeImpersonation`
  mit der Zieladresse, HTTP-Kopf `X-AnchorMailbox`.
* Postfach ausserhalb des Scopes → HTTP 500, SOAP-Fault
  `ErrorImpersonateUserDenied`.

⚠️ XML NUR über xml.etree bauen und lesen — nie per Regex oder
Zeichenkettenverkettung. Ein Probelauf, der Antworttexte per Regex aus dem XML
schnitt und wieder einsetzte, schrieb danach wörtlich „<Message/>“ in die
Abwesenheitsnotiz. `tests/test_ews_onprem.py` prüft Sonderzeichen und den
Rundlauf lesen → setzen → lesen.

KALENDER (seit v1.12.61): `kalender_lesen` liest per FindItem/CalendarView die
Termine für Kalender-Automatik und Ankündigung; `kalender_als_graph` übersetzt
sie in die Graph-`calendarView`-Form — die Auswertung bleibt in abwesenheit.

EIGENE CA (`EWS_ONPREM_CA`, seit v1.12.61): PEM einer internen CA, ZUSÄTZLICH
zu certifi (`_tls_kontext`). Ohne Eintrag bleibt die Sitzung, wie requests sie
anlegt. Prüfung beim Speichern: `ca_pruefen`.

Diese Aufrufe sind blockierend (requests). Aufrufer im Event-Loop nutzen
`asyncio.to_thread`. Nie im SMTP-Pfad aufrufen.
"""
from __future__ import annotations

import contextvars
import ipaddress
import logging
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import settings_store

log = logging.getLogger("ews_onprem")

KEY_URL = "EWS_ONPREM_URL"
KEY_KONTO = "EWS_ONPREM_KONTO"
KEY_KENNWORT = "EWS_ONPREM_KENNWORT"
KEY_VERBINDEN = "EWS_ONPREM_VERBINDEN"
KEY_CA = "EWS_ONPREM_CA"

# Eigene CA: Obergrenzen gegen versehentlich eingefügte Riesendateien.
CA_MAX_ZEICHEN = 200_000
CA_MAX_ANZAHL = 50

ZEITLIMIT_S = 20
SERVER_VERSION = "Exchange2016"     # kleinster gemeinsamer Nenner für 2016/2019/SE

NS_SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
NS_T = "http://schemas.microsoft.com/exchange/services/2006/types"
NS_M = "http://schemas.microsoft.com/exchange/services/2006/messages"
NS_E = "http://schemas.microsoft.com/exchange/services/2006/errors"

# EWS ↔ Graph (automaticRepliesSetting). Die Abwesenheit des Gateways rechnet
# durchgehend in der Graph-Form; diese Tabellen sind die einzige Übersetzung.
ZUSTAND_ZU_GRAPH = {"Disabled": "disabled", "Enabled": "alwaysEnabled", "Scheduled": "scheduled"}
ZUSTAND_AUS_GRAPH = {v: k for k, v in ZUSTAND_ZU_GRAPH.items()}
PUBLIKUM_ZU_GRAPH = {"None": "none", "Known": "contactsOnly", "All": "all"}
PUBLIKUM_AUS_GRAPH = {v: k for k, v in PUBLIKUM_ZU_GRAPH.items()}

KEIN_ZUGRIFF_CODES = frozenset({"ErrorImpersonateUserDenied", "ErrorImpersonationDenied"})

_KLARTEXT = {
    "ErrorImpersonateUserDenied":
        "Dienstkonto darf dieses Postfach nicht (Management Scope/ApplicationImpersonation)",
    "ErrorImpersonationDenied":
        "Dienstkonto hat keine Rolle ApplicationImpersonation",
    "ErrorNonExistentMailbox":
        "Postfach in der lokalen Exchange-Organisation nicht gefunden",
    "ErrorMailboxMoveInProgress":
        "Postfach wird gerade verschoben",
    "ErrorServerBusy":
        "Exchange ist ausgelastet — später erneut",
}


class EwsFehler(Exception):
    """Ein EWS-Aufruf ist gescheitert. `code` ist maschinenlesbar (EWS-Code
    oder eine eigene Kennung: anmeldung, tls, verbindung, zeitlimit, http,
    antwort, konfiguration), `detail` ein Klartext für Oberfläche und Protokoll."""

    def __init__(self, code: str, detail: str, ausfall: bool = False):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        # True: betrifft die ANBINDUNG, nicht dieses Postfach (siehe _AUSFALL_CODES).
        self.ausfall = ausfall or code in _AUSFALL_CODES

    @property
    def kein_zugriff(self) -> bool:
        return self.code in KEIN_ZUGRIFF_CODES


# ── Ausfallsperre ─────────────────────────────────────────────────────────────
# Anlass (18.11.2026, Skalierung auf hunderte Hybrid-Nutzer): Gesundheitsprüfung,
# Abwesenheit und OWA-Abgleich rufen EWS je Postfach. Ist die lokale Exchange
# nicht erreichbar, wartete JEDER Aufruf bis zum Fehler — gemessen im Lab 3,1 s
# bei abgelehnter Verbindung, bis ZEITLIMIT_S (20 s) bei verworfenen Paketen.
# 500 Postfächer × 2 Aufrufe ergaben so 50 Minuten bis über fünf Stunden, in
# denen der Scheduler-Faden (auch die Wiederholung zurückgehaltener Post) stand.
#
# Nach einem Fehler, der die ANBINDUNG betrifft (nicht ein Postfach), schlagen
# weitere Aufrufe AUSFALL_PAUSE_S lang sofort mit demselben Code fehl — ohne
# Netz. Danach geht wieder ein echter Aufruf hinaus; gelingt einer, ist die
# Sperre weg. Postfachbezogene Fehler (Management Scope, Postfach fehlt) und
# Lesezeitüberschreitungen (ein grosses Postfach) lösen sie nicht aus. Der Knopf
# „Verbindung testen“ geht immer hinaus (`verbindung_testen(…, sperre_umgehen=True)`).
AUSFALL_PAUSE_S = 60
_AUSFALL_CODES = frozenset({"anmeldung", "tls", "verbindung", "ErrorServerBusy"})
_ausfall_lock = threading.Lock()
_ausfall: dict = {}              # {"bis", "seit", "code", "detail", "uebersprungen"}
_sperre_umgehen = contextvars.ContextVar("ews_sperre_umgehen", default=False)
# Zugangsdaten für EINE Probe (`verbindung_testen(zugang=…)`), ohne die
# gespeicherten zu ändern — ews_bereich prüft so ein ausstehendes Dienstkonto,
# bevor es wirksam wird. Gelesen nur von `_zugang()`.
_zugang_probe = contextvars.ContextVar("ews_zugang_probe", default=None)


def _konfig_marke() -> str:
    """Prüfsumme der Anbindungsfelder. Ändert der Betreiber sie (neue CA, neues
    Kennwort), gilt eine Sperre aus der alten Einstellung nicht mehr."""
    import hashlib
    roh = "\x00".join(_wert(k) for k in (KEY_URL, KEY_KONTO, KEY_KENNWORT, KEY_VERBINDEN, KEY_CA))
    return hashlib.sha256(roh.encode("utf-8")).hexdigest()


def _ausfall_pruefen() -> None:
    """Vor jedem Aufruf: Steht die Sperre, sofort EwsFehler (ohne Netz)."""
    if _sperre_umgehen.get():
        return
    marke = _konfig_marke()
    with _ausfall_lock:
        if not _ausfall or time.monotonic() >= _ausfall["bis"] or _ausfall["marke"] != marke:
            return
        _ausfall["uebersprungen"] += 1
        rest = _ausfall["bis"] - time.monotonic()
        code, detail = _ausfall["code"], _ausfall["detail"]
    raise EwsFehler(code, f"{detail} — Aufruf übersprungen, Exchange lokal war eben nicht "
                          f"erreichbar (nächster Versuch in {rest:.0f} s)", ausfall=True)


def _ausfall_merken(exc: "EwsFehler") -> None:
    if _zugang_probe.get() is not None:
        # Probe mit fremden Zugangsdaten: Ihr Scheitern (etwa „anmeldung“ eines
        # noch nicht berechtigten Kontos) darf die Anbindung mit dem GESPEICHERTEN
        # Konto nicht sperren.
        return
    with _ausfall_lock:
        neu = not _ausfall
        jetzt = time.monotonic()
        _ausfall.update({"bis": jetzt + AUSFALL_PAUSE_S, "code": exc.code,
                         "marke": _konfig_marke(),
                         "detail": exc.detail, "seit": _ausfall.get("seit", jetzt),
                         "uebersprungen": _ausfall.get("uebersprungen", 0)})
    if neu:
        log.warning("EWS: Anbindung gestört (%s) — weitere Aufrufe %d s lang ohne Netz "
                    "übersprungen", exc.code, AUSFALL_PAUSE_S)


def _ausfall_vorbei() -> None:
    if _zugang_probe.get() is not None:
        return          # eine Probe mit anderen Zugangsdaten belegt nichts über die gespeicherten
    with _ausfall_lock:
        if not _ausfall:
            return
        dauer = time.monotonic() - _ausfall["seit"]
        n = _ausfall["uebersprungen"]
        _ausfall.clear()
    log.info("EWS: Anbindung wieder erreichbar nach %.0f s — %d Aufrufe in der Zeit "
             "übersprungen", dauer, n)


def ausfall_stand() -> dict:
    """Für Anzeige und Tests: {} oder {code, detail, uebersprungen, rest_s}."""
    with _ausfall_lock:
        if not _ausfall:
            return {}
        return {"code": _ausfall["code"], "detail": _ausfall["detail"],
                "uebersprungen": _ausfall["uebersprungen"],
                "rest_s": max(0.0, _ausfall["bis"] - time.monotonic())}


# ── Konfiguration ─────────────────────────────────────────────────────────────

def _wert(key: str) -> str:
    v = settings_store.get(key)
    return v.strip() if isinstance(v, str) else ""


def konfiguriert() -> bool:
    """URL, Konto und Kennwort gesetzt? Sonst ist die Anbindung aus — und
    niemand ruft dieses Modul auf."""
    konto, kennwort = _zugang()
    return bool(_wert(KEY_URL) and konto and kennwort)


def _zugang() -> tuple[str, str]:
    """(Konto, Kennwort) für die NTLM-Anmeldung — DIE eine Quelle: die
    gespeicherten, oder während `verbindung_testen(zugang=…)` die übergebenen."""
    probe = _zugang_probe.get()
    if probe is not None:
        return probe
    return _wert(KEY_KONTO), _wert(KEY_KENNWORT)


def url_pruefen(url: str) -> str:
    """Fehlertext oder "" — nur https mit Hostnamen."""
    teile = urlsplit((url or "").strip())
    if teile.scheme != "https" or not teile.hostname:
        return "Die EWS-Adresse muss mit https:// beginnen, z. B. https://exchange.firma.at/EWS/Exchange.asmx"
    return ""


def verbinden_pruefen(adresse: str) -> str:
    """Fehlertext oder "" — leer oder eine IP-Adresse."""
    a = (adresse or "").strip()
    if not a:
        return ""
    try:
        ipaddress.ip_address(a)
    except ValueError:
        return "„Verbinden mit“ erwartet eine IP-Adresse (z. B. 10.99.0.20) oder bleibt leer"
    return ""


def ca_pruefen(text: str, jetzt: datetime | None = None) -> tuple[str, str]:
    """Eigenes CA-Zertifikat (PEM, eines oder mehrere) prüfen.

    (normalisiertes PEM, Fehlertext). Leer → ("", ""). Zurück kommen nur die
    Zertifikate selbst, neu kodiert — Begleittext (etwa „Bag Attributes“ aus
    openssl) fällt weg. Zugelassen ist, was OpenSSL als Vertrauensanker nimmt:
    ein CA-Zertifikat (BasicConstraints CA) oder ein selbstsigniertes Zertifikat
    (etwa das Standardzertifikat einer frisch installierten Exchange). Ein
    Serverzertifikat, das eine CA ausgestellt hat, hilft als Anker nicht — dafür
    nennt die Meldung die ausstellende CA. Abgelaufene Zertifikate scheitern bei
    der Prüfung ohnehin und werden gleich hier abgelehnt."""
    from cryptography import x509
    roh = (text or "").strip()
    if not roh:
        return "", ""
    if len(roh) > CA_MAX_ZEICHEN:
        return "", "Das CA-Zertifikat ist zu groß — bitte nur die Zertifikate der CA eintragen"
    if "PRIVATE KEY" in roh:
        return "", ("Das ist ein privater Schlüssel. Hier gehört nur das öffentliche Zertifikat "
                    "der CA hinein (-----BEGIN CERTIFICATE-----)")
    if "-----BEGIN CERTIFICATE-----" not in roh:
        return "", ("Kein PEM-Zertifikat erkannt — erwartet wird Text ab "
                    "-----BEGIN CERTIFICATE----- (Base64, nicht DER)")
    try:
        zerts = x509.load_pem_x509_certificates(roh.encode("utf-8", "replace"))
    except ValueError:
        return "", "Das CA-Zertifikat ist nicht lesbar (PEM beschädigt oder unvollständig)"
    if len(zerts) > CA_MAX_ANZAHL:
        return "", f"Höchstens {CA_MAX_ANZAHL} Zertifikate"
    jetzt = jetzt or datetime.now(timezone.utc)
    from cryptography.hazmat.primitives.serialization import Encoding
    teile = []
    for z in zerts:
        name = z.subject.rfc4514_string() or "(ohne Namen)"
        if z.not_valid_after_utc < jetzt:
            return "", f"Zertifikat {name} ist am {z.not_valid_after_utc:%d.%m.%Y} abgelaufen"
        if z.not_valid_before_utc > jetzt:
            return "", f"Zertifikat {name} gilt erst ab {z.not_valid_before_utc:%d.%m.%Y}"
        try:
            ca = z.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        except x509.ExtensionNotFound:
            ca = False
        if not ca and z.issuer != z.subject:
            return "", (f"{name} ist kein CA-Zertifikat, sondern ein von "
                        f"{z.issuer.rfc4514_string()} ausgestelltes Zertifikat — trage das "
                        "Zertifikat dieser CA ein")
        teile.append(z.public_bytes(Encoding.PEM).decode("ascii"))
    return "".join(teile), ""


def ca_hinterlegt() -> int:
    """Wie viele eigene CA-Zertifikate sind hinterlegt (0 = nur certifi)?"""
    return _wert(KEY_CA).count("-----BEGIN CERTIFICATE-----")


# ── XML bauen ─────────────────────────────────────────────────────────────────

def _t(name: str) -> str:
    return f"{{{NS_T}}}{name}"


def _m(name: str) -> str:
    return f"{{{NS_M}}}{name}"


def _umschlag(ziel: str, inhalt: ET.Element) -> bytes:
    """SOAP-Umschlag mit Impersonation auf `ziel`."""
    env = ET.Element(f"{{{NS_SOAP}}}Envelope")
    kopf = ET.SubElement(env, f"{{{NS_SOAP}}}Header")
    ET.SubElement(kopf, _t("RequestServerVersion"), {"Version": SERVER_VERSION})
    imp = ET.SubElement(kopf, _t("ExchangeImpersonation"))
    sid = ET.SubElement(imp, _t("ConnectingSID"))
    ET.SubElement(sid, _t("PrimarySmtpAddress")).text = ziel
    rumpf = ET.SubElement(env, f"{{{NS_SOAP}}}Body")
    rumpf.append(inhalt)
    return ET.tostring(env, encoding="utf-8", xml_declaration=True)


def _postfach(ziel: str) -> ET.Element:
    mb = ET.Element(_t("Mailbox"))
    ET.SubElement(mb, _t("Address")).text = ziel
    return mb


def anfrage_oof_lesen(ziel: str) -> bytes:
    req = ET.Element(_m("GetUserOofSettingsRequest"))
    req.append(_postfach(ziel))
    return _umschlag(ziel, req)


def _zeit_ews(wert: str) -> str:
    """ISO-Zeitpunkt → EWS xs:dateTime in UTC („…Z“). Ohne Zone gilt UTC."""
    d = datetime.fromisoformat(wert.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def anfrage_oof_setzen(ziel: str, state: str, extern_publikum: str, intern: str,
                       extern: str, start: str = "", ende: str = "") -> bytes:
    """SetUserOofSettings. EWS ersetzt die Einstellung als Ganzes — der Aufrufer
    übergibt deshalb ALLE Felder (abwesenheit führt sie aus dem gelesenen Stand
    zusammen). `start`/`ende` sind ISO-Zeitpunkte; fehlt einer, entfällt die Dauer."""
    if state not in ZUSTAND_ZU_GRAPH:
        raise ValueError(f"Unbekannter Abwesenheitszustand: {state!r}")
    if extern_publikum not in PUBLIKUM_ZU_GRAPH:
        raise ValueError(f"Unbekannter Empfängerkreis: {extern_publikum!r}")
    req = ET.Element(_m("SetUserOofSettingsRequest"))
    req.append(_postfach(ziel))
    s = ET.SubElement(req, _t("UserOofSettings"))
    ET.SubElement(s, _t("OofState")).text = state
    ET.SubElement(s, _t("ExternalAudience")).text = extern_publikum
    if start and ende:
        dauer = ET.SubElement(s, _t("Duration"))
        ET.SubElement(dauer, _t("StartTime")).text = _zeit_ews(start)
        ET.SubElement(dauer, _t("EndTime")).text = _zeit_ews(ende)
    ET.SubElement(ET.SubElement(s, _t("InternalReply")), _t("Message")).text = intern or ""
    ET.SubElement(ET.SubElement(s, _t("ExternalReply")), _t("Message")).text = extern or ""
    return _umschlag(ziel, req)


def anfrage_kalender(ziel: str, von: datetime, bis: datetime) -> bytes:
    """FindItem mit CalendarView auf den Kalender von `ziel` — Serientermine kommen
    als einzelne Vorkommen, wie bei Graph `calendarView`. Nur die Felder, die die
    Abwesenheit braucht (wie `$select` beim Graph-Weg): kein Betreff, kein Text.
    Kein MaxEntriesReturned: Bei Überschreitung bräche EWS die ganze Abfrage ab."""
    req = ET.Element(_m("FindItem"), {"Traversal": "Shallow"})
    form = ET.SubElement(req, _m("ItemShape"))
    ET.SubElement(form, _t("BaseShape")).text = "IdOnly"
    felder = ET.SubElement(form, _t("AdditionalProperties"))
    for f in ("calendar:Start", "calendar:End", "calendar:LegacyFreeBusyStatus",
              "calendar:IsAllDayEvent", "item:Sensitivity"):
        ET.SubElement(felder, _t("FieldURI"), {"FieldURI": f})
    ET.SubElement(req, _m("CalendarView"), {"StartDate": _zeit_ews(von.isoformat()),
                                             "EndDate": _zeit_ews(bis.isoformat())})
    ordner = ET.SubElement(ET.SubElement(req, _m("ParentFolderIds")), _t("DistinguishedFolderId"),
                           {"Id": "calendar"})
    ET.SubElement(ET.SubElement(ordner, _t("Mailbox")), _t("EmailAddress")).text = ziel
    return _umschlag(ziel, req)


POSTEINGANG_MAX = 50        # wie der größte Graph-Abruf ($top=50, acme_state)


def _feld(eltern: ET.Element, uri: str) -> None:
    ET.SubElement(eltern, _t("FieldURI"), {"FieldURI": uri})


def _vergleich(art: str, uri: str, wert: str, **attribute) -> ET.Element:
    el = ET.Element(_t(art), attribute)
    _feld(el, uri)
    if art == "Contains":
        ET.SubElement(el, _t("Constant"), {"Value": wert})
    else:
        ET.SubElement(ET.SubElement(el, _t("FieldURIOrConstant")), _t("Constant"), {"Value": wert})
    return el


def anfrage_posteingang_suchen(ziel: str, ab: datetime | None = None,
                               betreff_enthaelt: str = "", anzahl: int = 25) -> bytes:
    """FindItem im Posteingang von `ziel`: nur Kennungen, jüngste zuerst.

    Entspricht dem Graph-Abruf `mailFolders/inbox/messages?$orderby=
    receivedDateTime desc&$top=…&$filter=receivedDateTime ge …`. Zusätzlich
    filtert der Server auf einen Betreff-Teil (ohne Rücksicht auf Groß-/
    Kleinschreibung) — die Aufrufer prüfen den Betreff danach selbst noch
    einmal; der Filter spart nur, unbeteiligte Mails abzuholen. Die Werte gehen
    als Attribut über xml.etree hinein, nie per Zeichenkettenverkettung."""
    anzahl = max(1, min(int(anzahl), POSTEINGANG_MAX))
    req = ET.Element(_m("FindItem"), {"Traversal": "Shallow"})
    ET.SubElement(ET.SubElement(req, _m("ItemShape")), _t("BaseShape")).text = "IdOnly"
    ET.SubElement(req, _m("IndexedPageItemView"), {"MaxEntriesReturned": str(anzahl),
                                                   "Offset": "0", "BasePoint": "Beginning"})
    bedingungen = []
    if ab is not None:
        bedingungen.append(_vergleich("IsGreaterThanOrEqualTo", "item:DateTimeReceived",
                                      _zeit_ews(ab.isoformat())))
    if betreff_enthaelt:
        bedingungen.append(_vergleich("Contains", "item:Subject", betreff_enthaelt,
                                      ContainmentMode="Substring",
                                      ContainmentComparison="IgnoreCase"))
    if bedingungen:
        r = ET.SubElement(req, _m("Restriction"))
        if len(bedingungen) == 1:
            r.append(bedingungen[0])
        else:
            und = ET.SubElement(r, _t("And"))
            for b in bedingungen:
                und.append(b)
    ordnung = ET.SubElement(ET.SubElement(req, _m("SortOrder")), _t("FieldOrder"),
                            {"Order": "Descending"})
    _feld(ordnung, "item:DateTimeReceived")
    ordner = ET.SubElement(ET.SubElement(req, _m("ParentFolderIds")), _t("DistinguishedFolderId"),
                           {"Id": "inbox"})
    ET.SubElement(ET.SubElement(ordner, _t("Mailbox")), _t("EmailAddress")).text = ziel
    return _umschlag(ziel, req)


# Zusätzlich für die Kette einer beantworteten Mail (Verfassen-Add-in): Entwurf
# ja/nein und die Kopfzeilen References/In-Reply-To — dieselben Angaben, die
# Graph über `isDraft` und `internetMessageHeaders` liefert. ⚠️ `item:InReplyTo`,
# nicht `message:…` — im Lab (Exchange SE, 18.11.2026) gemessen: „message:InReplyTo“
# lässt das ganze GetItem mit ErrorInvalidRequest scheitern; References dagegen
# heißt `message:References`.
KETTE_FELDER = ("item:IsDraft", "message:References", "item:InReplyTo")


def anfrage_nachrichten_holen(ziel: str, kennungen: list[tuple[str, str]],
                              kette: bool = False) -> bytes:
    """GetItem für die gefundenen Kennungen: Betreff, Absender, Empfangszeit,
    Message-ID und Text (`BodyType Best` — HTML bleibt HTML, Text bleibt Text,
    wie Graph `body` ohne Prefer-Kopf). `kette`: dazu KETTE_FELDER."""
    req = ET.Element(_m("GetItem"))
    form = ET.SubElement(req, _m("ItemShape"))
    ET.SubElement(form, _t("BaseShape")).text = "IdOnly"
    ET.SubElement(form, _t("BodyType")).text = "Best"
    felder = ET.SubElement(form, _t("AdditionalProperties"))
    for f in ("item:Subject", "message:From", "item:DateTimeReceived",
              "message:InternetMessageId", "item:Body") + (KETTE_FELDER if kette else ()):
        _feld(felder, f)
    ids = ET.SubElement(req, _m("ItemIds"))
    for kennung, aenderung in kennungen:
        attr = {"Id": kennung}
        if aenderung:
            attr["ChangeKey"] = aenderung
        ET.SubElement(ids, _t("ItemId"), attr)
    return _umschlag(ziel, req)


# ── XML lesen ─────────────────────────────────────────────────────────────────

def _parse(roh: bytes | str) -> ET.Element:
    daten = roh.encode("utf-8") if isinstance(roh, str) else (roh or b"")
    if b"<!DOCTYPE" in daten[:2048].upper():
        # SOAP kennt keine DTD — eine Antwort mit DTD ist keine EWS-Antwort.
        raise EwsFehler("antwort", "Antwort enthält eine DTD — keine EWS-Antwort")
    try:
        return ET.fromstring(daten)
    except ET.ParseError as exc:
        raise EwsFehler("antwort", f"Antwort ist kein lesbares XML ({exc})") from None


def _text(el: ET.Element | None) -> str:
    return (el.text or "") if el is not None else ""


def _klartext(code: str, meldung: str) -> str:
    return _KLARTEXT.get(code) or (f"EWS meldet {code}" + (f": {meldung}" if meldung else ""))


def _fault(root: ET.Element) -> EwsFehler | None:
    f = root.find(f".//{{{NS_SOAP}}}Fault")
    if f is None:
        return None
    code = _text(f.find(f".//{{{NS_E}}}ResponseCode")).strip()
    if not code:
        roh = _text(f.find("faultcode")).strip()
        code = roh.split(":", 1)[-1] if roh else "Fault"
    meldung = (_text(f.find(f".//{{{NS_E}}}Message")) or _text(f.find("faultstring"))).strip()
    return EwsFehler(code, _klartext(code, meldung))


def _antwort_ok(rm: ET.Element) -> bool:
    return (rm.get("ResponseClass") == "Success"
            and _text(rm.find(_m("ResponseCode"))).strip() == "NoError")


def antwort_pruefen(status: int, roh: bytes | str, teilweise: bool = False) -> ET.Element:
    """HTTP-Status + Rumpf → Wurzel des Antwort-XML, oder EwsFehler.

    `teilweise` (GetItem mit mehreren Kennungen): Je Element kommt eine eigene
    ResponseMessage. Es genügt, dass eine davon gelang — ein Element, das
    zwischen Suche und Abruf gelöscht wurde, darf die übrigen nicht mitnehmen.
    Wer die Elemente liest, nimmt nur die aus erfolgreichen Antworten
    (`nachrichten_aus_antwort`)."""
    if status == 401:
        raise EwsFehler("anmeldung", "Anmeldung abgelehnt (401) — Konto/Kennwort prüfen; "
                                     "das EWS-Verzeichnis muss NTLM erlauben")
    if status == 403:
        raise EwsFehler("http", "Zugriff verweigert (403)")
    root: ET.Element | None = None
    try:
        root = _parse(roh)
    except EwsFehler:
        if status != 200:
            raise EwsFehler("http", f"Exchange antwortet HTTP {status}") from None
        raise
    fault = _fault(root)
    if fault:
        raise fault
    if status != 200:
        raise EwsFehler("http", f"Exchange antwortet HTTP {status}")
    # GetUserOofSettings antwortet mit <ResponseMessage>, FindItem & Co. mit
    # <FindItemResponseMessage> (in <ResponseMessages>) — das erste davon zählt.
    rms = [el for el in root.iter() if el.tag.startswith(f"{{{NS_M}}}")
           and el.tag.endswith("ResponseMessage")]
    if not rms:
        raise EwsFehler("antwort", "Antwort ohne ResponseMessage")
    if teilweise and any(_antwort_ok(r) for r in rms):
        return root
    rm = rms[0]
    code = _text(rm.find(_m("ResponseCode"))).strip()
    if not _antwort_ok(rm):
        code = code or "Error"
        raise EwsFehler(code, _klartext(code, _text(rm.find(_m("MessageText"))).strip()))
    return root


def oof_aus_antwort(root: ET.Element) -> dict:
    """GetUserOofSettingsResponse → {state, extern_publikum, intern, extern, start, ende}."""
    s = root.find(f".//{_t('OofSettings')}")
    if s is None:
        raise EwsFehler("antwort", "Antwort ohne OofSettings")
    return {
        "state": _text(s.find(_t("OofState"))).strip() or "Disabled",
        "extern_publikum": _text(s.find(_t("ExternalAudience"))).strip() or "All",
        "intern": _text(s.find(f"{_t('InternalReply')}/{_t('Message')}")),
        "extern": _text(s.find(f"{_t('ExternalReply')}/{_t('Message')}")),
        "start": _text(s.find(f"{_t('Duration')}/{_t('StartTime')}")).strip(),
        "ende": _text(s.find(f"{_t('Duration')}/{_t('EndTime')}")).strip(),
    }


def kalender_aus_antwort(root: ET.Element) -> list[dict]:
    """FindItemResponse → [{start, ende, frei_belegt, ganztaegig, vertraulichkeit}]
    (EWS-Werte, Zeiten wie geliefert — EWS antwortet in UTC)."""
    ergebnis = root.find(f".//{_m('RootFolder')}")
    if ergebnis is None:
        raise EwsFehler("antwort", "Antwort ohne RootFolder")
    out = []
    for el in ergebnis.iter(_t("CalendarItem")):
        out.append({
            "start": _text(el.find(_t("Start"))).strip(),
            "ende": _text(el.find(_t("End"))).strip(),
            "frei_belegt": _text(el.find(_t("LegacyFreeBusyStatus"))).strip(),
            "ganztaegig": _text(el.find(_t("IsAllDayEvent"))).strip().lower() == "true",
            "vertraulichkeit": _text(el.find(_t("Sensitivity"))).strip(),
        })
    return out


def kennungen_aus_antwort(root: ET.Element) -> list[tuple[str, str]]:
    """FindItemResponse → [(ItemId, ChangeKey)] in der Reihenfolge des Servers."""
    ergebnis = root.find(f".//{_m('RootFolder')}")
    if ergebnis is None:
        raise EwsFehler("antwort", "Antwort ohne RootFolder")
    out = []
    for el in ergebnis.iter(_t("ItemId")):
        if el.get("Id"):
            out.append((el.get("Id"), el.get("ChangeKey") or ""))
    return out


def nachrichten_aus_antwort(root: ET.Element) -> list[dict]:
    """GetItemResponse → [{id, betreff, von, von_art, empfangen, message_id, text,
    text_art}] — nur aus erfolgreichen ResponseMessages (siehe antwort_pruefen
    `teilweise`). Jede Elementart zählt (Message, MeetingRequest, …), wie bei
    Graph `messages`. `von_art` ist der RoutingType (SMTP; EX bei Absendern der
    lokalen Organisation — dann steht in `von` kein SMTP-Name)."""
    out = []
    for rm in root.iter():
        if not (rm.tag.startswith(f"{{{NS_M}}}") and rm.tag.endswith("ResponseMessage")):
            continue
        if not _antwort_ok(rm):
            continue
        elemente = rm.find(_m("Items"))
        if elemente is None:
            continue
        for el in list(elemente):
            kennung = el.find(_t("ItemId"))
            koerper = el.find(_t("Body"))
            absender = el.find(f"{_t('From')}/{_t('Mailbox')}")
            out.append({
                "id": kennung.get("Id", "") if kennung is not None else "",
                "betreff": _text(el.find(_t("Subject"))),
                "von": _text(absender.find(_t("EmailAddress"))).strip() if absender is not None else "",
                "von_name": _text(absender.find(_t("Name"))) if absender is not None else "",
                "von_art": _text(absender.find(_t("RoutingType"))).strip() if absender is not None else "",
                "empfangen": _text(el.find(_t("DateTimeReceived"))).strip(),
                "message_id": _text(el.find(_t("InternetMessageId"))).strip(),
                "text": _text(koerper),
                "text_art": (koerper.get("BodyType") or "") if koerper is not None else "",
                # Nur mit `kette` angefragt (KETTE_FELDER), sonst leer.
                "entwurf": _text(el.find(_t("IsDraft"))).strip().lower() == "true",
                "references": " ".join(_text(el.find(_t("References"))).split()),
                "in_reply_to": _text(el.find(_t("InReplyTo"))).strip(),
            })
    return out


def nachrichten_als_graph(nachrichten: list[dict]) -> list[dict]:
    """EWS-Nachrichten → die Felder, die Graph `mailFolders/inbox/messages` mit
    `$select=subject,from,receivedDateTime,internetMessageId,body` liefert. Die
    Auswertung (Betreff, Absender der CA, Link bzw. Challenge im Text) bleibt
    beim Aufrufer — für beide Wege dieselbe."""
    out = []
    for n in nachrichten:
        out.append({
            "id": n.get("id") or "",
            "subject": n.get("betreff") or "",
            "from": {"emailAddress": {"address": n.get("von") or "",
                                      "name": n.get("von_name") or ""}},
            "receivedDateTime": n.get("empfangen") or "",
            "internetMessageId": n.get("message_id") or "",
            "body": {"contentType": "html" if (n.get("text_art") or "").upper() == "HTML" else "text",
                     "content": n.get("text") or ""},
            "isDraft": bool(n.get("entwurf")),
            # Wie Graph: nur die Kopfzeilen, die es gibt (graph_client._bezug_aus liest sie).
            "internetMessageHeaders": [{"name": name, "value": wert} for name, wert in
                                       (("References", n.get("references") or ""),
                                        ("In-Reply-To", n.get("in_reply_to") or "")) if wert],
        })
    return out


# ── Übersetzung in die Graph-Form (automaticRepliesSetting) ───────────────────

def _graph_zeit(wert: str) -> dict | None:
    if not wert:
        return None
    try:
        d = datetime.fromisoformat(wert.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is not None:
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return {"dateTime": d.strftime("%Y-%m-%dT%H:%M:%S.0000000"), "timeZone": "UTC"}


def als_graph(oof: dict) -> dict:
    """EWS-Form → Graph-`automaticRepliesSetting`."""
    g = {
        "status": ZUSTAND_ZU_GRAPH.get(oof.get("state") or "", "disabled"),
        "externalAudience": PUBLIKUM_ZU_GRAPH.get(oof.get("extern_publikum") or "", "all"),
        "internalReplyMessage": oof.get("intern") or "",
        "externalReplyMessage": oof.get("extern") or "",
    }
    s, e = _graph_zeit(oof.get("start") or ""), _graph_zeit(oof.get("ende") or "")
    if s:
        g["scheduledStartDateTime"] = s
    if e:
        g["scheduledEndDateTime"] = e
    return g


# EWS LegacyFreeBusyStatus / Sensitivity → Graph showAs / sensitivity
FREI_BELEGT_ZU_GRAPH = {"Free": "free", "Tentative": "tentative", "Busy": "busy", "OOF": "oof",
                        "WorkingElsewhere": "workingElsewhere", "NoData": "unknown"}
VERTRAULICHKEIT_ZU_GRAPH = {"Normal": "normal", "Personal": "personal", "Private": "private",
                            "Confidential": "confidential"}


def _ganztag_graph(wert: str, zone) -> dict | None:
    """Grenze eines GANZTÄGIGEN EWS-Termins → Graph-Form.

    EWS liefert die Mitternacht der Termin-Zeitzone als UTC-Zeitpunkt (Berlin:
    „…T23:00:00Z“ am Vortag). Graph liefert ganztägige Termine dagegen als
    „00:00“ des Kalendertags in der angefragten Zone — die Abwesenheit fragt UTC
    an und rechnet danach selbst auf die lokale Mitternacht um
    (`abwesenheit._ganztaegig_utc`). Gebraucht wird also nur der Kalendertag:
    in `zone` (Anzeige-Zeitzone) umgerechnet und auf die nächste Mitternacht
    gerundet — so stimmt der Tag auch, wenn die Termin-Zone eine andere ist
    (bis ±12 h Versatz)."""
    from datetime import timedelta
    if not wert:
        return None
    try:
        d = datetime.fromisoformat(wert.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    lokal = d.astimezone(zone)
    tag = lokal.date() + (timedelta(days=1) if lokal.hour >= 12 else timedelta(0))
    return {"dateTime": f"{tag:%Y-%m-%d}T00:00:00.0000000", "timeZone": "UTC"}


def kalender_als_graph(termine: list[dict], zone=timezone.utc) -> list[dict]:
    """EWS-Termine → Graph-`calendarView`-Einträge mit genau den Feldern, die
    der Graph-Weg anfragt (start, end, showAs, sensitivity, isAllDay), Zeiten in
    UTC wie unter `Prefer: outlook.timezone="UTC"`. Die Auswertung (Status
    „Abwesend“, Mindestdauer, privat) bleibt damit EINE — in abwesenheit."""
    out = []
    for t in termine:
        ganz = bool(t.get("ganztaegig"))
        wandeln = (lambda w: _ganztag_graph(w, zone)) if ganz else _graph_zeit
        out.append({
            "start": wandeln(t.get("start") or ""),
            "end": wandeln(t.get("ende") or ""),
            "showAs": FREI_BELEGT_ZU_GRAPH.get(t.get("frei_belegt") or "", "unknown"),
            "sensitivity": VERTRAULICHKEIT_ZU_GRAPH.get(t.get("vertraulichkeit") or "", "normal"),
            "isAllDay": ganz,
        })
    return out


def _iso_aus_graph(wert: dict | None) -> str:
    """Graph-dateTimeTimeZone → ISO mit Zone. Das Gateway schreibt selbst nur UTC
    (abwesenheit.als_utc); IANA-Namen werden umgerechnet, Unbekanntes gilt als UTC."""
    if not isinstance(wert, dict):
        return ""
    roh = (wert.get("dateTime") or "").strip()
    if not roh:
        return ""
    d = datetime.fromisoformat(roh.split(".")[0])
    zone = (wert.get("timeZone") or "UTC").strip()
    tz = timezone.utc
    if zone.upper() != "UTC":
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(zone)
        except Exception:                                          # noqa: BLE001
            log.warning("EWS: Zeitzone %r unbekannt — als UTC behandelt", zone)
    return d.replace(tzinfo=tz).isoformat()


def aus_graph(setting: dict, aenderung: dict) -> dict:
    """Gelesenen Stand (Graph-Form) mit einer Änderung (Graph-Form, wie sie
    abwesenheit._patch_setting baut) zur VOLLSTÄNDIGEN EWS-Form zusammenführen.
    Was die Änderung nicht nennt, bleibt wie gelesen — wie beim Graph-PATCH."""
    neu = {**(setting or {}), **{k: v for k, v in (aenderung or {}).items() if v is not None}}
    return {
        "state": ZUSTAND_AUS_GRAPH.get(neu.get("status") or "disabled", "Disabled"),
        "extern_publikum": PUBLIKUM_AUS_GRAPH.get(neu.get("externalAudience") or "all", "All"),
        "intern": neu.get("internalReplyMessage") or "",
        "extern": neu.get("externalReplyMessage") or "",
        "start": _iso_aus_graph(neu.get("scheduledStartDateTime")),
        "ende": _iso_aus_graph(neu.get("scheduledEndDateTime")),
    }


# ── Netz ──────────────────────────────────────────────────────────────────────

def _tls_kontext(ca_pem: str):
    """TLS-Kontext: certifi-Bündel PLUS die eigene CA — ergänzt, ersetzt nicht
    (sonst brächen öffentliche Zertifikate). Gebaut wie urllib3 es ohne eigenen
    Kontext täte (`create_urllib3_context`), damit nichts strenger oder lockerer
    wird als der Weg ohne eigene CA. Prüfung und Hostnamen bleiben an; urllib3
    prüft den Hostnamen bei eigenem Kontext selbst (`assert_hostname`)."""
    import ssl

    import certifi
    from urllib3.util.ssl_ import create_urllib3_context
    ctx = create_urllib3_context(cert_reqs=ssl.CERT_REQUIRED)
    ctx.load_verify_locations(cafile=certifi.where())
    if ca_pem:      # leer: nur certifi (LDAPS in ews_bereich ruft auch ohne eigene CA)
        ctx.load_verify_locations(cadata=ca_pem)
    return ctx


def _tls_sitzung(url: str, ip: str = "", ca_pem: str = ""):
    """(requests.Session ohne Anmeldung, Ziel-URL) — TLS-Teil der Sitzung.

    `ip` (Split-DNS, `EWS_ONPREM_VERBINDEN`): Die Verbindung geht an diese IP,
    SNI und Zertifikatsprüfung bleiben beim Hostnamen der URL. Kein globales
    Umbiegen der Namensauflösung — nur diese Sitzung ist betroffen.
    `ca_pem` (`EWS_ONPREM_CA`): eigene CA zusätzlich zu certifi.
    Ohne beides bleibt die Sitzung exakt wie requests sie anlegt. TLS-Prüfung
    ist immer an."""
    import requests
    from requests.adapters import HTTPAdapter

    sitzung = requests.Session()
    sitzung.trust_env = False      # kein Proxy aus der Umgebung vor die lokale Exchange
    teile = urlsplit(url)
    host = teile.hostname or ""
    if not ip and not ca_pem:
        return sitzung, url

    class _EwsAdapter(HTTPAdapter):
        def init_poolmanager(self, *a, **kw):
            if ip:
                kw["server_hostname"] = host
                kw["assert_hostname"] = host
            if ca_pem:
                kw["ssl_context"] = _tls_kontext(ca_pem)
            super().init_poolmanager(*a, **kw)

    if ip:
        ip_teil = f"[{ip}]" if ":" in ip else ip
        netloc = ip_teil + (f":{teile.port}" if teile.port else "")
        url = urlunsplit((teile.scheme, netloc, teile.path, teile.query, ""))
        sitzung.headers["Host"] = host + (f":{teile.port}" if teile.port else "")
    sitzung.mount("https://", _EwsAdapter())
    return sitzung, url


def _sitzung_und_url():
    """(requests.Session, URL) mit NTLM. Lazy: requests_ntlm wird erst hier geladen."""
    from requests_ntlm import HttpNtlmAuth

    url = _wert(KEY_URL)
    fehler = url_pruefen(url)
    if fehler:
        raise EwsFehler("konfiguration", fehler)
    ip = _wert(KEY_VERBINDEN)
    if ip and verbinden_pruefen(ip):
        raise EwsFehler("konfiguration", verbinden_pruefen(ip))
    sitzung, url = _tls_sitzung(url, ip, _wert(KEY_CA))
    sitzung.auth = HttpNtlmAuth(*_zugang())
    return sitzung, url


def _tls_hinweis(zertifikat: str = "das Exchange-Zertifikat",
                 hostname: str = "der EWS-Adresse") -> str:
    """Zusatz zur TLS-Fehlermeldung: Ist eine eigene CA hinterlegt? Derselbe Text
    für EWS und LDAPS (ews_bereich) — beide prüfen gegen denselben Kontext."""
    n = ca_hinterlegt()
    if n:
        return (f" Eigenes CA-Zertifikat hinterlegt ({n}) — passt es zur CA, die "
                f"{zertifikat} ausgestellt hat, und lautet das Zertifikat auf den "
                f"Hostnamen {hostname}?")
    return (f" Kein eigenes CA-Zertifikat hinterlegt — ist {zertifikat} selbstsigniert "
            "oder von einer internen CA, trage das Zertifikat bzw. das der CA unter "
            "„Eigene CA“ ein.")


def _senden(ziel: str, umschlag: bytes) -> tuple[int, bytes]:
    """Ein SOAP-Aufruf. (HTTP-Status, Rumpf) oder EwsFehler bei Netz/TLS."""
    import requests
    sitzung, url = _sitzung_und_url()
    try:
        r = sitzung.post(url, data=umschlag, timeout=ZEITLIMIT_S, allow_redirects=False,
                         headers={"Content-Type": "text/xml; charset=utf-8",
                                  "X-AnchorMailbox": ziel})
        return r.status_code, r.content
    except requests.exceptions.SSLError as exc:
        raise EwsFehler("tls", "TLS-Prüfung fehlgeschlagen — Zertifikat passt nicht zum "
                               f"Hostnamen oder ist nicht vertrauenswürdig ({_kurz(exc)})."
                        + _tls_hinweis()) from None
    except requests.exceptions.ConnectTimeout:
        # Kein Verbindungsaufbau — betrifft die Anbindung, nicht dieses Postfach.
        raise EwsFehler("zeitlimit", f"Keine Verbindung binnen {ZEITLIMIT_S} s",
                        ausfall=True) from None
    except requests.exceptions.Timeout:
        raise EwsFehler("zeitlimit", f"Keine Antwort binnen {ZEITLIMIT_S} s") from None
    except requests.exceptions.ConnectionError as exc:
        raise EwsFehler("verbindung", f"Exchange nicht erreichbar ({_kurz(exc)})") from None
    finally:
        sitzung.close()


def _kurz(exc: Exception) -> str:
    return str(exc)[:160]


def _aufruf(ziel: str, was: str, umschlag: bytes, teilweise: bool = False,
            erwartet: frozenset = frozenset()) -> ET.Element:
    """Senden, auswerten, protokollieren — die Zeile steht NACH dem Aufruf und
    nennt den Ausgang. Codes in `erwartet` sind ein regulärer Ausgang (etwa
    ErrorItemNotFound beim Lesen einer nie angelegten Konfiguration) und
    stehen als INFO statt WARNING im Protokoll."""
    t0 = time.monotonic()
    try:
        _ausfall_pruefen()
    except EwsFehler as exc:
        log.info("EWS %s für %s: übersprungen (Ausfallsperre, %s)", was, ziel, exc.code)
        raise
    try:
        status, roh = _senden(ziel, umschlag)
        root = antwort_pruefen(status, roh, teilweise)
    except EwsFehler as exc:
        (log.info if exc.code in erwartet else log.warning)("EWS %s für %s: fehlgeschlagen nach %.2f s — %s", was, ziel,
                    time.monotonic() - t0, exc)
        if exc.ausfall:
            _ausfall_merken(exc)
        else:
            _ausfall_vorbei()        # Exchange hat geantwortet — die Anbindung steht
        raise
    _ausfall_vorbei()
    log.info("EWS %s für %s: NoError (%.2f s)", was, ziel, time.monotonic() - t0)
    try:
        import ews_bereich
        ews_bereich.zugriff_bestaetigt(ziel)
    except Exception:                                        # noqa: BLE001
        pass                     # Wartemarke ist Anzeige, nie ein Grund zu scheitern
    return root


# ── Öffentliche Funktionen ────────────────────────────────────────────────────

def _ziel(adresse: str) -> str:
    a = (adresse or "").strip()
    if "@" not in a or any(c in a for c in "\r\n\t <>") or len(a) > 254:
        raise EwsFehler("adresse", "Bitte eine gültige E-Mail-Adresse angeben")
    return a


def oof_lesen(adresse: str) -> dict:
    """{state, extern_publikum, intern, extern, start, ende} — EwsFehler bei Fehlern."""
    if not konfiguriert():
        raise EwsFehler("konfiguration", "Anbindung an Exchange lokal ist nicht eingerichtet")
    ziel = _ziel(adresse)
    return oof_aus_antwort(_aufruf(ziel, "GetUserOofSettings", anfrage_oof_lesen(ziel)))


def oof_setzen(adresse: str, state: str, extern_publikum: str, intern: str, extern: str,
               start: str = "", ende: str = "") -> dict:
    """Setzen und danach erneut lesen. Zurück kommt der Stand, wie Exchange ihn
    gespeichert hat — die Abwesenheit vergleicht gegen diese Fassung (Exchange
    kodiert HTML um; ohne Rücklesen schriebe jeder Abgleich neu)."""
    if not konfiguriert():
        raise EwsFehler("konfiguration", "Anbindung an Exchange lokal ist nicht eingerichtet")
    ziel = _ziel(adresse)
    _aufruf(ziel, f"SetUserOofSettings ({state})",
            anfrage_oof_setzen(ziel, state, extern_publikum, intern, extern, start, ende))
    return oof_lesen(ziel)


def kalender_lesen(adresse: str, von: datetime, bis: datetime) -> list[dict]:
    """Termine im Zeitraum (EWS-Form, siehe kalender_aus_antwort) — EwsFehler bei
    Fehlern. Nur lesend."""
    if not konfiguriert():
        raise EwsFehler("konfiguration", "Anbindung an Exchange lokal ist nicht eingerichtet")
    ziel = _ziel(adresse)
    return kalender_aus_antwort(_aufruf(ziel, "FindItem (Kalender)",
                                        anfrage_kalender(ziel, von, bis)))


def verbindung_testen(adresse: str, sperre_umgehen: bool = False,
                      zugang: tuple[str, str] | None = None) -> dict:
    """Probe für den Knopf „Verbindung testen“ und die Gesundheitsprüfung:
    GetUserOofSettings auf `adresse`. {ok, code, detail, dauer}.
    `sperre_umgehen` (nur der Knopf): immer ein echter Aufruf, auch während der
    Ausfallsperre — sonst sähe der Betreiber nach der Behebung bis zu
    AUSFALL_PAUSE_S lang weiter den alten Fehler.
    `zugang` (Konto, Kennwort): nur für diese Probe statt der gespeicherten —
    ews_bereich.pruefen testet so ein ausstehendes Dienstkonto. Die
    Ausfallsperre bleibt davon unberührt (`_ausfall_merken`)."""
    t0 = time.monotonic()
    marke = _sperre_umgehen.set(sperre_umgehen)
    marke_zugang = _zugang_probe.set(tuple(zugang) if zugang else None)
    try:
        oof = oof_lesen(adresse)
    except EwsFehler as exc:
        return {"ok": False, "code": exc.code, "detail": exc.detail,
                "dauer": round(time.monotonic() - t0, 2)}
    except Exception as exc:                                       # noqa: BLE001
        log.warning("EWS-Verbindungstest für %s: unerwarteter Fehler %s", adresse, exc)
        return {"ok": False, "code": "intern", "detail": _kurz(exc),
                "dauer": round(time.monotonic() - t0, 2)}
    finally:
        _sperre_umgehen.reset(marke)
        _zugang_probe.reset(marke_zugang)
    return {"ok": True, "code": "NoError",
            "detail": f"Abwesenheit lesbar (Zustand {oof['state']})",
            "dauer": round(time.monotonic() - t0, 2)}


# ── Postfach lesen: die Weiche für S/MIME-Bezug (seit v1.12.62) ───────────────

def zustaendig(adresse: str) -> bool:
    """DIE Weiche: Geht ein Zugriff auf das Postfach von `adresse` über EWS
    statt Graph? Nur für Hybrid-Nutzer UND nur bei eingerichteter Anbindung.

    Ein EXO-Postfach geht NIE hierher (exo_mailboxes.ist_hybrid ist für jedes
    bekannte EXO-Postfach falsch). Ohne Anbindung wird ist_hybrid gar nicht erst
    befragt. Abwesenheit, Kalender (abwesenheit._ueber_ews) und das Lesen der
    CA-Mails beim Zertifikatsbezug (`postfach_weg`) laufen alle hierüber."""
    if not konfiguriert():
        return False
    import exo_mailboxes
    return exo_mailboxes.ist_hybrid(adresse)


WEG_GRAPH, WEG_EWS, WEG_GESPERRT = "graph", "ews", "gesperrt"

BEZUG_GESPERRT = ("Hybrid-Nutzer: Das Postfach liegt in der lokalen Exchange-Organisation. "
                  "Für den Zertifikatsbezug liest das Gateway die Mail der "
                  "Zertifizierungsstelle — dafür braucht es die Anbindung an Exchange lokal "
                  "(Einstellungen → Allgemein → Exchange lokal (EWS)). Ohne sie: Zertifikat "
                  "über „Importieren“ einspielen.")


def postfach_weg(adresse: str) -> str:
    """Wie liest das Gateway das Postfach von `adresse`?

    graph    — EXO-Postfach (unverändert wie vor v1.12.62)
    ews      — Hybrid-Nutzer mit eingerichteter Anbindung (`zustaendig`)
    gesperrt — Hybrid-Nutzer ohne Anbindung: Graph erreicht das Postfach nicht
               (es gibt in EXO keins), ein Versuch scheiterte nur später und
               undeutlicher."""
    if zustaendig(adresse):
        return WEG_EWS
    import exo_mailboxes
    return WEG_GESPERRT if exo_mailboxes.ist_hybrid(adresse) else WEG_GRAPH


def bezug_sperre(adresse: str) -> str:
    """Grund, warum für `adresse` kein Zertifikat über einen Bezugsweg bestellt
    werden kann, der das Postfach liest (CASTLE, Hub) — oder ""."""
    return BEZUG_GESPERRT if postfach_weg(adresse) == WEG_GESPERRT else ""


def anfrage_regelprobe(von: str, an: str, betreff: str, kopf: str, kopf_wert: str,
                       text: str) -> bytes:
    """CreateItem mit MessageDisposition="SendOnly" ALS `von` (Impersonation):
    senden ohne Kopie in „Gesendete Elemente“. Reihenfolge der Kinder von
    <Message> laut EWS-Schema (learn.microsoft.com, „Message“): Subject, Body,
    ExtendedProperty, ToRecipients. Der x-Kopf als erweiterte Eigenschaft im
    Satz InternetHeaders (ExtendedFieldURI, DistinguishedPropertySetId). Dass
    Exchange ihn als Kopfzeile hinausgibt, belegt die Unzustellbarkeitsmeldung
    einer Lab-Probe (18.11.2026: `x-gw-regelprobe:` unter den Kopfzeilen des
    Originals); die Kennung steht trotzdem auch im Betreff (wie beim Graph-Weg)."""
    req = ET.Element(_m("CreateItem"), {"MessageDisposition": "SendOnly"})
    nachricht = ET.SubElement(ET.SubElement(req, _m("Items")), _t("Message"))
    ET.SubElement(nachricht, _t("Subject")).text = betreff
    ET.SubElement(nachricht, _t("Body"), {"BodyType": "Text"}).text = text
    ep = ET.SubElement(nachricht, _t("ExtendedProperty"))
    ET.SubElement(ep, _t("ExtendedFieldURI"), {"DistinguishedPropertySetId": "InternetHeaders",
                                               "PropertyName": kopf, "PropertyType": "String"})
    ET.SubElement(ep, _t("Value")).text = kopf_wert
    an_el = ET.SubElement(ET.SubElement(nachricht, _t("ToRecipients")), _t("Mailbox"))
    ET.SubElement(an_el, _t("EmailAddress")).text = an
    return _umschlag(von, req)


def regelprobe_senden(von: str, an: str, betreff: str, kopf: str, kopf_wert: str,
                      text: str) -> None:
    """Die Prüfmail der Regelprobe aus dem lokalen Postfach `von` an `an`
    (ein Postfach in Exchange Online). EwsFehler bei Fehlern. Nur regel_probe."""
    if not konfiguriert():
        raise EwsFehler("konfiguration", "Anbindung an Exchange lokal ist nicht eingerichtet")
    ziel = _ziel(von)
    _aufruf(ziel, "CreateItem (Regelprobe)",
            anfrage_regelprobe(ziel, _ziel(an), betreff, kopf, kopf_wert, text))


# ── Unzustellbarkeitsmeldung der Regelprobe (seit v1.12.72) ───────────────────
#
# Scheitert die Probe schon in der lokalen Organisation (Lab 18.11.2026: Domäne
# lokal Authoritative, für das reine Cloud-Postfach kein MailUser/RemoteMailbox
# → `550 5.1.10 RESOLVER.ADR.RecipientNotFound`), legt Exchange die Meldung in
# den Posteingang des Hybrid-Nutzers. Live gelesen (Exchange SE, englisch):
# ItemClass `REPORT.IPM.Note.NDR`, Betreff „Undeliverable: <Betreff der Probe>“
# (deutsch „Unzustellbar: …“ — Annahme, nicht gemessen), Text mit der Zeile
# „Remote Server returned '550 5.1.10 RESOLVER.ADR.RecipientNotFound; …'“ und
# den Kopfzeilen der Probe (darunter `x-gw-regelprobe` — der Kopf geht also
# tatsächlich hinaus). Erkannt wird über ItemClass + Betreff, nie über den
# lokalisierten Vorspann.

NDR_FELDER = ("item:ItemClass", "item:Subject", "item:DateTimeReceived", "item:Body")


def anfrage_ndr_holen(ziel: str, kennungen: list[tuple[str, str]]) -> bytes:
    """GetItem: ItemClass, Betreff, Empfangszeit und Text als Klartext
    (BodyType Text — der Statuscode wird aus dem Text gelesen)."""
    req = ET.Element(_m("GetItem"))
    form = ET.SubElement(req, _m("ItemShape"))
    ET.SubElement(form, _t("BaseShape")).text = "IdOnly"
    ET.SubElement(form, _t("BodyType")).text = "Text"
    felder = ET.SubElement(form, _t("AdditionalProperties"))
    for f in NDR_FELDER:
        _feld(felder, f)
    ids = ET.SubElement(req, _m("ItemIds"))
    for kennung, aenderung in kennungen:
        attr = {"Id": kennung}
        if aenderung:
            attr["ChangeKey"] = aenderung
        ET.SubElement(ids, _t("ItemId"), attr)
    return _umschlag(ziel, req)


def ndr_aus_antwort(root: ET.Element) -> list[dict]:
    """GetItemResponse → [{id, aenderung, klasse, betreff, text}] — nur
    Elemente, deren ItemClass eine Unzustellbarkeitsmeldung ist
    (`REPORT.….NDR`, ohne Rücksicht auf Groß-/Kleinschreibung)."""
    out = []
    for rm in root.iter():
        if not (rm.tag.startswith(f"{{{NS_M}}}") and rm.tag.endswith("ResponseMessage")):
            continue
        if not _antwort_ok(rm):
            continue
        elemente = rm.find(_m("Items"))
        if elemente is None:
            continue
        for el in list(elemente):
            klasse = _text(el.find(_t("ItemClass"))).strip()
            k = klasse.upper()
            if not (k.startswith("REPORT.") and k.endswith(".NDR")):
                continue
            kennung = el.find(_t("ItemId"))
            out.append({
                "id": kennung.get("Id", "") if kennung is not None else "",
                "aenderung": kennung.get("ChangeKey", "") if kennung is not None else "",
                "klasse": klasse,
                "betreff": _text(el.find(_t("Subject"))),
                "text": _text(el.find(_t("Body"))),
            })
    return out


def regelprobe_ndr_suchen(adresse: str, betreff_enthaelt: str,
                          ab: datetime | None = None) -> list[dict]:
    """Unzustellbarkeitsmeldungen im Posteingang von `adresse`, deren Betreff
    `betreff_enthaelt` trägt (siehe ndr_aus_antwort). EwsFehler bei Fehlern.
    Nur regel_probe."""
    kennungen = posteingang_suchen(adresse, 5, ab, betreff_enthaelt)
    if not kennungen:
        return []
    ziel = _ziel(adresse)
    return ndr_aus_antwort(_aufruf(ziel, f"GetItem (NDR, {len(kennungen)})",
                                   anfrage_ndr_holen(ziel, kennungen), teilweise=True))


# SoftDelete: Die Meldung verschwindet aus Posteingang UND „Gelöschte Elemente“
# (Ordner „Deletions“ des Dumpsters), bleibt aber über „Gelöschte Elemente
# wiederherstellen“ für die Aufbewahrungsfrist erreichbar. MoveToDeletedItems
# ließe sie beim Nutzer sichtbar liegen, HardDelete (Purges) entzöge sie ihm
# endgültig — für eine Meldung, die der Nutzer nicht selbst ausgelöst hat,
# ist das eine wie das andere unangemessen. Quelle: learn.microsoft.com,
# „Deleting items by using EWS in Exchange“, Tabelle 1.
NDR_LOESCHART = "SoftDelete"


def anfrage_loeschen(ziel: str, kennung: str, aenderung: str = "",
                     art: str = NDR_LOESCHART) -> bytes:
    req = ET.Element(_m("DeleteItem"), {"DeleteType": art})
    attr = {"Id": kennung}
    if aenderung:
        attr["ChangeKey"] = aenderung
    ET.SubElement(ET.SubElement(req, _m("ItemIds")), _t("ItemId"), attr)
    return _umschlag(ziel, req)


def regelprobe_ndr_loeschen(adresse: str, kennung: str, aenderung: str = "") -> None:
    """DeleteItem (SoftDelete) der Unzustellbarkeitsmeldung `kennung` im
    Postfach `adresse`. EwsFehler bei Fehlern. Nur regel_probe."""
    if not konfiguriert():
        raise EwsFehler("konfiguration", "Anbindung an Exchange lokal ist nicht eingerichtet")
    if not kennung:
        raise EwsFehler("kennung", "Keine Kennung der Meldung")
    ziel = _ziel(adresse)
    _aufruf(ziel, "DeleteItem (NDR der Regelprobe)", anfrage_loeschen(ziel, kennung, aenderung))


def posteingang_suchen(adresse: str, anzahl: int = 1, ab: datetime | None = None,
                       betreff_enthaelt: str = "") -> list[tuple[str, str]]:
    """Nur die Kennungen der jüngsten Mails (FindItem, IdOnly) — EwsFehler bei
    Fehlern. Auch die Probe der Gesundheitsprüfung: kein Betreff, kein Text."""
    if not konfiguriert():
        raise EwsFehler("konfiguration", "Anbindung an Exchange lokal ist nicht eingerichtet")
    ziel = _ziel(adresse)
    return kennungen_aus_antwort(_aufruf(
        ziel, "FindItem (Posteingang)", anfrage_posteingang_suchen(ziel, ab, betreff_enthaelt, anzahl)))


def posteingang_lesen(adresse: str, ab: datetime | None = None, betreff_enthaelt: str = "",
                      anzahl: int = 25) -> list[dict]:
    """Jüngste Mails im Posteingang (EWS-Form, siehe nachrichten_aus_antwort),
    neueste zuerst — EwsFehler bei Fehlern. Nur lesend: FindItem (Kennungen),
    dann EIN GetItem für alle Treffer. Ohne Treffer kein zweiter Aufruf."""
    kennungen = posteingang_suchen(adresse, anzahl, ab, betreff_enthaelt)
    if not kennungen:
        return []
    ziel = _ziel(adresse)
    return nachrichten_aus_antwort(_aufruf(
        ziel, f"GetItem ({len(kennungen)} Nachricht(en))",
        anfrage_nachrichten_holen(ziel, kennungen), teilweise=True))


def posteingang_als_graph(adresse: str, ab: datetime | None = None, betreff_enthaelt: str = "",
                          anzahl: int = 25) -> list[dict]:
    """`posteingang_lesen` in der Form des Graph-Abrufs (`value` von
    mailFolders/inbox/messages). Blockierend — Aufrufer im Event-Loop nutzen
    asyncio.to_thread."""
    return nachrichten_als_graph(posteingang_lesen(adresse, ab, betreff_enthaelt, anzahl))


# ── Beantwortete Mail für das Verfassen-Add-in (seit v1.12.69) ────────────────
#
# Antwortet ein Hybrid-Nutzer, nennt Outlook die beantwortete Mail über
# `item.inReplyTo` (Mailbox 1.14). Das Add-in sucht sie, um zu entscheiden, ob
# die volle oder die Antwort-Signatur gilt — für EXO-Postfächer per Graph
# (graph_client.bezug_per_kennung, über ALLE Ordner). EWS sucht nur flach je
# Ordner (Traversal Deep gibt es nur für Suchordner); gesucht wird deshalb in
# Posteingang und Gesendete Elemente in EINEM FindItem. Liegt die Mail woanders,
# bleibt der Entwurf leer und das Gateway entscheidet beim Versand — wie bei
# jeder nicht ermittelbaren Antwort.

BEZUG_ORDNER = ("inbox", "sentitems")
BEZUG_MAX = 5           # wie Graph ($top=5): dieselbe Mail ggf. in beiden Ordnern


def anfrage_kennung_suchen(ziel: str, message_id: str,
                           ordner: tuple[str, ...] = BEZUG_ORDNER) -> bytes:
    """FindItem nach der Message-ID (Gleichheit, kein Teilstring) in `ordner`.
    Der Wert geht als Attribut über xml.etree hinein."""
    req = ET.Element(_m("FindItem"), {"Traversal": "Shallow"})
    ET.SubElement(ET.SubElement(req, _m("ItemShape")), _t("BaseShape")).text = "IdOnly"
    ET.SubElement(req, _m("IndexedPageItemView"), {"MaxEntriesReturned": str(BEZUG_MAX),
                                                   "Offset": "0", "BasePoint": "Beginning"})
    ET.SubElement(req, _m("Restriction")).append(
        _vergleich("IsEqualTo", "message:InternetMessageId", message_id))
    ordner_el = ET.SubElement(req, _m("ParentFolderIds"))
    for o in ordner:
        d = ET.SubElement(ordner_el, _t("DistinguishedFolderId"), {"Id": o})
        ET.SubElement(ET.SubElement(d, _t("Mailbox")), _t("EmailAddress")).text = ziel
    return _umschlag(ziel, req)


def kennungen_aller_ordner(root: ET.Element) -> list[tuple[str, str]]:
    """FindItemResponse über mehrere Ordner → [(ItemId, ChangeKey)] aus jeder
    ERFOLGREICHEN Antwort (je Ordner eine). Ein Ordner mit Fehler nimmt die
    anderen nicht mit (antwort_pruefen `teilweise`)."""
    out = []
    for rm in root.iter():
        if not (rm.tag.startswith(f"{{{NS_M}}}") and rm.tag.endswith("ResponseMessage")):
            continue
        if not _antwort_ok(rm):
            continue
        ordner = rm.find(_m("RootFolder"))
        if ordner is None:
            continue
        for el in ordner.iter(_t("ItemId")):
            if el.get("Id"):
                out.append((el.get("Id"), el.get("ChangeKey") or ""))
    return out


def bezug_per_kennung(adresse: str, message_id: str) -> list[dict]:
    """Die Mail mit `message_id` aus Posteingang und Gesendete Elemente von
    `adresse`, in der Form des Graph-Abrufs `users/{p}/messages?$filter=
    internetMessageId eq …` (mit isDraft und internetMessageHeaders). [] = nicht
    gefunden. EwsFehler bei Fehlern. Blockierend — nur über asyncio.to_thread."""
    if not konfiguriert():
        raise EwsFehler("konfiguration", "Anbindung an Exchange lokal ist nicht eingerichtet")
    ziel = _ziel(adresse)
    kennungen = kennungen_aller_ordner(_aufruf(
        ziel, "FindItem (beantwortete Mail)", anfrage_kennung_suchen(ziel, message_id),
        teilweise=True))
    if not kennungen:
        return []
    return nachrichten_als_graph(nachrichten_aus_antwort(_aufruf(
        ziel, f"GetItem ({len(kennungen)} Nachricht(en), Kette)",
        anfrage_nachrichten_holen(ziel, kennungen, kette=True), teilweise=True)))


# ── OWA-Signatur lokal (seit v1.12.63) ────────────────────────────────────────
#
# OWA der lokalen Exchange hält ihre Einstellungen in der Benutzerkonfiguration
# „OWA.UserOptions“ im Stammordner (`root`) des Postfachs: ein Wörterbuch aus
# typisierten Einträgen (EWS GetUserConfiguration / UpdateUserConfiguration /
# CreateUserConfiguration). Im Lab gemessen (Exchange SE 15.2, 18.11.2026):
#
# * Fehlt das Objekt (Postfach hat OWA-Optionen nie gespeichert), antwortet
#   GetUserConfiguration mit ErrorItemNotFound; CreateUserConfiguration legt es
#   an, ein zweites Create scheitert mit ErrorItemSave („already exists“).
# * UpdateUserConfiguration ERSETZT das Wörterbuch als Ganzes. Ein Update nur
#   mit `signaturehtml` liess alle übrigen Einträge verschwinden. Deshalb hier
#   immer lesen → zusammenführen → schreiben (`uc_zusammenfuehren`), wie beim
#   OOF (`aus_graph`).
# * Typen bleiben erhalten (String, Boolean, Integer32 zurückgelesen wie
#   geschrieben), HTML mit Umlauten und & kommt Zeichen für Zeichen zurück.
#
# Die Schlüsselnamen (`signaturehtml`, `signaturetext`, `autoaddsignature`)
# stammen aus Microsofts Beispiel zu OWA.UserOptions (Exchange 2010, EWS
# Managed API); `autoaddsignatureonreply` ist aus dem Cmdlet-Parameter
# AutoAddSignatureOnReply (Exchange 2016+) abgeleitet. Im Lab-Postfach gab es
# das Objekt nicht — die Namen und dass OWA sie so liest, sind bis zum
# Klicktest in OWA eine ANNAHME.

UC_OWA = "OWA.UserOptions"
OWA_SIG_HTML = "signaturehtml"
OWA_SIG_TEXT = "signaturetext"
OWA_AUTO_NEU = "autoaddsignature"
OWA_AUTO_ANTWORT = "autoaddsignatureonreply"

# Werttypen des Wörterbuchs (UserConfigurationDictionaryObjectTypesType).
UC_TYPEN = frozenset({"DateTime", "Boolean", "Byte", "String", "Integer32", "UnsignedInteger32",
                      "Integer64", "UnsignedInteger64", "StringArray", "ByteArray"})


def _uc_name(tag: str, ziel: str, name: str) -> ET.Element:
    n = ET.Element(tag, {"Name": name})
    ordner = ET.SubElement(n, _t("DistinguishedFolderId"), {"Id": "root"})
    ET.SubElement(ET.SubElement(ordner, _t("Mailbox")), _t("EmailAddress")).text = ziel
    return n


def anfrage_uc_lesen(ziel: str, name: str = UC_OWA) -> bytes:
    """GetUserConfiguration mit allen Teilen (Wörterbuch, XmlData, BinaryData) —
    gebraucht wird alles, weil das Zurückschreiben nichts verlieren darf."""
    req = ET.Element(_m("GetUserConfiguration"))
    req.append(_uc_name(_m("UserConfigurationName"), ziel, name))
    ET.SubElement(req, _m("UserConfigurationProperties")).text = "All"
    return _umschlag(ziel, req)


def anfrage_uc_schreiben(ziel: str, konfig: dict, anlegen: bool, name: str = UC_OWA) -> bytes:
    """Create- bzw. UpdateUserConfiguration mit dem VOLLSTÄNDIGEN Stand `konfig`
    ({eintraege, xml_data, binary_data}, Form wie `uc_aus_antwort`). Update
    ersetzt das Wörterbuch als Ganzes — wer nur einen Eintrag ändert, übergibt
    die zusammengeführte Liste (`uc_zusammenfuehren`)."""
    req = ET.Element(_m("CreateUserConfiguration" if anlegen else "UpdateUserConfiguration"))
    uc = ET.SubElement(req, _m("UserConfiguration"))
    uc.append(_uc_name(_t("UserConfigurationName"), ziel, name))
    d = ET.SubElement(uc, _t("Dictionary"))
    for e in konfig.get("eintraege") or []:
        if e.get("vtyp") and e["vtyp"] not in UC_TYPEN:
            raise ValueError(f"Unbekannter Werttyp im Wörterbuch: {e['vtyp']!r}")
        de = ET.SubElement(d, _t("DictionaryEntry"))
        k = ET.SubElement(de, _t("DictionaryKey"))
        ET.SubElement(k, _t("Type")).text = e.get("ktyp") or "String"
        ET.SubElement(k, _t("Value")).text = e["k"]
        if e.get("werte") is not None:          # None = Eintrag ohne Wert (wie gelesen)
            v = ET.SubElement(de, _t("DictionaryValue"))
            ET.SubElement(v, _t("Type")).text = e["vtyp"]
            for w in e["werte"]:
                ET.SubElement(v, _t("Value")).text = w
    if konfig.get("xml_data") is not None:
        ET.SubElement(uc, _t("XmlData")).text = konfig["xml_data"]
    if konfig.get("binary_data") is not None:
        ET.SubElement(uc, _t("BinaryData")).text = konfig["binary_data"]
    return _umschlag(ziel, req)


def uc_aus_antwort(root: ET.Element) -> dict:
    """GetUserConfigurationResponse → {eintraege: [{ktyp, k, vtyp, werte}],
    xml_data, binary_data}. `werte` ist eine Liste (StringArray hat mehrere
    <Value>), None bei einem Eintrag ohne DictionaryValue. Reihenfolge wie
    geliefert."""
    uc = root.find(f".//{_m('UserConfiguration')}")
    if uc is None:
        raise EwsFehler("antwort", "Antwort ohne UserConfiguration")
    eintraege = []
    for de in uc.iter(_t("DictionaryEntry")):
        k = de.find(_t("DictionaryKey"))
        v = de.find(_t("DictionaryValue"))
        if k is None:
            continue
        eintraege.append({
            "ktyp": _text(k.find(_t("Type"))).strip() or "String",
            "k": _text(k.find(_t("Value"))),
            "vtyp": _text(v.find(_t("Type"))).strip() if v is not None else "",
            "werte": [_text(w) for w in v.findall(_t("Value"))] if v is not None else None,
        })
    xml_el, bin_el = uc.find(_t("XmlData")), uc.find(_t("BinaryData"))
    return {"eintraege": eintraege,
            "xml_data": _text(xml_el) if xml_el is not None else None,
            "binary_data": _text(bin_el) if bin_el is not None else None}


def _bool_wert(vtyp: str, an: bool) -> str:
    # Boolean schreibt EWS klein („true“, im Lab so zurückgelesen); steht der
    # Eintrag schon als String da, bleibt er String in .NET-Schreibweise.
    return ("True" if an else "False") if vtyp == "String" else ("true" if an else "false")


def uc_zusammenfuehren(eintraege: list[dict], aenderung: dict) -> list[dict]:
    """Gelesene Einträge + Änderung {schluessel: (typ, wert)} → vollständige
    Liste. Fremde Einträge bleiben unverändert und an ihrem Platz; ein
    vorhandener Schlüssel wird ersetzt (Vergleich ohne Groß-/Kleinschreibung,
    Schreibweise des Bestands bleibt), ein neuer hinten angefügt. Ein
    vorhandener Wahrheitswert behält seinen Typ (Boolean oder String)."""
    rest = {k.lower(): (k, typ, wert) for k, (typ, wert) in aenderung.items()}
    out = []
    for e in eintraege:
        treffer = rest.pop((e.get("k") or "").lower(), None)
        if treffer is None:
            out.append(dict(e))
            continue
        _k, typ, wert = treffer
        if typ == "Boolean":
            vtyp = e.get("vtyp") if e.get("vtyp") in ("Boolean", "String") else "Boolean"
            out.append({**e, "vtyp": vtyp, "werte": [_bool_wert(vtyp, bool(wert))]})
        else:
            out.append({**e, "vtyp": typ, "werte": [wert]})
    for k, typ, wert in rest.values():
        out.append({"ktyp": "String", "k": k, "vtyp": typ,
                    "werte": [_bool_wert(typ, bool(wert)) if typ == "Boolean" else wert]})
    return out


def _uc_wert(eintraege: list[dict], schluessel: str) -> str | None:
    """Erster Wert des Eintrags `schluessel` (ohne Groß-/Kleinschreibung) oder None."""
    for e in eintraege:
        if (e.get("k") or "").lower() == schluessel.lower():
            return (e.get("werte") or [""])[0]
    return None


def _uc_bool(eintraege: list[dict], schluessel: str) -> bool:
    # Boolean („true“) wie String („True“) — beides kommt vor (_bool_wert).
    return (_uc_wert(eintraege, schluessel) or "").strip().lower() == "true"


def owa_konfig_lesen(adresse: str) -> dict | None:
    """OWA.UserOptions vollständig (Form `uc_aus_antwort`) — None, wenn das
    Postfach das Objekt nicht hat (ErrorItemNotFound). EwsFehler sonst."""
    if not konfiguriert():
        raise EwsFehler("konfiguration", "Anbindung an Exchange lokal ist nicht eingerichtet")
    ziel = _ziel(adresse)
    try:
        return uc_aus_antwort(_aufruf(ziel, "GetUserConfiguration (OWA.UserOptions)",
                                      anfrage_uc_lesen(ziel),
                                      erwartet=frozenset({"ErrorItemNotFound"})))
    except EwsFehler as exc:
        if exc.code == "ErrorItemNotFound":
            return None
        raise


def owa_signatur_aus_konfig(konfig: dict | None) -> dict:
    """{vorhanden, html, text, auto_neu, auto_antwort} aus OWA.UserOptions."""
    eintraege = (konfig or {}).get("eintraege") or []
    return {"vorhanden": konfig is not None,
            "html": _uc_wert(eintraege, OWA_SIG_HTML) or "",
            "text": _uc_wert(eintraege, OWA_SIG_TEXT) or "",
            "auto_neu": _uc_bool(eintraege, OWA_AUTO_NEU),
            "auto_antwort": _uc_bool(eintraege, OWA_AUTO_ANTWORT)}


def owa_signatur_lesen(adresse: str) -> dict:
    """Signatur-Einstellungen der OWA (lokal) — nur lesend."""
    return owa_signatur_aus_konfig(owa_konfig_lesen(adresse))


def owa_signatur_setzen(adresse: str, html: str, text: str, auto_neu: bool,
                        auto_antwort: bool) -> dict:
    """Signatur in OWA.UserOptions schreiben, ohne fremde Einträge zu verlieren:
    lesen → zusammenführen → Update (bzw. Create, wenn das Objekt fehlt) →
    zurücklesen. Weicht der zurückgelesene Stand ab (etwa gekürzt), ist das ein
    Fehler — nicht still „gesetzt“. Zurück kommt die Form von
    `owa_signatur_aus_konfig`."""
    ziel = _ziel(adresse)
    vorher = owa_konfig_lesen(ziel)
    aenderung = {OWA_SIG_HTML: ("String", html or ""), OWA_SIG_TEXT: ("String", text or ""),
                 OWA_AUTO_NEU: ("Boolean", auto_neu), OWA_AUTO_ANTWORT: ("Boolean", auto_antwort)}
    neu = {**(vorher or {"xml_data": None, "binary_data": None}),
           "eintraege": uc_zusammenfuehren((vorher or {}).get("eintraege") or [], aenderung)}
    anlegen = vorher is None
    _aufruf(ziel, "CreateUserConfiguration (OWA.UserOptions)" if anlegen
            else "UpdateUserConfiguration (OWA.UserOptions)",
            anfrage_uc_schreiben(ziel, neu, anlegen))
    nachher = owa_konfig_lesen(ziel)
    stand = owa_signatur_aus_konfig(nachher)
    if (stand["html"] != (html or "") or stand["text"] != (text or "")
            or stand["auto_neu"] != bool(auto_neu) or stand["auto_antwort"] != bool(auto_antwort)):
        raise EwsFehler("antwort", "Exchange hat die Signatur nicht wie geschrieben übernommen "
                                   f"({len(stand['html'])} statt {len(html or '')} Zeichen HTML)")
    return stand
