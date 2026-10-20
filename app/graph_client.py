import asyncio
import datetime
import hashlib
import json
import logging
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass

import httpx
import msal

import config
import settings_store

log = logging.getLogger(__name__)

_SCOPES = ["https://graph.microsoft.com/.default"]
_BASE_SELECT_FIELDS = [
    "displayName", "givenName", "surname",
    "jobTitle", "department", "companyName",
    "mail", "userPrincipalName",
    "mobilePhone", "businessPhones", "faxNumber",
    "officeLocation", "streetAddress", "city", "state", "postalCode", "country",
    "businessHomePage",
    "onPremisesExtensionAttributes",
    "employeeId",
]

# Entra-Attribute die für Custom-Template-Vars wählbar sind (für das UI)
CUSTOM_VAR_ENTRA_FIELDS: list[dict] = [
    {"value": "displayName",         "label": "Anzeigename (displayName)"},
    {"value": "givenName",           "label": "Vorname (givenName)"},
    {"value": "surname",             "label": "Nachname (surname)"},
    {"value": "jobTitle",            "label": "Berufsbezeichnung (jobTitle)"},
    {"value": "department",          "label": "Abteilung (department)"},
    {"value": "companyName",         "label": "Unternehmen (companyName)"},
    {"value": "mail",                "label": "E-Mail (mail)"},
    {"value": "userPrincipalName",   "label": "UPN (userPrincipalName)"},
    {"value": "mobilePhone",         "label": "Mobiltelefon (mobilePhone)"},
    {"value": "businessPhones[0]",   "label": "Telefon geschäftlich (businessPhones[0])"},
    {"value": "faxNumber",           "label": "Fax (faxNumber)"},
    {"value": "officeLocation",      "label": "Bürostandort (officeLocation)"},
    {"value": "streetAddress",       "label": "Straße (streetAddress)"},
    {"value": "city",                "label": "Stadt (city)"},
    {"value": "state",               "label": "Bundesland/Kanton (state)"},
    {"value": "postalCode",          "label": "PLZ (postalCode)"},
    {"value": "country",             "label": "Land (country)"},
    {"value": "businessHomePage",    "label": "Webseite (businessHomePage)"},
    {"value": "employeeId",          "label": "Mitarbeiter-ID (employeeId)"},
    *[{"value": f"extensionAttribute{i}", "label": f"Erweiterungsattribut {i} (extensionAttribute{i})"} for i in range(1, 16)],
]

def _build_select_fields() -> str:
    """Build $select string, adding extra fields needed for custom template vars."""
    from settings_store import get as _sg
    extra = set()
    for field in [cv.get("entra_field", "") for cv in (_sg("CUSTOM_TEMPLATE_VARS") or [])]:
        if "[" in field:
            field = field.split("[")[0]
        if field.startswith("extensionAttribute"):
            extra.add("onPremisesExtensionAttributes")
        elif field and field not in _BASE_SELECT_FIELDS:
            extra.add(field)
    return ",".join(_BASE_SELECT_FIELDS + sorted(extra))

_SELECT_FIELDS = ",".join(_BASE_SELECT_FIELDS)

_pool_entries: list[dict] = []   # [{client_id, label, msal}]
_pool_lock = threading.Lock()
_pool_idx: int = 0
_pool_hash: str = ""
_throttled_until: dict[str, float] = {}   # client_id → monotonic timestamp
_last_used_client_id: str = ""            # für mark_throttled nach 429
_call_stats: dict[str, dict] = {}        # client_id → {date, hours[24], peak_hour, peak_count}

# Cache für das rohe Graph-Nutzerprofil (NUR die Graph-Felder). Der OOO-Poll ruft
# get_user je Postfach je Durchlauf (alle 10 min) — ungecacht wäre das je Postfach
# ein Graph-Call bei JEDEM Poll und skaliert nicht auf Tausende Postfächer. Das
# Profil (displayName/jobTitle/… aus Entra) ändert sich selten, darum TTL-Cache.
# ⚠️ NUR die Graph-Daten werden gecacht; die settings-abhängigen Teile (Overrides,
# Gruppen-/Custom-Variablen, Website) merkt get_user bei JEDEM Aufruf frisch —
# Admin-Änderungen greifen also sofort, nichts friert ein.
_USER_DATA_TTL = 3600.0                   # s — Profil ändert sich selten
_user_data_cache: dict[str, tuple[dict, float]] = {}   # email → (data, ablauf_monotonic)


def _flush_to_db() -> None:
    """Schreibt akkumulierte Stundenzähler in die SQLite-DB. Läuft im Hintergrund-Thread."""
    try:
        import mail_audit as _ma
        with _pool_lock:
            records = [
                (cid, s["date"], h, cnt)
                for cid, s in _call_stats.items()
                for h, cnt in enumerate(s["hours"])
                if cnt > 0
            ]
        if records:
            _ma.flush_graph_calls(records)
    except Exception as exc:
        log.warning("graph_client: _flush_to_db failed: %s", exc)


