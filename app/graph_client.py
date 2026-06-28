import asyncio
import datetime
import hashlib
import json
import logging
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
_SELECT_FIELDS = ",".join([
    "displayName",
    "jobTitle",
    "department",
    "companyName",
    "mail",
    "mobilePhone",
    "businessPhones",
    "officeLocation",
    "businessHomePage",
    "onPremisesExtensionAttributes",
])

_pool_entries: list[dict] = []   # [{client_id, label, msal}]
_pool_lock = threading.Lock()
_pool_idx: int = 0
_pool_hash: str = ""
_throttled_until: dict[str, float] = {}   # client_id → monotonic timestamp
_last_used_client_id: str = ""            # für mark_throttled nach 429
_call_stats: dict[str, dict] = {}        # client_id → {date, hours[24], peak_hour, peak_count}


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
    website: str = ""
    bookingsUrl: str = ""


async def update_sent_item(sender_email: str, message_id: str, html_body: str) -> bool | None:
    """Find a sent message by Message-ID and patch its HTML body."""
    token = await _acquire_token_async()
    if not token:
        return False

    mid = message_id.strip()
    if not mid.startswith("<"):
        mid = f"<{mid}>"
    search_filter = urllib.parse.quote(f"internetMessageId eq '{mid}'")
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
    sender_email: str, message_id: str, html_body: str
) -> bool | None:
    """Find all Sent Items with this Message-ID and clean up duplicates.

    Exchange creates a Sent Item when the user sends mail AND another when our
    gateway calls sendMail.  This function:

    - If multiple items found: deletes all but the newest (the one from our
      sendMail, which already has the signed MIME body).
    - If only one item found: patches it with html_body (covers SMTP reinject
      mode where sendMail never runs, and the early-timing edge case in Graph
      mode before the sendMail Sent Item has appeared — the caller retries with
      back-off so the next pass can still delete it once it shows up).

    Returns True on success, None on a permanent 4xx client error, False on
    transient failure / not-found (caller should retry).
    """
    token = await _acquire_token_async()
    if not token:
        return False

    mid = message_id.strip()
    if not mid.startswith("<"):
        mid = f"<{mid}>"
    search_filter = urllib.parse.quote(f"internetMessageId eq '{mid}'")
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

        if len(items) == 1:
            # Single item: patch its body (SMTP reinject mode, or Graph mode where
            # the sendMail Sent Item hasn't appeared yet → caller retries)
            item_id = items[0]["id"]
            patch_url = (
                f"https://graph.microsoft.com/v1.0/users/{sender_email}/messages/{item_id}"
            )
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
                    return None
                resp.raise_for_status()
            log.info("Sent item patched for %s (message-id %s)", sender_email, message_id)
            return True

        # Multiple items: the oldest are the original(s) from the mail client;
        # the newest is from our sendMail. Delete the older duplicates, then
        # patch the newest with html_body so encrypted Sent Items stay readable.
        to_delete = items[:-1]
        newest_id = items[-1]["id"]
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
        patch_url = (
            f"https://graph.microsoft.com/v1.0/users/{sender_email}/messages/{newest_id}"
        )
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
                return None
            resp.raise_for_status()
        log.info("Sent item patched for %s (message-id %s)", sender_email, message_id)
        return True

    except Exception as exc:
        log.error("cleanup_sent_items failed for %s: %s", sender_email, exc)
        return False


GRAPH = "https://graph.microsoft.com/v1.0"


_sender_mb_cache: list[dict] = []
_sender_mb_cache_ts: float = 0.0
_SENDER_MB_TTL = 3600.0  # 1 Stunde


def invalidate_sender_mailboxes_cache() -> None:
    global _sender_mb_cache_ts
    _sender_mb_cache_ts = 0.0


async def _verify_mailboxes_batch(
    client: httpx.AsyncClient, headers: dict, candidates: list[dict]
) -> list[dict]:
    """Prüft via /$batch ob Kandidaten ein echtes EXO-Postfach haben."""
    BATCH = "https://graph.microsoft.com/v1.0/$batch"
    verified: list[dict] = []
    for chunk_start in range(0, len(candidates), 20):
        chunk = candidates[chunk_start:chunk_start + 20]
        batch_body = {
            "requests": [
                {"id": str(i), "method": "GET",
                 "url": f"/users/{c['email']}/mailFolders/inbox?$select=id"}
                for i, c in enumerate(chunk)
            ]
        }
        r = await client.post(BATCH, json=batch_body, headers=headers)
        if r.status_code != 200:
            log.warning("mailbox inbox batch failed: %s", r.status_code)
            # Fallback: alle Kandidaten dieses Chunks übernehmen
            verified.extend(chunk)
            continue
        id_to_status = {resp["id"]: resp["status"]
                        for resp in r.json().get("responses", [])}
        for i, c in enumerate(chunk):
            if id_to_status.get(str(i)) == 200:
                verified.append(c)
    return verified


