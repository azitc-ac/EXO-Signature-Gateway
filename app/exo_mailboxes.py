"""Authoritative mailbox enumeration + identity resolution via Exchange Online.

EXO is the source of truth for *which real mailboxes exist* and their stable
**ExchangeGuid**. Graph /users only gives a fuzzy answer (guests, unlicensed,
contacts mixed in) and needs inbox-probing to confirm a real mailbox — EXO's
Get-EXOMailbox answers it directly and correctly.

The ExchangeGuid survives rename / primary-SMTP change, so it is the stable
anchor for mailbox-scoped config (see the MAILBOX_CONFIG GUID refactor).

HYBRID-NUTZER (seit v1.12.55, Schalter HYBRID_POSTFAECHER, nur Modus `smtp`)
--------------------------------------------------------------------------
Postfächer in der lokalen Exchange-Organisation erscheinen in EXO als
`MailUser` (dir-synchronisiert, mit echter ExchangeGuid). Ist der Schalter an,
holt dasselbe PowerShell-Skript sie mit `Get-MailUser` dazu; sie liegen im
selben Zwischenspeicher mit `type == "MailUser"`.

⚠️ Sie sind KEINE EXO-Postfächer. `list_mailboxes()`, `known_addresses()`,
`ist_geteilt()`, `resolve_guid()` und `as_sender_list()` lassen sie deshalb
weg — diese Abfragen bedeuten an rund zehn Stellen „hat ein EXO-Postfach“
(Graph sendMail ALS Absender, IMAP APPEND, 587 SendAsApp). Wer Hybrid-Nutzer
braucht, fragt ausdrücklich: `hybrid_postfaecher()`, `hybrid_addresses()`,
`ist_hybrid()`, `mit_hybrid()`.

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
import config

log = logging.getLogger("exo_mailboxes")

_AUTH_CERT_PATH = Path(config.DATA_DIR) / "auth.pfx"
_TTL = 3600                       # cache lifetime (s)
_lock = threading.RLock()
_cache: list[dict] = []
_cache_ts: float = 0.0

HYBRID_TYP = "MailUser"           # RecipientTypeDetails eines Hybrid-Nutzers
NULL_GUID = "00000000-0000-0000-0000-000000000000"
KEY_HYBRID = "HYBRID_POSTFAECHER"


def ist_teams_adresse(adresse: str) -> bool:
    """Teams-Objekte (`…@….teams.ms`) sind MailUser, aber kein Postfach."""
    return (adresse or "").strip().lower().endswith(".teams.ms")


def hybrid_eingeschaltet() -> bool:
    """Der Schalter in der Oberfläche (Einstellungen → Allgemein)."""
    return settings_store.get(KEY_HYBRID) is True


def hybrid_aktiv() -> bool:
    """Schalter an UND Rückweg `smtp`. Nur dann werden Hybrid-Nutzer abgefragt,
    angezeigt und in die Verteilerliste aufgenommen.

    Warum nur `smtp`: Post eines Hybrid-Nutzers kann nur über den Smarthost
    zurück (Graph und 587 senden ALS ein EXO-Postfach, das es nicht gibt). In
    den Graph-Modi legt die Einrichtung keinen Eingangs-Connector für das
    Gateway an (`-SkipInboundConnector`), und Azure sperrt Port 25 ausgehend —
    der Smarthost-Weg ist dort nicht verlässlich vorhanden."""
    return hybrid_eingeschaltet() and settings_store.reinject_mode() == "smtp"


def _ist_hybrid_eintrag(m: dict) -> bool:
    return (m or {}).get("type") == HYBRID_TYP


def _wahr(wert) -> bool:
    """IsDirSynced kommt als JSON-true, aus älteren Modulen auch als "True"."""
    if isinstance(wert, bool):
        return wert
    return str(wert).strip().lower() == "true"


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
        if not isinstance(m, dict):
            continue
        guid = (m.get("guid") or "").strip().lower()
        if not guid or guid == NULL_GUID:
            continue          # Null-GUID: Teams-Objekte u. ä. — nie ein Anker
        typ = m.get("RecipientTypeDetails") or ""
        primary = (m.get("primary") or "").strip().lower()
        adressen = _norm_addresses(m.get("addresses"))
        if typ == "GuestMailUser":
            continue          # externer B2B-Gast, kein Postfach
        if typ == HYBRID_TYP:
            # Nur echte Hybrid-Nutzer: aus dem lokalen AD synchronisiert, nicht Teams.
            if not _wahr(m.get("IsDirSynced")) or not primary or ist_teams_adresse(primary):
                continue
            adressen = [a for a in adressen if not ist_teams_adresse(a)]
        out.append({
            "guid": guid,
            "primary": primary,
            "addresses": adressen,
            "display_name": m.get("DisplayName") or "",
            "type": typ,
        })
    return out


def _ps_script(app_id: str, org: str, hybrid: bool = False) -> str:
    """Ohne `hybrid` byte-gleich mit dem Stand vor v1.12.55. Mit `hybrid` holt
    DIESELBE Sitzung zusätzlich die Hybrid-Nutzer (ein Verbindungsaufbau)."""
    import ps_text
    if hybrid:
        return _ps_script_hybrid(app_id, org)
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "Import-Module ExchangeOnlineManagement",
        "$cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(",
        f"    '{ps_text.lit(_AUTH_CERT_PATH)}', [string]$null,",
        "    ([System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet))",
        f"Connect-ExchangeOnline -AppId '{ps_text.lit(app_id)}' -Certificate $cert -Organization '{ps_text.lit(org)}'"
        " -ShowBanner:$false | Out-Null",
        "Get-EXOMailbox -RecipientTypeDetails UserMailbox,SharedMailbox -ResultSize Unlimited"
        " -Properties ExchangeGuid,EmailAddresses |",
        "  Select-Object @{n='guid';e={$_.ExchangeGuid.ToString()}},"
        " @{n='primary';e={$_.PrimarySmtpAddress}}, DisplayName, RecipientTypeDetails,"
        " @{n='addresses';e={@($_.EmailAddresses | Where-Object {$_ -clike 'smtp:*' -or $_ -clike 'SMTP:*'})}} |",
        "  ConvertTo-Json -Depth 4",
        "Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue | Out-Null",
    ])


def _ps_script_hybrid(app_id: str, org: str) -> str:
    """Postfächer UND Hybrid-Nutzer in einer Sitzung, als EIN JSON-Feld.

    `ConvertTo-Json -InputObject @(...)` liefert immer ein Feld, auch bei einem
    einzigen Objekt; der Parser verträgt trotzdem beides. Gefiltert wird hier
    UND in `_parse_mailboxes` (Teams, Null-GUID, nicht synchronisiert, Gäste)."""
    import ps_text
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "Import-Module ExchangeOnlineManagement",
        "$cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(",
        f"    '{ps_text.lit(_AUTH_CERT_PATH)}', [string]$null,",
        "    ([System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet))",
        f"Connect-ExchangeOnline -AppId '{ps_text.lit(app_id)}' -Certificate $cert -Organization '{ps_text.lit(org)}'"
        " -ShowBanner:$false | Out-Null",
        "$mb = @(Get-EXOMailbox -RecipientTypeDetails UserMailbox,SharedMailbox -ResultSize Unlimited"
        " -Properties ExchangeGuid,EmailAddresses |",
        "  Select-Object @{n='guid';e={$_.ExchangeGuid.ToString()}},"
        " @{n='primary';e={$_.PrimarySmtpAddress}}, DisplayName, RecipientTypeDetails,"
        " @{n='addresses';e={@($_.EmailAddresses | Where-Object {$_ -clike 'smtp:*' -or $_ -clike 'SMTP:*'})}})",
        "$mu = @(Get-MailUser -ResultSize Unlimited |",
        # Wörtlich statt eingesetzt (ps_text-Prüfung): 'MailUser' = HYBRID_TYP,
        # die Null-GUID = NULL_GUID — test_hybrid_postfaecher prüft den Gleichlauf.
        "  Where-Object { $_.RecipientTypeDetails -eq 'MailUser' -and $_.IsDirSynced"
        " -and [string]$_.ExchangeGuid -ne '00000000-0000-0000-0000-000000000000'"
        " -and $_.PrimarySmtpAddress -notlike '*.teams.ms' } |",
        "  Select-Object @{n='guid';e={[string]$_.ExchangeGuid}},"
        " @{n='primary';e={$_.PrimarySmtpAddress}}, DisplayName, RecipientTypeDetails, IsDirSynced,"
        " @{n='addresses';e={@($_.EmailAddresses | Where-Object {$_ -clike 'smtp:*' -or $_ -clike 'SMTP:*'})}})",
        "ConvertTo-Json -InputObject @($mb + $mu) -Depth 4",
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
        f.write(_ps_script(app_id, org, hybrid_aktiv()))
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
            return _nur_postfaecher(_cache)
        mbs = fetch_mailboxes()
        if mbs:
            _cache = mbs
            _cache_ts = time.monotonic()
        return _nur_postfaecher(mbs or _cache)


def _nur_postfaecher(eintraege: list[dict]) -> list[dict]:
    """Nur EXO-Postfächer — Hybrid-Nutzer heraus (siehe Modulkopf)."""
    return [m for m in eintraege if not _ist_hybrid_eintrag(m)]


def hybrid_postfaecher() -> list[dict]:
    """Hybrid-Nutzer aus dem VORHANDENEN Zwischenspeicher (nie ein Abruf),
    und nur, solange `hybrid_aktiv()` gilt — nach dem Ausschalten verschwinden
    sie sofort aus der Oberfläche, nicht erst nach Ablauf des Speichers."""
    if not hybrid_aktiv():
        return []
    with _lock:
        return [m for m in _cache if _ist_hybrid_eintrag(m)]


def mit_hybrid(force: bool = False) -> list[dict]:
    """EXO-Postfächer plus (falls aktiv) Hybrid-Nutzer — für die Postfachliste,
    das Speichern und die Umstellung auf ExchangeGuid-Anker. NICHT für Wege,
    die ein EXO-Postfach voraussetzen."""
    roh = list_mailboxes(force)
    return list(roh) + hybrid_postfaecher()


def invalidate_cache() -> None:
    """Cache als abgelaufen markieren — der nächste `list_mailboxes()` fragt EXO
    frisch. Nach dem Anlegen eines Postfachs aufrufen, damit es (sobald EXO es
    propagiert hat) auch ohne erzwungenen Abruf erscheint."""
    global _cache_ts
    with _lock:
        _cache_ts = 0.0


def known_addresses() -> set[str]:
    """All known primary + alias addresses from the CURRENT cache snapshot,
    lowercased. Unlike list_mailboxes(), this NEVER triggers a PowerShell
    fetch — safe to call from the SMTP hot path (e.g. to decide whether IMAP
    APPEND is even worth attempting for a given recipient). Empty set if the
    cache hasn't been populated yet (callers must treat that as "unknown",
    not "no mailboxes exist")."""
    with _lock:
        result: set[str] = set()
        for m in _cache:
            if _ist_hybrid_eintrag(m):
                continue      # kein EXO-Postfach — siehe Modulkopf
            if m.get("primary"):
                result.add(m["primary"])
            result.update(m.get("addresses") or [])
        return result


def ist_geteilt(email: str) -> bool | None:
    """Ist die Adresse ein geteiltes Postfach? Wie `known_addresses()` nur aus
    dem vorhandenen Zwischenspeicher — nie ein PowerShell-Abruf, sicher im
    SMTP-Pfad. None = unbekannt (Speicher leer oder Adresse nicht darin)."""
    e = (email or "").strip().lower()
    with _lock:
        for m in _cache:
            if _ist_hybrid_eintrag(m):
                continue      # Hybrid-Nutzer: kein geteiltes EXO-Postfach
            if e == m.get("primary") or e in (m.get("addresses") or []):
                return m.get("type") != "UserMailbox"
    return None


def hybrid_addresses() -> set[str]:
    """Alle Adressen der Hybrid-Nutzer im Zwischenspeicher (nie ein Abruf).
    Unabhängig vom Schalter: Eine einmal gelesene Adresse bleibt intern, bis
    der Speicher neu gefüllt wird."""
    with _lock:
        result: set[str] = set()
        for m in _cache:
            if not _ist_hybrid_eintrag(m):
                continue
            if m.get("primary"):
                result.add(m["primary"])
            result.update(m.get("addresses") or [])
        return result


def ist_hybrid(email: str) -> bool:
    """Gehört die Adresse einem Hybrid-Nutzer? Heißer Pfad, nie ein Abruf.

    Zwei Quellen, weil der Zwischenspeicher nach einem Neustart leer ist: der
    Speicher selbst und die Markierung `hybrid` im MAILBOX_CONFIG-Eintrag (beim
    Speichern der Postfachliste gesetzt). Ohne die zweite ginge die erste Mail
    nach einem Neustart den Graph-Weg — und scheiterte mit ErrorInvalidUser."""
    e = (email or "").strip().lower()
    if not e:
        return False
    if e in hybrid_addresses():
        return True
    if e in known_addresses():
        # Inzwischen nach EXO migriert: Der Zwischenspeicher kennt ein echtes
        # Postfach, die Markierung im Eintrag ist nur noch nicht nachgezogen.
        return False
    import mailbox_match
    eintrag = mailbox_match.match_sender(settings_store.get("MAILBOX_CONFIG") or {}, e)
    return bool(eintrag.get("hybrid"))


def resolve_guid(email: str) -> str | None:
    """ExchangeGuid for ANY of a mailbox's SMTP addresses (primary or alias).
    This is what makes config survive rename/address changes.
    Nur EXO-Postfächer (list_mailboxes) — Hybrid-Nutzer liefern None."""
    e = (email or "").strip().lower()
    for m in list_mailboxes():
        if e == m["primary"] or e in m["addresses"]:
            return m["guid"]
    return None


def as_sender_list() -> list[dict]:
    """list_mailboxes(), reshaped to {"email","name","type"} for UI dropdowns
    (e.g. notification-sender selection) — both User and Shared mailboxes are
    valid senders. Sorted by email. Hybrid-Nutzer NICHT: Benachrichtigungen
    gehen per Graph sendMail ALS dieses Postfach, das es in EXO nicht gibt."""
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
