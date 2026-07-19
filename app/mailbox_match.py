"""Runtime sender→config matching for MAILBOX_CONFIG (hot-path, zero deps).

Understands BOTH schema forms transparently, so the config can migrate from
e-mail keys to ExchangeGuid anchors without ever touching this matcher again:

  - e-mail-keyed  {"user@dom": cfg}                       → the key IS the address
  - guid-keyed    {"<guid>": {known_addresses:[...], ...}} → match any known address

The index is tiny (a handful of mailboxes × few aliases), so it is rebuilt per
call — no cache, no invalidation bugs.
"""


def build_address_index(mailbox_config: dict) -> dict:
    """address(lower) → cfg, from an e-mail-, guid-, or mixed-keyed config."""
    idx: dict[str, dict] = {}
    for key, cfg in (mailbox_config or {}).items():
        if not isinstance(cfg, dict):
            continue
        if "@" in key:                      # e-mail-keyed: the key is the address
            idx[key.lower()] = cfg
        else:                               # guid-keyed: match on the address cache
            for a in cfg.get("known_addresses", []):
                if a:
                    idx[str(a).lower()] = cfg
            primary = (cfg.get("primary") or "").lower()
            if primary:
                idx[primary] = cfg
    return idx


def match_sender(mailbox_config: dict, sender: str) -> dict:
    """Return the MAILBOX_CONFIG entry for a sender address (primary or alias),
    or {} if none. Works regardless of whether the config is e-mail- or guid-keyed."""
    if not mailbox_config or not sender:
        return {}
    return build_address_index(mailbox_config).get(sender.lower(), {})


def match_sender_key(mailbox_config: dict, sender: str) -> str:
    """Return the MAILBOX_CONFIG key (ExchangeGuid or email) for a sender, or '' if not found."""
    if not mailbox_config or not sender:
        return ""
    sender_l = sender.lower()
    for key, cfg in mailbox_config.items():
        if not isinstance(cfg, dict):
            continue
        if "@" in key:
            if key.lower() == sender_l:
                return key
        else:
            if (cfg.get("primary") or "").lower() == sender_l:
                return key
            if sender_l in [str(a).lower() for a in cfg.get("known_addresses", [])]:
                return key
    return ""


def configured_addresses(mailbox_config: dict) -> list[str]:
    """The primary address of each config entry — the key itself for e-mail-keyed
    entries, or the cached `primary` for guid-keyed ones. Used wherever code needs
    to iterate 'which mailboxes are configured' (health checks, DG membership)."""
    out: list[str] = []
    for key, cfg in (mailbox_config or {}).items():
        if not isinstance(cfg, dict):
            continue
        addr = key.lower() if "@" in key else (cfg.get("primary") or "").lower()
        if addr and addr not in out:
            out.append(addr)
    return out
