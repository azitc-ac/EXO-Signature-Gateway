"""Migrate MAILBOX_CONFIG from e-mail keys to ExchangeGuid anchors.

Pure planning (`plan_migration`) is separated from any I/O so it is unit-testable
and supports a **dry-run** before anything is written. A guid-keyed entry carries
the policy flags plus a refreshable address cache:

    MAILBOX_CONFIG[<exchange_guid>] = {
        sig, smime, use_policy, ...      # policy (unchanged)
        known_addresses: [...],          # all SMTP addresses (EXO cache)
        primary, display_name            # display
    }

Keys are told apart trivially: an e-mail key contains '@', a guid never does.
Unresolvable entries (mailbox gone) are kept e-mail-keyed and flagged `_orphan`
so nothing is silently lost; the handler's reverse-index tolerates both forms.

IMPORTANT: do NOT apply the migration until handler.py matches via the address
reverse-index — otherwise guid-keyed entries wouldn't match the sender address.
"""
import logging

log = logging.getLogger("mailbox_migrate")

# Policy flags that are OR-merged when two e-mail entries map to the same mailbox.
_POLICY_BOOLS = ("sig", "smime", "use_policy")
_MANAGED = ("known_addresses", "primary", "display_name", "_orphan")


def _is_email_key(key: str) -> bool:
    return "@" in key


def _address_index(mailboxes: list[dict]) -> dict:
    """address(lower) → mailbox record."""
    idx: dict[str, dict] = {}
    for m in mailboxes:
        for a in m.get("addresses", []):
            if a:
                idx[a.lower()] = m
        p = (m.get("primary") or "").lower()
        if p:
            idx[p] = m
    return idx


def _entry_from(cfg: dict, m: dict | None) -> dict:
    """Policy flags from cfg + address cache/display from the EXO record."""
    entry = {k: v for k, v in cfg.items() if k not in _MANAGED}
    if m:
        entry["known_addresses"] = list(m.get("addresses", []))
        entry["primary"] = m.get("primary", "")
        entry["display_name"] = m.get("display_name", "")
    return entry


def _merge_into(new_config: dict, guid: str, entry: dict) -> bool:
    """Insert entry under guid, OR-merging policy flags on collision.
    Returns True if a merge (collision) happened."""
    if guid not in new_config:
        new_config[guid] = entry
        return False
    existing = new_config[guid]
    for b in _POLICY_BOOLS:
        existing[b] = bool(existing.get(b)) or bool(entry.get(b))
    for k in ("known_addresses", "primary", "display_name"):
        if entry.get(k):
            existing[k] = entry[k]
    return True


def plan_migration(mailbox_config: dict, mailboxes: list[dict]) -> dict:
    """Compute a guid-keyed config from an (email- or mixed-keyed) config.

    Returns {new_config, migrated:[...], merges:[...], orphans:[...], kept:[...]}
    — a pure plan, nothing is written."""
    idx = _address_index(mailboxes)
    by_guid = {m["guid"]: m for m in mailboxes}
    new_config: dict = {}
    migrated, merges, orphans, kept = [], [], [], []

    for key, cfg in (mailbox_config or {}).items():
        cfg = dict(cfg or {})
        if not _is_email_key(key):
            # already guid-keyed → refresh metadata from EXO if the mailbox exists
            m = by_guid.get(key.lower())
            _merge_into(new_config, key.lower(), _entry_from(cfg, m))
            kept.append(key)
            continue
        m = idx.get(key.lower())
        if not m:
            cfg["_orphan"] = True
            new_config[key.lower()] = cfg
            orphans.append(key)
            continue
        guid = m["guid"]
        collided = _merge_into(new_config, guid, _entry_from(cfg, m))
        migrated.append({"email": key, "guid": guid, "primary": m["primary"]})
        if collided:
            merges.append({"email": key, "guid": guid, "primary": m["primary"]})

    return {"new_config": new_config, "migrated": migrated,
            "merges": merges, "orphans": orphans, "kept": kept}