async def list_sender_mailboxes() -> list[dict]:
    """User- und Shared-Mailboxen mit echtem EXO-Postfach (via mailFolders/inbox-Batch).

    Ergebnis wird 1 Stunde gecacht. invalidate_sender_mailboxes_cache() erzwingt Neuladen.
    """
    global _sender_mb_cache, _sender_mb_cache_ts
    if _sender_mb_cache and (time.monotonic() - _sender_mb_cache_ts) < _SENDER_MB_TTL:
        return _sender_mb_cache

    token = await _acquire_token_async()
    if not token:
        return []
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    candidates: list[dict] = []
    url = (f"{GRAPH}/users"
           "?$filter=accountEnabled eq true"
           "&$select=mail,displayName,assignedLicenses,userType,proxyAddresses"
           "&$top=999")
    async with httpx.AsyncClient(timeout=30) as client:
        while url:
            r = await client.get(url, headers=headers)
            if r.status_code != 200:
                log.warning("list_sender_mailboxes: /users returned %s", r.status_code)
                break
            data = r.json()
            for u in data.get("value", []):
                mail = (u.get("mail") or "").lower().strip()
                if not mail or u.get("userType") == "Guest":
                    continue
                proxy = u.get("proxyAddresses") or []
                if not any(".onmicrosoft.com" in p.lower() for p in proxy):
                    continue
                has_license = bool(u.get("assignedLicenses"))
                candidates.append({
                    "email": mail,
                    "name": u.get("displayName") or mail,
                    "type": "user" if has_license else "shared",
                })
            url = data.get("@odata.nextLink")

        verified = await _verify_mailboxes_batch(client, headers, candidates)

    result = sorted(verified, key=lambda x: x["email"])
    _sender_mb_cache = result
    _sender_mb_cache_ts = time.monotonic()
    log.info("list_sender_mailboxes: %d mailboxes cached", len(result))
    return result


async def list_mailboxes() -> list[dict]:
    """
    List all EXO mailboxes via Graph API: licensed users, shared mailboxes,
    and Microsoft 365 group mailboxes.
    Returns list of {"email", "name", "type"} dicts sorted by email.
    """
    token = await _acquire_token_async()
    if not token:
        return []
    headers = {"Authorization": f"Bearer {token}"}
    results: list[dict] = []

    async with httpx.AsyncClient(timeout=30) as client:
        # ── User mailboxes (licensed = regular, unlicensed with mail = shared) ──
        url = (f"{GRAPH}/users"
               "?$select=mail,displayName,assignedLicenses,userType"
               "&$top=999")
        while url:
            r = await client.get(url, headers=headers)
            if r.status_code != 200:
                log.warning("list_mailboxes: /users returned %s", r.status_code)
                break
            data = r.json()
            for u in data.get("value", []):
                mail = (u.get("mail") or "").lower().strip()
                if not mail or u.get("userType") == "Guest":
                    continue
                has_license = bool(u.get("assignedLicenses"))
                results.append({
                    "email": mail,
                    "name": u.get("displayName") or mail,
                    "type": "user" if has_license else "shared",
                })
            url = data.get("@odata.nextLink")

        # ── Microsoft 365 group mailboxes ──────────────────────────────────────
        grp_url = (f"{GRAPH}/groups"
                   "?$filter=groupTypes/any(c:c+eq+'Unified')"
                   "&$select=mail,displayName"
                   "&$top=999")
        while grp_url:
            r = await client.get(grp_url, headers=headers)
            if r.status_code != 200:
                log.debug("list_mailboxes: /groups returned %s (Group.Read.All may be missing)",
                          r.status_code)
                break
            data = r.json()
            for g in data.get("value", []):
                mail = (g.get("mail") or "").lower().strip()
                if not mail:
                    continue
                results.append({
                    "email": mail,
                    "name": g.get("displayName") or mail,
                    "type": "group",
                })
            grp_url = data.get("@odata.nextLink")

    return sorted(results, key=lambda x: x["email"])


async def get_user(email: str) -> UserData:
    token = await _acquire_token_async()
    if not token:
        return UserData(mail=email)

    url = f"https://graph.microsoft.com/v1.0/users/{email}?$select={_SELECT_FIELDS}"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)

        if resp.status_code == 404:
            log.warning("Graph: user not found: %s", email)
            return UserData(mail=email)

        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.error("Graph API error for %s: %s", email, exc)
        return UserData(mail=email)

    ext = data.get("onPremisesExtensionAttributes") or {}
    phones = data.get("businessPhones") or []
    resolved_mail = data.get("mail") or email

    user_websites: dict = settings_store.get("USER_WEBSITES") or {}
    user_bookings: dict = settings_store.get("USER_BOOKINGS") or {}

    return UserData(
        displayName=data.get("displayName") or "",
        jobTitle=data.get("jobTitle") or "",
        department=data.get("department") or "",
        companyName=data.get("companyName") or "",
        mail=resolved_mail,
        mobilePhone=data.get("mobilePhone") or "",
        phone=phones[0] if phones else "",
        officeLocation=data.get("officeLocation") or "",
        website=user_websites.get(resolved_mail.lower()) or data.get("businessHomePage") or ext.get("extensionAttribute1") or "",
        bookingsUrl=user_bookings.get(resolved_mail.lower()) or ext.get("extensionAttribute2") or "",
    )
