"""
Source-IP allowlist for the inbound SMTP listener (:25).

Legitimate traffic to this port comes only from Exchange Online's outbound
connector, so connections from other sources are rejected as defense-in-depth.

The allowlist is Microsoft's official Exchange Online IP ranges, fetched from
the O365 endpoints web service (endpoints.office.com) and cached to disk so it
stays current as Microsoft changes IPs — never hardcoded/stale.

FAIL-SAFE: if the range list is empty (fetch never succeeded and no cache),
is_allowed() returns True for everything — i.e. it degrades to the previous
(unfiltered) behaviour rather than blocking all mail. Enforcement only kicks
in once we have a valid list. Loopback and admin-configured extra CIDRs are
always allowed.
"""
import ipaddress
import json
import logging
import threading
import uuid
from pathlib import Path

import httpx

import settings_store

log = logging.getLogger(__name__)

_CACHE_FILE = Path("/app/data/exchange_ip_ranges.json")
_ENDPOINT = "https://endpoints.office.com/endpoints/worldwide"

_lock = threading.RLock()
_networks: list = []          # list[ipaddress.ip_network] from Microsoft
_loaded = False
_last_refresh_ts: float = 0.0   # epoch of last SUCCESSFUL network refresh (0 = never)
_recent_rejects: "deque" = __import__("collections").deque(maxlen=50)  # (epoch, ip) blocked conns


def record_reject(ip: str) -> None:
    """Remember a blocked source IP (for the admin UI). Best-effort, bounded."""
    import time as _t
    with _lock:
        _recent_rejects.append((_t.time(), ip))


def recent_rejects() -> list:
    """Most-recent-first list of (epoch, ip) blocked connections."""
    with _lock:
        return list(reversed(_recent_rejects))


def last_refresh_ts() -> float:
    return _last_refresh_ts


def _always_allowed_networks() -> list:
    nets = [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")]
    for cidr in (settings_store.get("SMTP_ACL_EXTRA_CIDRS") or []):
        try:
            nets.append(ipaddress.ip_network(str(cidr).strip(), strict=False))
        except Exception as exc:
            log.warning("smtp_acl: ignoring invalid extra CIDR %r: %s", cidr, exc)
    return nets


def _load_cache() -> None:
    global _networks, _loaded
    with _lock:
        if _loaded:
            return
        _loaded = True
        if _CACHE_FILE.exists():
            try:
                cidrs = json.loads(_CACHE_FILE.read_text())
                _networks = [ipaddress.ip_network(c, strict=False) for c in cidrs]
                log.info("smtp_acl: loaded %d Exchange ranges from cache", len(_networks))
            except Exception as exc:
                log.warning("smtp_acl: cache load failed: %s", exc)


def refresh() -> int:
    """Fetch current Exchange Online IP ranges, update cache. Returns count.
    Keeps the previous list on failure (never empties a good list)."""
    global _networks
    try:
        cid = str(uuid.uuid4())
        r = httpx.get(_ENDPOINT, params={"clientrequestid": cid, "ServiceAreas": "Exchange"},
                      timeout=30)
        r.raise_for_status()
        cidrs: set[str] = set()
        for entry in r.json():
            if entry.get("serviceArea") != "Exchange":
                continue
            for ip in (entry.get("ips") or []):
                cidrs.add(ip)
        nets = []
        for c in sorted(cidrs):
            try:
                nets.append(ipaddress.ip_network(c, strict=False))
            except Exception:
                pass
        if not nets:
            log.warning("smtp_acl: refresh returned 0 ranges — keeping existing %d",
                        len(_networks))
            return len(_networks)
        import time as _t
        global _last_refresh_ts
        with _lock:
            _networks = nets
            _last_refresh_ts = _t.time()
        try:
            _CACHE_FILE.write_text(json.dumps(sorted(cidrs)))
        except Exception as exc:
            log.warning("smtp_acl: cache write failed: %s", exc)
        log.info("smtp_acl: refreshed %d Exchange ranges", len(nets))
        return len(nets)
    except Exception as exc:
        log.warning("smtp_acl: refresh failed (%s) — keeping existing %d ranges",
                    exc, len(_networks))
        return len(_networks)


def is_allowed(ip: str) -> bool:
    """True if the source IP may talk to the SMTP listener.

    - ACL disabled            -> always True
    - loopback / extra CIDRs  -> always True
    - Exchange range list empty (never fetched) -> True (FAIL-SAFE: no block)
    - otherwise               -> True only if ip is inside an Exchange range
    """
    if settings_store.get("SMTP_SOURCE_ACL_ENABLED") is False:
        return True
    _load_cache()
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # can't parse -> don't block (fail-safe)

    for n in _always_allowed_networks():
        if addr in n:
            return True

    with _lock:
        nets = _networks
    if not nets:
        return True  # no list yet -> fail-safe allow (degrade to old behaviour)
    return any(addr in n for n in nets)


def range_count() -> int:
    _load_cache()
    with _lock:
        return len(_networks)