def _restore_from_db(entries: list[dict]) -> None:
    """Stellt Tages-Statistik aus DB wieder her (nach Container-Neustart)."""
    try:
        import mail_audit as _ma
        today = datetime.date.today().isoformat()
        with _pool_lock:
            for e in entries:
                cid = e["client_id"]
                if cid in _call_stats:
                    continue  # bereits initialisiert
                hours = _ma.get_graph_calls_hours(cid, today)
                if any(h > 0 for h in hours):
                    peak_h = max(range(24), key=lambda i: hours[i])
                    _call_stats[cid] = {
                        "date": today,
                        "hours": hours,
                        "peak_hour": peak_h if hours[peak_h] > 0 else -1,
                        "peak_count": hours[peak_h],
                    }
    except Exception as exc:
        log.warning("graph_client: _restore_from_db failed: %s", exc)


def _start_flush_thread() -> None:
    def _loop():
        while True:
            time.sleep(60)
            _flush_to_db()
    t = threading.Thread(target=_loop, daemon=True, name="graph-stats-flush")
    t.start()


_start_flush_thread()


def _rebuild_pool_if_needed() -> None:
    global _pool_entries, _pool_hash, _pool_idx
    tenant = config.TENANT_ID or settings_store.get("TENANT_ID") or ""
    raw_pool: list[dict] = settings_store.get("APP_POOL") or []
    if not raw_pool:
        client = config.CLIENT_ID or settings_store.get("CLIENT_ID") or ""
        secret = config.CLIENT_SECRET or settings_store.get("CLIENT_SECRET") or ""
        if client and secret:
            raw_pool = [{"client_id": client, "client_secret": secret, "label": "App 1"}]
    new_hash = hashlib.md5(
        f"{tenant}:{json.dumps(raw_pool, sort_keys=True)}".encode()
    ).hexdigest()
    if new_hash == _pool_hash:
        return
    new_entries: list[dict] = []
    for entry in raw_pool:
        cid  = entry.get("client_id", "")
        csec = entry.get("client_secret", "")
        if not (cid and csec and tenant):
            continue
        authority = f"https://login.microsoftonline.com/{tenant}"
        msal_app = msal.ConfidentialClientApplication(
            cid, authority=authority, client_credential=csec,
        )
        new_entries.append({
            "client_id": cid,
            "label": entry.get("label", f"App {len(new_entries) + 1}"),
            "msal": msal_app,
        })
    with _pool_lock:
        _pool_entries = new_entries
        _pool_hash = new_hash
        _pool_idx = 0
    log.info("Graph app pool rebuilt: %d app(s)", len(new_entries))
    _restore_from_db(new_entries)


def reset_msal_app() -> None:
    """Force pool rebuild on next call (after credentials change)."""
    global _pool_hash
    _pool_hash = ""


def _record_call(client_id: str) -> None:
    """Stündliche Aufrufstatistik — muss unter _pool_lock aufgerufen werden."""
    now_dt = datetime.datetime.now()
    date_str = now_dt.strftime("%Y-%m-%d")
    hour = now_dt.hour
    s = _call_stats.get(client_id)
    if s is None or s["date"] != date_str:
        s = {"date": date_str, "hours": [0] * 24, "peak_hour": -1, "peak_count": 0}
        _call_stats[client_id] = s
    s["hours"][hour] += 1
    if s["hours"][hour] > s["peak_count"]:
        s["peak_count"] = s["hours"][hour]
        s["peak_hour"] = hour


def mark_throttled(client_id: str, retry_after_s: int) -> None:
    """Markiert einen Pool-Eintrag als gedrosselt bis Retry-After abgelaufen ist."""
    with _pool_lock:
        _throttled_until[client_id] = time.monotonic() + retry_after_s
    log.warning("Graph pool: app '%s' throttled for %ds", client_id[:8], retry_after_s)


def get_pool_status() -> list[dict]:
    """Pool-Einträge ohne Secrets, inkl. Throttle-Status und Aufrufstatistik für Dashboard."""
    _rebuild_pool_if_needed()
    now = time.monotonic()
    now_hour = datetime.datetime.now().hour
    today = datetime.date.today().isoformat()
    with _pool_lock:
        result = []
        for e in _pool_entries:
            cid = e["client_id"]
            s = _call_stats.get(cid)
            if s is None or s["date"] != today:
                hours = [0] * 24
                peak_hour = -1
                peak_count = 0
            else:
                hours = list(s["hours"])
                peak_hour = s["peak_hour"]
                peak_count = s["peak_count"]
            result.append({
                "client_id": cid,
                "label": e["label"],
                "throttled": now < _throttled_until.get(cid, 0),
                "throttled_until_s": max(0.0, _throttled_until.get(cid, 0) - now),
                "calls_this_hour": hours[now_hour],
                "calls_today": sum(hours),
                "peak_hour": peak_hour,
                "peak_count": peak_count,
                "hours_today": hours,
            })
        return result


