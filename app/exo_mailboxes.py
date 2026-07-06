"""Authoritative mailbox enumeration + identity resolution via Exchange Online.

EXO is the source of truth for *which real mailboxes exist* and their stable
**ExchangeGuid**. Graph /users only gives a fuzzy answer (guests, unlicensed,
contacts mixed in) and needs inbox-probing to confirm a real mailbox — EXO's
Get-EXOMailbox answers it directly and correctly.

The ExchangeGuid survives rename / primary-SMTP change, so it is the stable
anchor for mailbox-scoped config (see the MAILBOX_CONFIG GUID refactor).

COST: establishing an EXO PowerShell session takes seconds. NEVER call this on
the SMTP hot-path or in a synchronous UI request — use the cached result
(refreshed periodically / on demand) and match locally.
"""
import json
import logging
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import settings_store

log = logging.getLogger("exo_mailboxes")

_AUTH_CERT_PATH = Path("/app/data/auth.pfx")
_TTL = 3600                       # cache lifetime (s)
_lock = threading.RLock()
_cache: list[dict] = []
_cache_ts: float = 0.0


def _norm_addresses(raw) -> list[str]:
    """EmailAddresses is a list — but ConvertTo-Json collapses a single entry to
    a bare string. Normalize to a deduped list of lower-cased SMTP addresses
    (drop the smtp:/SMTP: prefix; ignore non-SMTP proxies like SIP:/X500:)."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    seen: set[str] = set()
    out: list[str] = []
    for a in raw:
        s = str(a)
        if s[:5].lower() == "smtp:":
            addr = s[5:].strip().lower()
            if addr and addr not in seen:
                seen.add(addr)
                out.append(addr)
    return out


def _parse_mailboxes(raw_json: str) -> list[dict]:
    """Parse Get-EXOMailbox JSON → normalized records. Pure & testable."""
    try:
        data = json.loads(raw_json)
    except Exception as exc:
        log.warning("EXO mailbox JSON parse failed: %s", exc)
        return []
    if isinstance(data, dict):    # a single mailbox → ConvertTo-Json emits an object
        data = [data]
    out: list[dict] = []
    for m in data:
        guid = (m.get("guid") or "").strip().lower()
        if not guid:
            continue
        out.append({
            "guid": guid,
            "primary": (m.get("primary") or "").strip().lower(),
            "addresses": _norm_addresses(m.get("addresses")),
            "display_name": m.get("DisplayName") or "",
            "type": m.get("RecipientTypeDetails") or "",
        })
    return out


def _ps_script(app_id: str, org: str) -> str:
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "Import-Module ExchangeOnlineManagement",
        "$cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(",
        f"    '{_AUTH_CERT_PATH}', [string]$null,",
        "    ([System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet))",
        f"Connect-ExchangeOnline -AppId '{app_id}' -Certificate $cert -Organization '{org}'"
        " -ShowBanner:$false | Out-Null",
        "Get-EXOMailbox -RecipientTypeDetails UserMailbox,SharedMailbox -ResultSize Unlimited"
        " -Properties ExchangeGuid,EmailAddresses |",
        "  Select-Object @{n='guid';e={$_.ExchangeGuid.ToString()}},"
        " @{n='primary';e={$_.PrimarySmtpAddress}}, DisplayName, RecipientTypeDetails,"
        " @{n='addresses';e={@($_.EmailAddresses | Where-Object {$_ -clike 'smtp:*' -or $_ -clike 'SMTP:*'})}} |",
        "  ConvertTo-Json -Depth 4",
        "Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue | Out-Null",
    ])


def fetch_mailboxes() -> list[dict]:
    """Run EXO PowerShell NOW (blocking, ~seconds). Returns [] on any failure.
    Callers in async context must offload (asyncio.to_thread)."""
    app_id = settings_store.get("CLIENT_ID") or ""
    org = settings_store.get("TENANT_DOMAIN") or ""
    if not (app_id and org and _AUTH_CERT_PATH.exists()):
        log.warning("EXO mailbox fetch skipped — CLIENT_ID/TENANT_DOMAIN/auth cert missing")
        return []
    with tempfile.NamedTemporaryFile(suffix=".ps1", mode="w", delete=False) as f:
        f.write(_ps_script(app_id, org))
        ps_path = f.name
    try:
        proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", ps_path],
                              capture_output=True, text=True, timeout=180)
    except Exception as exc:
        log.error("EXO mailbox fetch failed: %s", exc)
        return []
    finally:
        try:
            Path(ps_path).unlink()
        except OSError:
            pass
    if proc.returncode != 0:
        log.error("EXO mailbox fetch rc=%s: %s", proc.returncode, (proc.stderr or "")[:300])
        return []
    out = proc.stdout or ""
    # ConvertTo-Json may be preceded by warnings — start at the first [ or {.
    starts = [x for x in (out.find("["), out.find("{")) if x >= 0]
    return _parse_mailboxes(out[min(starts):] if starts else out)


def list_mailboxes(force: bool = False) -> list[dict]:
    """Cached authoritative mailbox list (TTL 1h). Falls back to stale cache on
    a failed refresh so a transient EXO hiccup doesn't empty the world."""
    global _cache, _cache_ts
    with _lock:
        if not force and _cache and (time.monotonic() - _cache_ts) < _TTL:
            return _cache
        mbs = fetch_mailboxes()
        if mbs:
            _cache = mbs
            _cache_ts = time.monotonic()
        return mbs or _cache


def resolve_guid(email: str) -> str | None:
    """ExchangeGuid for ANY of a mailbox's SMTP addresses (primary or alias).
    This is what makes config survive rename/address changes."""
    e = (email or "").strip().lower()
    for m in list_mailboxes():
        if e == m["primary"] or e in m["addresses"]:
            return m["guid"]
    return None


def as_sender_list() -> list[dict]:
    """list_mailboxes(), reshaped to {"email","name","type"} for UI dropdowns
    (e.g. notification-sender selection) — both User and Shared mailboxes are
    valid senders. Sorted by email."""
    out = [{
        "email": m["primary"],
        "name": m.get("display_name") or m["primary"],
        "type": "user" if m.get("type") == "UserMailbox" else "shared",
    } for m in list_mailboxes() if m.get("primary")]
    return sorted(out, key=lambda x: x["email"])


def invalidate() -> None:
    """Force the next list_mailboxes() to re-query EXO."""
    global _cache_ts
    with _lock:
        _cache_ts = 0.0
