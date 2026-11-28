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
    """address(lower) → cfg, from an e-mail-, guid-, or mixed-keyed config.

    Dieselbe Zuordnung wie `build_key_index` (eine Regel für alle drei
    Zugriffe): Haupt- vor Nebenadresse. Bis v1.12.189 gewann hier der LETZTE
    Eintrag, in `match_sender_key` der ERSTE — trug ein Eintrag eine veraltete
    Nebenadresse (Adresse inzwischen ein eigenes Postfach), landete dieselbe
    Adresse je nach Aufrufer in zwei verschiedenen Einträgen."""
    return {a: mailbox_config[k] for a, k in build_key_index(mailbox_config).items()}


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
    return build_key_index(mailbox_config).get(sender.lower(), "")


def build_key_index(mailbox_config: dict) -> dict[str, str]:
    """address(lower) → MAILBOX_CONFIG-Schlüssel — die EINE Zuordnungsregel.

    Vorrang: Schlüssel-Adresse bzw. Hauptadresse (`primary`) eines Eintrags vor
    jeder Nebenadresse (`known_addresses`) eines ANDEREN Eintrags; innerhalb
    einer Stufe gewinnt der erste Eintrag. Anlass (28.11.2026): Eine Adresse
    wurde aus einem Postfach entfernt und als eigenes geteiltes Postfach
    angelegt; der alte Eintrag führte sie noch als Nebenadresse, und
    `match_sender_key` ordnete sie dem alten Postfach zu.

    Einmal gebaut für Schleifen über viele Postfächer (18.11.2026: je Zeile neu
    gebaut war n², 700 Postfächer 3,7 s auf dem Raspi)."""
    idx: dict[str, str] = {}
    eintraege = [(k, c) for k, c in (mailbox_config or {}).items() if isinstance(c, dict)]
    for key, cfg in eintraege:
        haupt = key.lower() if "@" in key else (cfg.get("primary") or "").lower()
        if haupt:
            idx.setdefault(haupt, key)
    for key, cfg in eintraege:
        if "@" in key:
            continue
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


def adressen_nachfuehren(mailbox_config: dict, exo_liste: list[dict]) -> dict | None:
    """Adress-Zwischenspeicher der ExchangeGuid-Einträge (`known_addresses`,
    `primary`, `display_name`) an den aktuellen EXO-Stand angleichen. Rein:
    liefert die neue Konfiguration oder None, wenn sich nichts ändert.

    Bis v1.12.189 wurde der Zwischenspeicher nur beim Speichern der
    Postfachliste neu geschrieben. Wurde eine Nebenadresse in Exchange entfernt
    (etwa, weil sie ein eigenes Postfach wurde), führte der Eintrag sie weiter.

    Nur Einträge, deren GUID in der Liste steht — eine leere oder unvollständige
    Liste (EXO nicht erreichbar, Hybrid-Nutzer ohne Option) ändert nichts."""
    nach_guid = {m.get("guid"): m for m in (exo_liste or []) if isinstance(m, dict) and m.get("guid")}
    if not nach_guid:
        return None
    neu = dict(mailbox_config or {})
    geaendert = False
    for key, cfg in (mailbox_config or {}).items():
        if "@" in key or not isinstance(cfg, dict) or key not in nach_guid:
            continue
        mb = nach_guid[key]
        soll = {"known_addresses": list(mb.get("addresses") or []),
                "primary": mb.get("primary") or cfg.get("primary", ""),
                "display_name": mb.get("display_name") or cfg.get("display_name", "")}
        if not soll["known_addresses"]:
            continue
        if any(cfg.get(f) != w for f, w in soll.items()):
            neu[key] = {**cfg, **soll}
            geaendert = True
    return neu if geaendert else None