def _acquire_token() -> str | None:
    global _pool_idx
    _rebuild_pool_if_needed()
    now = time.monotonic()
    with _pool_lock:
        entries = list(_pool_entries)
        if not entries:
            log.warning("Graph credentials not configured — skipping token acquisition")
            return None
        # Bevorzuge nicht-gedrosselte Einträge; Round-Robin innerhalb der freien Apps
        free = [e for e in entries if now >= _throttled_until.get(e["client_id"], 0)]
        if free:
            entry = free[_pool_idx % len(free)]
        else:
            # Alle gedrosselt — nimm den mit der kürzesten Restzeit
            entry = min(entries, key=lambda e: _throttled_until.get(e["client_id"], 0))
            log.warning("Graph pool: all %d app(s) throttled, using earliest ('%s')",
                        len(entries), entry["label"])
        _pool_idx += 1
        global _last_used_client_id
        _last_used_client_id = entry["client_id"]
        _record_call(entry["client_id"])
    result = entry["msal"].acquire_token_silent(_SCOPES, account=None)
    if not result:
        result = entry["msal"].acquire_token_for_client(scopes=_SCOPES)
    if "access_token" in result:
        log.debug("Graph token acquired from pool entry '%s'", entry["label"])
        return result["access_token"]
    log.error("Failed to acquire Graph token from '%s': %s",
              entry["label"], result.get("error_description"))
    return None


