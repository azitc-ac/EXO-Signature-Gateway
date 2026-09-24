"""Onprem-Postfächer über EXO ermitteln (MailUser) — für Per-Empfänger-Routing.

In einer Hybrid-Koexistenz erscheinen **onprem-Postfächer** im Cloud-Tenant als
`MailUser` (RecipientTypeDetails=MailUser), Cloud-Postfächer dagegen als
`UserMailbox`. Diese MailUser-Adressen sind die maßgebliche Liste, welche
Empfänger einer sonst gerouteten Domäne WIRKLICH onprem liegen — der Rest der
Domäne geht an EXO (siehe domain_routing.route_fuer_empfaenger).

⚠️ `Get-MailUser` liefert AUCH `GuestMailUser` (externe B2B-Gäste) und
Teams-Objekte (`*.teams.ms`). Beide sind KEINE onprem-Postfächer und werden
ausgefiltert (`RecipientTypeDetails -eq 'MailUser'`, keine `*.teams.ms`).

Die Liste wird PERSISTENT in den Einstellungen gehalten (`ONPREM_MAILUSERS`) und
bleibt maßgeblich, bis ein ERFOLGREICHER Abgleich sie ersetzt — ein
fehlgeschlagener Abruf lässt die bestehende Liste unangetastet (kein stilles
Leerräumen, das plötzlich alles nach EXO umleiten würde). Refresh: beim
Aktivieren der Option (Force), per Knopf, und periodisch (scheduler).

Wie exo_mailboxes: reine, testbare Parsing-Logik getrennt vom PowerShell-Abruf.
`settings_store.update()` NUR aus dem Web-/Hauptprozess (nie Subprozess).
"""
import json
import logging
import subprocess
import tempfile
from pathlib import Path

import config
import settings_store

log = logging.getLogger("exo_mailusers")

_AUTH_CERT_PATH = Path(config.DATA_DIR) / "auth.pfx"

KEY_LISTE = "ONPREM_MAILUSERS"       # list[str] — lowercased SMTP-Adressen
KEY_STAND = "ONPREM_MAILUSERS_TS"    # ISO8601-Zeitpunkt des letzten erfolgreichen Abgleichs


def _norm_addresses(raw) -> list[str]:
    """EmailAddresses → deduplizierte, kleingeschriebene SMTP-Adressen ohne
    smtp:-Präfix (Nicht-SMTP-Proxies wie SIP:/X500: ignorieren). ConvertTo-Json
    macht aus einer Ein-Element-Liste einen Skalar — beides abfangen."""
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


def _parse(raw_json: str) -> list[str]:
    """Get-MailUser-JSON → sortierte, deduplizierte Adressliste (primary + Proxies).
    Rein & testbar. GuestMailUser/Teams sind bereits im PS-Skript ausgefiltert;
    hier zusätzlich defensiv, falls das Skript einmal ohne Filter läuft."""
    try:
        data = json.loads(raw_json)
    except Exception as exc:                              # noqa: BLE001
        log.warning("MailUser JSON parse failed: %s", exc)
        return []
    if isinstance(data, dict):
        data = [data]
    adressen: set[str] = set()
    for m in data:
        if (m.get("RecipientTypeDetails") or "") != "MailUser":
            continue
        primary = (m.get("primary") or "").strip().lower()
        if primary.endswith(".teams.ms"):
            continue
        if primary:
            adressen.add(primary)
        adressen.update(a for a in _norm_addresses(m.get("addresses"))
                        if not a.endswith(".teams.ms"))
    return sorted(adressen)


