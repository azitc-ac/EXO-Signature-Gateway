"""Runtime sender→config matching for MAILBOX_CONFIG (hot-path, zero deps).

Understands BOTH schema forms transparently, so the config can migrate from
e-mail keys to ExchangeGuid anchors without ever touching this matcher again:

  - e-mail-keyed  {"user@dom": cfg}                       → the key IS the address
  - guid-keyed    {"<guid>": {known_addresses:[...], ...}} → match any known address

The index is rebuilt per call — no cache, no invalidation bugs. For ONE
lookup that is cheap; in a loop over many mailboxes it is n² (700 mailboxes:
seconds). Loops build the index ONCE: `build_address_index` / `build_key_index`.
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


def build_key_index(mailbox_config: dict) -> dict[str, str]:
    """address(lower) → MAILBOX_CONFIG-Schlüssel, EINMAL gebaut — für Schleifen
    über viele Postfächer (Postfachliste, Gesundheitsprüfung).

    Liefert für jede Adresse genau das, was `match_sender_key` liefern würde:
    Dort gewinnt der ERSTE Eintrag in Einfügereihenfolge, der die Adresse
    trägt (Primäradresse vor den bekannten Adressen desselben Eintrags) —
    deshalb `setdefault`. `build_address_index` dagegen lässt den LETZTEN
    gewinnen; beide Regeln bleiben, wie sie sind (tests/test_postfach_index.py).

    Anlass (18.11.2026): `match_sender`/`match_sender_key` bauen bei JEDEM
    Aufruf den ganzen Index neu. In einer Schleife über n Postfächer ist das
    n² — gemessen 700 Postfächer: 3,7 s für GET /api/mailboxes auf dem Raspi,
    im Event-Loop des Webservers."""
    idx: dict[str, str] = {}
    for key, cfg in (mailbox_config or {}).items():
        if not isinstance(cfg, dict):
            continue
        if "@" in key:
            idx.setdefault(key.lower(), key)
            continue
        primary = (cfg.get("primary") or "").lower()
        if primary:
            idx.setdefault(primary, key)
        for a in cfg.get("known_addresses", []):
            a = str(a).lower()
            if a:
                idx.setdefault(a, key)
    return idx


def selbes_postfach(mailbox_config: dict, a: str, b: str) -> bool:
    """Gehören zwei Adressen zum SELBEN Postfach-Eintrag (ExchangeGuid samt
    bekannter Adressen)? Für jeden Vergleich „angemeldete Person ↔ Adresse
    aus Outlook“: Die Anmeldung liefert den UPN, Outlook die Hauptadresse —
    weichen beide ab (UPN = Kurzform, Hauptadresse = Langform), ist es
    trotzdem das eigene Postfach. Ein Textvergleich hielt das bis v1.12.39
    im Verfassen-Add-in für „schreibt für ein fremdes Postfach“."""
    ka = match_sender_key(mailbox_config, a)
    return bool(ka) and ka == match_sender_key(mailbox_config, b)


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