async def _acquire_token_async() -> str | None:
    """Non-blocking token acquisition for async callers.

    MSAL makes synchronous HTTP calls to Microsoft Identity which can block
    the asyncio event loop for 50–200ms (longer on token cache miss).
    Running in an executor keeps the loop free for ACME polling and other tasks.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _acquire_token)


def cached_token_valid() -> bool:
    """True, wenn ein GÜLTIGES Graph-Token im MSAL-Cache liegt — OHNE Netzaufruf.

    Für `/health`: `acquire_token_silent` liefert bei Client-Credentials nur aus
    dem Cache; ist keins da oder abgelaufen (kein Refresh-Token in diesem Fluss),
    kommt None zurück, ohne dass ein neues geholt wird. Der Gesundheitscheck löst
    also nie einen Token-Abruf aus. Kann direkt nach dem Start (leerer Cache, noch
    kein Graph-Aufruf) False liefern — das Feld ist bewusst nur informativ und
    steuert den Bypass NICHT (siehe /health-Status: nur der SMTP-Listener zählt).
    """
    try:
        app = _get_msal_app()
        if app is None:
            return False
        result = app.acquire_token_silent(_SCOPES, account=None)
        return bool(result and "access_token" in result)
    except Exception:                                       # noqa: BLE001
        return False


def _get_msal_app():
    """Return the first available MSAL ConfidentialClientApplication from the pool.

    Used by keyvault.py and smtp_submit.py for non-Graph scopes (vault.azure.net,
    management.azure.com, outlook.office365.com).
    """
    _rebuild_pool_if_needed()
    now = time.monotonic()
    with _pool_lock:
        if not _pool_entries:
            return None
        free = [e for e in _pool_entries if now >= _throttled_until.get(e["client_id"], 0)]
        entry = (free[0] if free else _pool_entries[0])
        return entry["msal"]


def _get_effective_credentials() -> tuple[str, str, str]:
    """Return (tenant_id, client_id, client_secret) of the first pool entry."""
    _rebuild_pool_if_needed()
    tenant = config.TENANT_ID or settings_store.get("TENANT_ID") or ""
    raw_pool: list[dict] = settings_store.get("APP_POOL") or []
    with _pool_lock:
        if _pool_entries:
            cid = _pool_entries[0]["client_id"]
            secret = next(
                (e.get("client_secret", "") for e in raw_pool if e.get("client_id") == cid),
                config.CLIENT_SECRET or settings_store.get("CLIENT_SECRET") or "",
            )
            return tenant, cid, secret
    client_id = config.CLIENT_ID or settings_store.get("CLIENT_ID") or ""
    client_secret = config.CLIENT_SECRET or settings_store.get("CLIENT_SECRET") or ""
    return tenant, client_id, client_secret


@dataclass
class UserData:
    displayName: str = ""
    jobTitle: str = ""
    department: str = ""
    companyName: str = ""
    mail: str = ""
    mobilePhone: str = ""
    phone: str = ""
    officeLocation: str = ""
    streetAddress: str = ""
    postalCode: str = ""
    city: str = ""
    state: str = ""
    country: str = ""
    website: str = ""
    bookingsUrl: str = ""
    custom: dict = None  # populated from CUSTOM_TEMPLATE_VARS

    def __post_init__(self):
        if self.custom is None:
            self.custom = {}


def _adressen(liste) -> set[str]:
    return {((x.get("emailAddress") or {}).get("address") or "").lower()
            for x in (liste or [])} - {""}


def _beantwortete(nachrichten: list[dict], postfach: str, art: str, an: list[str],
                  cc: list[str] | None = None) -> dict | None:
    """Welche Nachricht der Konversation beantwortet bzw. weitergeleitet wird.

    Wo Outlook es dem Add-in nicht sagt (inReplyTo fehlt, etwa auf iOS), wird
    geschätzt: Für jede Nachricht — auch die eigenen — ergibt sich, wen Outlook
    bei „Antworten“ und „Allen antworten“ einträgt. Die jüngste, deren
    Ergebnis An (und, falls bekannt, Cc) des Entwurfs genau trifft, gewinnt.
    Antwort auf die eigene Mail: An = ihr An, Cc = ihr Cc; auf eine fremde:
    An = Absender (+ ihr An), Cc = ihr Cc — jeweils ohne einen selbst.
    Passt keine (Empfänger geändert), die jüngste des ersten Empfängers, dann
    die jüngste fremde, zuletzt die jüngste (VM 20.10.2026: Antwort auf die
    eigene Mail an Stocker wurde als Antwort auf Stockers ältere Mail
    geschätzt)."""
    selbst = postfach.lower()
    echt = [m for m in nachrichten if not m.get("isDraft") and m.get("internetMessageId")]
    echt.sort(key=lambda m: m.get("receivedDateTime") or "", reverse=True)

    def von(m):
        return (((m.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()

    an_menge = {x.lower() for x in (an or [])}
    cc_menge = None if cc is None else {x.lower() for x in cc}
    if art == "reply" and an_menge:
        for m in echt:
            zu, kopie = _adressen(m.get("toRecipients")), _adressen(m.get("ccRecipients"))
            if von(m) == selbst:
                moeglich = [(zu, kopie), (zu, set())]
            else:
                moeglich = [({von(m)}, set()), (({von(m)} | zu) - {selbst}, kopie - {selbst})]
            if any(a == an_menge and (cc_menge is None or k == cc_menge) for a, k in moeglich):
                return m
    fremd = [m for m in echt if von(m) != selbst]
    an = [x.lower() for x in (an or [])]
    if art == "reply" and an:
        for kandidat in ([an[0]], an):
            treffer = [m for m in fremd if von(m) in kandidat]
            if treffer:
                return treffer[0]
    return (fremd or echt or [None])[0]


async def antwort_bezug(postfach: str, konversation: str, art: str = "reply",
                        an: list[str] | None = None, cc: list[str] | None = None) -> dict | None:
    """Kette der beantworteten Mail — dieselben Angaben, die handler.py beim
    Versand aus der Antwort liest: `kennungen` (ihre Message-ID plus ihre
    References/In-Reply-To = die References der Antwort) und `marker` (eine
    Gateway-Signatur in ihrem Text = im Zitat der Antwort).

    ⚠️ Nicht die ganze Konversation: Sie enthält auch Zweige, die in der
    beantworteten Mail nicht vorkommen. Wer auf eine ältere Mail antwortet,
    bekäme sonst die Antwort-Signatur, obwohl die Empfänger die volle nie
    gesehen haben (VM 19.10.2026, „Allen antworten“ auf eine Mail vor der
    eigenen signierten Antwort). `None` = nicht ermittelbar."""
    import httpx
    from urllib.parse import quote
    tok = await _acquire_token_async()
    if not tok or not konversation:
        return None
    filt = "conversationId eq '" + konversation.replace("'", "''") + "'"
    url = (f"https://graph.microsoft.com/v1.0/users/{quote(postfach)}/messages"
           f"?$filter={quote(filt)}&$top=50&$select={_BEZUG_FELDER}")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {tok}"})
    except Exception as exc:                                   # noqa: BLE001
        log.warning("antwort_bezug %s: %s", postfach, exc)
        return None
    if resp.status_code != 200:
        log.warning("antwort_bezug %s: Graph %s %s", postfach, resp.status_code, resp.text[:200])
        return None
    return _bezug_aus(_beantwortete(resp.json().get("value", []), postfach, art, an or [], cc))


_BEZUG_FELDER = ("internetMessageId,isDraft,from,toRecipients,ccRecipients,receivedDateTime,"
                 "internetMessageHeaders,body")


def _bezug_aus(m: dict | None) -> dict | None:
    """Kennungen, Text (HTML), Absender und Zeit einer Nachricht (siehe antwort_bezug).
    `kennungen[0]` ist die eigene Message-ID der Nachricht."""
    import email.message
    import sig_thread
    if not m:
        return None
    kopf = email.message.Message()
    for h in m.get("internetMessageHeaders") or []:
        if (h.get("name") or "").lower() in ("references", "in-reply-to"):
            kopf[h["name"]] = h.get("value") or ""
    return {"kennungen": [m["internetMessageId"]] + sig_thread.kennungen(kopf),
            "html": (m.get("body") or {}).get("content") or "",
            "von": (((m.get("from") or {}).get("emailAddress") or {}).get("address") or ""),
            "zeit": m.get("receivedDateTime") or ""}


async def bezug_per_kennung(postfach: str, message_id: str) -> dict | None:
    """Wie antwort_bezug, aber für die GENAU bekannte beantwortete Mail —
    `item.inReplyTo` (Office.js, Mailbox 1.14) nennt ihre Message-ID. Keine
    Schätzung. `None` = nicht im Postfach gefunden oder Graph-Fehler."""
    import httpx
    from urllib.parse import quote
    tok = await _acquire_token_async()
    if not tok or not message_id:
        return None
    filt = "internetMessageId eq '" + message_id.replace("'", "''") + "'"
    url = (f"https://graph.microsoft.com/v1.0/users/{quote(postfach)}/messages"
           f"?$filter={quote(filt)}&$top=5&$select={_BEZUG_FELDER}")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {tok}"})
    except Exception as exc:                                   # noqa: BLE001
        log.warning("bezug_per_kennung %s: %s", postfach, exc)
        return None
    if resp.status_code != 200:
        log.warning("bezug_per_kennung %s: Graph %s %s", postfach, resp.status_code, resp.text[:200])
        return None
    # Dieselbe Mail liegt ggf. mehrfach vor (Posteingang + Kopie) — gleiche Kette.
    echt = [m for m in resp.json().get("value", []) if not m.get("isDraft")]
    return _bezug_aus(echt[0] if echt else None)


_BETREFF_PRAEFIX = re.compile(
    r"^\s*(?:(?:re|aw|wg|fw|fwd|antw|antwort|wtr|tr|sv|vs|rv|enc)\s*(?:\[\d+\])?\s*:\s*)+",
    re.IGNORECASE)


def betreff_kern(betreff: str) -> str:
    """Betreff ohne Antwort-/Weiterleitungs-Vorsätze (AW:, Re:, WG:, Fwd: …),
    Leerraum zusammengefasst, Gross/klein egal — zum Vergleich über Sprachen."""
    kern = _BETREFF_PRAEFIX.sub("", betreff or "")
    return " ".join(kern.split()).casefold()


# Nur schlichte Adressen gehen in einen Graph-Filter — kein Anführungszeichen,
# keine Klammer, nichts, was die OData-Abfrage verlassen könnte.
_FILTER_ADRESSE_RE = re.compile(r"[a-z0-9._%+-]{1,64}@[a-z0-9-]+(\.[a-z0-9-]+)+")


def _betreff_treffer(nachrichten: list, kern: str) -> list:
    return [m for m in nachrichten
            if not m.get("isDraft") and m.get("conversationId")
            and betreff_kern(m.get("subject") or "") == kern]


async def konversation_per_betreff(postfach: str, betreff: str,
                                   empfaenger: list[str] | None = None) -> str | None:
    """`conversationId` der jüngsten Mail im Postfach mit demselben Betreff-Kern.

    Rückfall für Outlook auf iOS: Im Antwortentwurf ist `item.conversationId`
    dort leer (VM 19.10.2026, drei Antworten ohne Abruf). Zwei Stufen:

    1. Die letzten 100 Nachrichten des Postfachs. Unter mehreren Treffern
       gewinnt eine Mail, deren Absender unter den Empfängern des Entwurfs
       steht (bei einer Antwort der ursprüngliche Absender), sonst die jüngste.
    2. Nur wenn Stufe 1 gar keinen Treffer hat: je Empfänger die letzten 50
       Mails VON dieser Adresse (Graph-Filter). Die 100 reichten auf der VM
       am 20.10.2026 nur zwei Tage zurück; die beantwortete Mail vom 17.10.
       lag knapp ausserhalb. Stufe 2 greift bewusst nicht, wenn Stufe 1 etwas
       fand — sonst gewönne bei einer Antwort auf die eigene, neue Mail eine
       alte gleichnamige Mail des Empfängers."""
    import httpx
    from urllib.parse import quote
    kern = betreff_kern(betreff)
    tok = await _acquire_token_async()
    if not tok or not kern:
        return None
    kopf = {"Authorization": f"Bearer {tok}"}
    basis = f"https://graph.microsoft.com/v1.0/users/{quote(postfach)}/messages"
    felder = "subject,conversationId,from,isDraft,receivedDateTime"

    async def holen(client, url: str) -> list:
        try:
            resp = await client.get(url, headers=kopf)
        except Exception as exc:                               # noqa: BLE001
            log.warning("konversation_per_betreff %s: %s", postfach, exc)
            return []
        if resp.status_code != 200:
            log.warning("konversation_per_betreff %s: Graph %s %s", postfach,
                        resp.status_code, resp.text[:200])
            return []
        return resp.json().get("value", [])

    an = [e.lower() for e in (empfaenger or [])]
    async with httpx.AsyncClient(timeout=10) as client:
        treffer = _betreff_treffer(await holen(
            client, f"{basis}?$top=100&$select={felder}&$orderby=receivedDateTime%20desc"), kern)
        if treffer:
            passend = [m for m in treffer
                       if (((m.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower() in an]
            return (passend or treffer)[0]["conversationId"]
        # Stufe 2. Graph nimmt $orderby neben $filter nur, wenn das
        # Sortierfeld zuerst im Filter steht — daher die receivedDateTime-Klausel.
        adressen = [a for a in dict.fromkeys(an)
                    if a != postfach.lower() and _FILTER_ADRESSE_RE.fullmatch(a)][:5]
        listen = await asyncio.gather(*(holen(client, (
            f"{basis}?$top=50&$select={felder}&$orderby=receivedDateTime%20desc&$filter="
            + quote(f"receivedDateTime ge 1900-01-01T00:00:00Z and from/emailAddress/address eq '{a}'")))
            for a in adressen))
    treffer = sorted((m for liste in listen for m in _betreff_treffer(liste, kern)),
                     key=lambda m: m.get("receivedDateTime") or "", reverse=True)
    if treffer:
        log.info("konversation_per_betreff %s: nicht unter den letzten 100, über den Absender "
                 "gefunden (%s)", postfach, (treffer[0].get("receivedDateTime") or "")[:16])
        return treffer[0]["conversationId"]
    return None


async def update_sent_item(sender_email: str, message_id: str, html_body: str) -> bool | None:
    """Find a sent message by Message-ID and patch its HTML body."""
    token = await _acquire_token_async()
    if not token:
        return False

    mid = message_id.strip()
    if not mid.startswith("<"):
        mid = f"<{mid}>"
    search_filter = urllib.parse.quote("internetMessageId eq '" + mid.replace("'", "''") + "'")
    search_url = (
        f"https://graph.microsoft.com/v1.0/users/{sender_email}"
        f"/mailFolders/sentitems/messages?$filter={search_filter}&$select=id&$top=1"
    )
    auth = {"Authorization": f"Bearer {token}"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(search_url, headers=auth)
            resp.raise_for_status()
            items = resp.json().get("value", [])

        if not items:
            return False

        msg_graph_id = items[0]["id"]
        patch_url = f"https://graph.microsoft.com/v1.0/users/{sender_email}/messages/{msg_graph_id}"

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(
                patch_url,
                headers={**auth, "Content-Type": "application/json"},
                json={"body": {"contentType": "html", "content": html_body}},
            )
            if 400 <= resp.status_code < 500:
                log.warning(
                    "Sent item PATCH %d for %s (not retrying): %s",
                    resp.status_code, sender_email, resp.text[:300],
                )
                return None  # permanent client error — caller must not retry
            resp.raise_for_status()

        log.info("Sent item updated for %s (message-id %s)", sender_email, message_id)
        return True

    except Exception as exc:
        log.error("update_sent_item failed for %s: %s", sender_email, exc)
        return False


async def cleanup_sent_items(
    sender_email: str, message_id: str, html_body: str,
    subject: str = "", to_recipients: list[str] | None = None,
    replace_all: bool = False,
    nur_duplikate: bool = False,
) -> bool | None:
    """Find all Sent Items with this Message-ID and clean up duplicates.

    Exchange creates a Sent Item when the user sends mail AND another when our
    gateway calls sendMail.

    replace_all=False (signed mail):
      - Multiple items: delete originals, keep sendMail copy (already readable).
      - Single item: patch body (SMTP mode or timing edge case → caller retries).

    replace_all=True (encrypted mail):
      - Multiple items: delete ALL, create a fresh readable JSON Sent Item.
      - Single item: return False so caller retries once the sendMail copy appears.

    nur_duplikate=True (unveränderte Post, die nur des Zustellwegs wegen über
    sendMail lief — z.B. rein interne Mail im Modus Graph++):
      - Mehrere Elemente: Originale löschen, die sendMail-Kopie behalten. Beide
        sind inhaltsgleich, es gibt nichts zu patchen.
      - Ein Element: NICHT patchen und NICHT fertig melden (False) — die
        sendMail-Kopie kann noch kommen. Kommt sie nie, bleibt das Original
        unangetastet stehen.

    Returns True on success, None on permanent 4xx, False on transient/not-found.
    """
    token = await _acquire_token_async()
    if not token:
        return False

    mid = message_id.strip()
    if not mid.startswith("<"):
        mid = f"<{mid}>"
    search_filter = urllib.parse.quote("internetMessageId eq '" + mid.replace("'", "''") + "'")
    search_url = (
        f"https://graph.microsoft.com/v1.0/users/{sender_email}"
        f"/mailFolders/sentitems/messages"
        f"?$filter={search_filter}&$select=id,createdDateTime&$top=10"
    )
    auth = {"Authorization": f"Bearer {token}"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(search_url, headers=auth)
            resp.raise_for_status()
            items = resp.json().get("value", [])

        if not items:
            return False

        # Sort oldest-first; createdDateTime is ISO 8601 so lexicographic order works
        items.sort(key=lambda x: x.get("createdDateTime", ""))

        # Patch helper — patches body (and optionally subject) of a Sent Item.
        # Returns True on success, None on permanent 4xx.
        async def _patch_item(item_id: str, patch_subject: str = "") -> bool | None:
            patch_data: dict = {"body": {"contentType": "html", "content": html_body}}
            if patch_subject:
                patch_data["subject"] = patch_subject
            url = (
                f"https://graph.microsoft.com/v1.0/users/{sender_email}/messages/{item_id}"
            )
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.patch(
                    url,
                    headers={**auth, "Content-Type": "application/json"},
                    json=patch_data,
                )
            if 400 <= r.status_code < 500:
                log.warning(
                    "Sent item PATCH %d for %s (not retrying): %s",
                    r.status_code, sender_email, r.text[:300],
                )
                return None
            r.raise_for_status()
            return True

        if len(items) == 1:
            if nur_duplikate:
                return False      # sendMail-Kopie noch nicht da — weiter warten
            if replace_all:
                # Encrypted: patch the single item immediately so it's readable,
                # then keep retrying — the sendMail copy (encrypted) will appear soon.
                r = await _patch_item(items[0]["id"], patch_subject=subject)
                if r is None:
                    return None
                log.debug(
                    "Sent item cleanup: patched 1 item for %s — retrying for sendMail copy",
                    sender_email,
                )
                return False  # caller retries to catch & delete the sendMail copy
            # Signed mail: patch the single item and we're done.
            # patch_subject MUSS mit: bei Betreff-Triggern (#nosig/#nodigsig/#enc)
            # hat der Handler den Betreff bereinigt und reicht ihn als `subject`
            # durch — ohne diesen Parameter behielt das Sent Item den Roh-Betreff
            # samt Schlüsselwörtern (der Verschlüsselungspfad gab ihn schon mit).
            r = await _patch_item(items[0]["id"], patch_subject=subject)
            if r is None:
                return None
            log.info("Sent item patched for %s (message-id %s)", sender_email, message_id)
            return True

        if replace_all:
            # Encrypted mail, multiple items: the OLDEST item is the Outlook-created
            # Sent Item (not a draft, MAPI-native → PATCH works correctly). The newer
            # item(s) are encrypted sendMail copies. Strategy: patch the oldest to make
            # it readable, delete all newer encrypted copies.
            oldest_id = items[0]["id"]
            r = await _patch_item(oldest_id, patch_subject=subject)
            if r is None:
                return None
            log.info(
                "Sent item patched (oldest) for %s (message-id %s)", sender_email, message_id
            )
            async with httpx.AsyncClient(timeout=30) as client:
                for item in items[1:]:
                    del_url = (
                        f"https://graph.microsoft.com/v1.0/users/{sender_email}"
                        f"/messages/{item['id']}"
                    )
                    d = await client.delete(del_url, headers=auth)
                    if d.is_success or d.status_code == 404:
                        log.info(
                            "Deleted encrypted sendMail copy for %s (created %s)",
                            sender_email, item.get("createdDateTime", "?"),
                        )
                    else:
                        log.warning(
                            "Failed to delete Sent Item for %s: HTTP %d %s",
                            sender_email, d.status_code, d.text[:200],
                        )
            return True

        # Signed mail, multiple items: delete originals, keep sendMail copy.
        to_delete = items[:-1]
        async with httpx.AsyncClient(timeout=30) as client:
            for item in to_delete:
                del_url = (
                    f"https://graph.microsoft.com/v1.0/users/{sender_email}"
                    f"/messages/{item['id']}"
                )
                d = await client.delete(del_url, headers=auth)
                if d.is_success or d.status_code == 404:
                    log.info(
                        "Deleted original Sent Item for %s (created %s)",
                        sender_email, item.get("createdDateTime", "?"),
                    )
                else:
                    log.warning(
                        "Failed to delete Sent Item for %s: HTTP %d %s",
                        sender_email, d.status_code, d.text[:200],
                    )
        log.info(
            "Sent items cleaned for %s: %d original(s) removed", sender_email, len(to_delete)
        )
        return True

    except Exception as exc:
        log.error("cleanup_sent_items failed for %s: %s", sender_email, exc)
        return False


GRAPH = "https://graph.microsoft.com/v1.0"



def _resolve_entra_field(field: str, data: dict, ext: dict, phones: list) -> str:
    """Resolve a single Entra field name to its string value from the API response."""
    if field == "businessPhones[0]":
        return phones[0] if phones else ""
    if field.startswith("extensionAttribute"):
        return ext.get(field) or ""
    return str(data.get(field) or "")


async def _graph_user_data(email: str) -> dict | None:
    """Rohes Graph-Nutzerprofil — der EINZIGE Graph-Call in get_user —, prozessweit
    per TTL gecacht. None bei fehlendem Token / 404 / Fehler (NICHT gecacht, damit
    transiente Fehler erneut versucht werden)."""
    schluessel = (email or "").lower()
    now = time.monotonic()
    treffer = _user_data_cache.get(schluessel)
    if treffer and treffer[1] > now:
        return treffer[0]

    token = await _acquire_token_async()
    if not token:
        return None
    url = f"https://graph.microsoft.com/v1.0/users/{email}?$select={_build_select_fields()}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code == 404:
            log.warning("Graph: user not found: %s", email)
            return None
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.error("Graph API error for %s: %s", email, exc)
        return None
    _user_data_cache[schluessel] = (data, now + _USER_DATA_TTL)
    return data


async def get_user(email: str) -> UserData:
    data = await _graph_user_data(email)
    if data is None:
        return UserData(mail=email)

    ext = data.get("onPremisesExtensionAttributes") or {}
    phones = data.get("businessPhones") or []
    resolved_mail = data.get("mail") or email

    user_websites: dict = settings_store.get("USER_WEBSITES") or {}
    user_bookings: dict = settings_store.get("USER_BOOKINGS") or {}
    user_overrides: dict = (settings_store.get("USER_OVERRIDES") or {}).get(resolved_mail.lower(), {})

    custom_vars: list[dict] = settings_store.get("CUSTOM_TEMPLATE_VARS") or []
    custom: dict = {
        cv["name"]: _resolve_entra_field(cv["entra_field"], data, ext, phones)
        for cv in custom_vars
        if cv.get("name") and cv.get("entra_field")
    }
    # Gruppen-Ebene: Variablenwerte, die den Gruppen des Absenders zugewiesen sind
    # (z.B. Vertreter je Team). Rang: Entra-Feld (oben) < Gruppe < Postfach-Override
    # (die USER_OVERRIDES-Schleife unten schlägt beide). Für Variablen OHNE
    # Entra-Mapping (rein manuell, wie vertreter_*) ist die Gruppe die Quelle.
    try:
        import policies as _pol
        for _vn, _vv in _pol.group_vars_for(resolved_mail).items():
            custom[_vn] = _vv
    except Exception as _exc:                                       # noqa: BLE001
        log.debug("group vars resolution skipped: %s", _exc)

    ud = UserData(
        displayName=user_overrides.get("user.displayName") or data.get("displayName") or "",
        jobTitle=user_overrides.get("user.jobTitle") or data.get("jobTitle") or "",
        department=user_overrides.get("user.department") or data.get("department") or "",
        companyName=user_overrides.get("user.companyName") or data.get("companyName") or "",
        mail=resolved_mail,
        mobilePhone=user_overrides.get("user.mobilePhone") or data.get("mobilePhone") or "",
        phone=user_overrides.get("user.phone") or (phones[0] if phones else ""),
        officeLocation=user_overrides.get("user.officeLocation") or data.get("officeLocation") or "",
        streetAddress=user_overrides.get("user.streetAddress") or data.get("streetAddress") or "",
        postalCode=user_overrides.get("user.postalCode") or data.get("postalCode") or "",
        city=user_overrides.get("user.city") or data.get("city") or "",
        state=user_overrides.get("user.state") or data.get("state") or "",
        country=user_overrides.get("user.country") or data.get("country") or "",
        website=user_overrides.get("user.website") or user_websites.get(resolved_mail.lower()) or settings_store.get("WEBSITE_URL") or "",
        bookingsUrl=user_overrides.get("user.bookingsUrl") or user_bookings.get(resolved_mail.lower()) or "",
        custom=custom,
    )
    for key, val in user_overrides.items():
        if key.startswith("custom.") and val:
            ud.custom[key[7:]] = val
    return ud