def _ps_script(app_id: str, org: str) -> str:
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "Import-Module ExchangeOnlineManagement",
        "$cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(",
        f"    '{_AUTH_CERT_PATH}', [string]$null,",
        "    ([System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet))",
        f"Connect-ExchangeOnline -AppId '{app_id}' -Certificate $cert -Organization '{org}'"
        " -ShowBanner:$false | Out-Null",
        # Nur echte onprem-MailUser: RecipientTypeDetails=MailUser schließt
        # GuestMailUser (externe Gäste) aus; *.teams.ms ist kein Postfach.
        "Get-MailUser -ResultSize Unlimited |",
        "  Where-Object { $_.RecipientTypeDetails -eq 'MailUser'"
        " -and $_.PrimarySmtpAddress -notlike '*.teams.ms' } |",
        "  Select-Object @{n='primary';e={$_.PrimarySmtpAddress}}, RecipientTypeDetails,"
        " @{n='addresses';e={@($_.EmailAddresses | Where-Object {$_ -clike 'smtp:*' -or $_ -clike 'SMTP:*'})}} |",
        "  ConvertTo-Json -Depth 4",
        "Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue | Out-Null",
    ])


def fetch() -> list[str] | None:
    """EXO JETZT abfragen (blockierend, ~Sekunden). Adressliste bei Erfolg
    (auch leer möglich), `None` bei Fehler — damit der Aufrufer die bestehende
    persistierte Liste NICHT überschreibt, wenn der Abruf scheiterte."""
    app_id = settings_store.get("CLIENT_ID") or ""
    org = settings_store.get("TENANT_DOMAIN") or ""
    if not (app_id and org and _AUTH_CERT_PATH.exists()):
        log.warning("MailUser-Abruf übersprungen — CLIENT_ID/TENANT_DOMAIN/auth-Zert fehlt")
        return None
    with tempfile.NamedTemporaryFile(suffix=".ps1", mode="w", delete=False) as f:
        f.write(_ps_script(app_id, org))
        ps_path = f.name
    try:
        proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", ps_path],
                              capture_output=True, text=True, timeout=180)
    except Exception as exc:                              # noqa: BLE001
        log.error("MailUser-Abruf fehlgeschlagen: %s", exc)
        return None
    finally:
        try:
            Path(ps_path).unlink()
        except OSError:
            pass
    if proc.returncode != 0:
        log.error("MailUser-Abruf rc=%s: %s", proc.returncode, (proc.stderr or "")[:300])
        return None
    out = proc.stdout or ""
    starts = [x for x in (out.find("["), out.find("{")) if x >= 0]
    if not starts:
        # rc=0, aber keine JSON-Ausgabe = 0 MailUser (gültiges, leeres Ergebnis)
        return []
    return _parse(out[min(starts):])


def refresh_and_store(now_iso: str) -> dict:
    """Onprem-MailUser abgleichen und die maßgebliche Liste ERSETZEN — nur bei
    Erfolg. Gibt {ok, count, ts} zurück. `now_iso` wird von aussen gestellt
    (kein Date.now() im Modul, testbar). NUR aus dem Web-/Hauptprozess."""
    adressen = fetch()
    if adressen is None:
        return {"ok": False, "count": len(adressen_gespeichert()), "ts": stand_ts()}
    settings_store.update({KEY_LISTE: adressen, KEY_STAND: now_iso})
    log.info("Onprem-MailUser abgeglichen: %d Adressen (Stand %s)", len(adressen), now_iso)
    return {"ok": True, "count": len(adressen), "ts": now_iso}


def adressen_gespeichert() -> set[str]:
    """Die persistierte Onprem-Adressliste (schnell, hot-path-tauglich)."""
    roh = settings_store.get(KEY_LISTE)
    return {str(a).strip().lower() for a in roh if str(a).strip()} if isinstance(roh, list) else set()


def stand_ts() -> str:
    """Zeitpunkt des letzten erfolgreichen Abgleichs (ISO8601) oder ''."""
    return settings_store.get(KEY_STAND) or ""


def stand() -> dict:
    """Für die Oberfläche: Anzahl onprem-MailUser + Zeitpunkt des letzten Abgleichs."""
    return {"count": len(adressen_gespeichert()), "ts": stand_ts()}
