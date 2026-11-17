"""Anbindung an eine lokale Exchange-Organisation (2016/2019/SE) über EWS — optional.

WOFÜR
-----
Hybrid-Nutzer (MAILBOX_CONFIG-Eintrag mit `hybrid: True`, siehe exo_mailboxes)
haben ihr Postfach in der lokalen Organisation. Graph erreicht es nicht. Für
die Abwesenheit spricht das Gateway deshalb mit der lokalen Exchange per EWS —
und NUR für diese Postfächer. Die Weiche dafür steht an EINER Stelle:
`abwesenheit._ueber_ews()`. Ein EXO-Postfach geht nie hierher.

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

Diese Aufrufe sind blockierend (requests). Aufrufer im Event-Loop nutzen
`asyncio.to_thread`. Nie im SMTP-Pfad aufrufen.
"""
from __future__ import annotations

import ipaddress
import logging
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

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail

    @property
    def kein_zugriff(self) -> bool:
        return self.code in KEIN_ZUGRIFF_CODES


# ── Konfiguration ─────────────────────────────────────────────────────────────

def _wert(key: str) -> str:
    v = settings_store.get(key)
    return v.strip() if isinstance(v, str) else ""


def konfiguriert() -> bool:
    """URL, Konto und Kennwort gesetzt? Sonst ist die Anbindung aus — und
    niemand ruft dieses Modul auf."""
    return bool(_wert(KEY_URL) and _wert(KEY_KONTO) and _wert(KEY_KENNWORT))


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


def antwort_pruefen(status: int, roh: bytes | str) -> ET.Element:
    """HTTP-Status + Rumpf → Wurzel des Antwort-XML, oder EwsFehler."""
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
    rm = root.find(f".//{{{NS_M}}}ResponseMessage")
    if rm is None:
        raise EwsFehler("antwort", "Antwort ohne ResponseMessage")
    code = _text(rm.find(_m("ResponseCode"))).strip()
    if rm.get("ResponseClass") != "Success" or code != "NoError":
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

def _sitzung_und_url():
    """(requests.Session, URL). Lazy: requests_ntlm wird erst hier geladen.

    `EWS_ONPREM_VERBINDEN` (Split-DNS): Die Verbindung geht an diese IP, SNI und
    Zertifikatsprüfung bleiben beim Hostnamen der URL. Kein globales Umbiegen
    der Namensauflösung — nur diese Sitzung ist betroffen. TLS-Prüfung ist
    immer an."""
    import requests
    from requests.adapters import HTTPAdapter
    from requests_ntlm import HttpNtlmAuth

    url = _wert(KEY_URL)
    fehler = url_pruefen(url)
    if fehler:
        raise EwsFehler("konfiguration", fehler)
    sitzung = requests.Session()
    sitzung.auth = HttpNtlmAuth(_wert(KEY_KONTO), _wert(KEY_KENNWORT))
    sitzung.trust_env = False      # kein Proxy aus der Umgebung vor die lokale Exchange
    ip = _wert(KEY_VERBINDEN)
    if ip:
        if verbinden_pruefen(ip):
            raise EwsFehler("konfiguration", verbinden_pruefen(ip))
        teile = urlsplit(url)
        host = teile.hostname or ""

        class _FesteAdresse(HTTPAdapter):
            def init_poolmanager(self, *a, **kw):
                kw["server_hostname"] = host
                kw["assert_hostname"] = host
                super().init_poolmanager(*a, **kw)

        ip_teil = f"[{ip}]" if ":" in ip else ip
        netloc = ip_teil + (f":{teile.port}" if teile.port else "")
        url = urlunsplit((teile.scheme, netloc, teile.path, teile.query, ""))
        sitzung.mount("https://", _FesteAdresse())
        sitzung.headers["Host"] = host + (f":{teile.port}" if teile.port else "")
    return sitzung, url


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
                               f"Hostnamen oder ist nicht vertrauenswürdig ({_kurz(exc)})") from None
    except requests.exceptions.Timeout:
        raise EwsFehler("zeitlimit", f"Keine Antwort binnen {ZEITLIMIT_S} s") from None
    except requests.exceptions.ConnectionError as exc:
        raise EwsFehler("verbindung", f"Exchange nicht erreichbar ({_kurz(exc)})") from None
    finally:
        sitzung.close()


def _kurz(exc: Exception) -> str:
    return str(exc)[:160]


def _aufruf(ziel: str, was: str, umschlag: bytes) -> ET.Element:
    """Senden, auswerten, protokollieren — die Zeile steht NACH dem Aufruf und
    nennt den Ausgang."""
    t0 = time.monotonic()
    try:
        status, roh = _senden(ziel, umschlag)
        root = antwort_pruefen(status, roh)
    except EwsFehler as exc:
        log.warning("EWS %s für %s: fehlgeschlagen nach %.2f s — %s", was, ziel,
                    time.monotonic() - t0, exc)
        raise
    log.info("EWS %s für %s: NoError (%.2f s)", was, ziel, time.monotonic() - t0)
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


def verbindung_testen(adresse: str) -> dict:
    """Probe für den Knopf „Verbindung testen“ und die Gesundheitsprüfung:
    GetUserOofSettings auf `adresse`. {ok, code, detail, dauer}."""
    t0 = time.monotonic()
    try:
        oof = oof_lesen(adresse)
    except EwsFehler as exc:
        return {"ok": False, "code": exc.code, "detail": exc.detail,
                "dauer": round(time.monotonic() - t0, 2)}
    except Exception as exc:                                       # noqa: BLE001
        log.warning("EWS-Verbindungstest für %s: unerwarteter Fehler %s", adresse, exc)
        return {"ok": False, "code": "intern", "detail": _kurz(exc),
                "dauer": round(time.monotonic() - t0, 2)}
    return {"ok": True, "code": "NoError",
            "detail": f"Abwesenheit lesbar (Zustand {oof['state']})",
            "dauer": round(time.monotonic() - t0, 2)}
