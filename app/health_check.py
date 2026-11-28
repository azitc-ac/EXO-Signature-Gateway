"""
Mailbox health check system.

Runs per-configured-mailbox checks and stores results in settings_store under
MAILBOX_HEALTH (dict keyed by email). Gateway auto-fix actions are appended
to GATEWAY_AUDIT_LOG (rolling 200 entries).

Public API:
    async def run_all_checks(emails=None) -> dict
    async def run_checks_for_mailbox(email: str) -> dict
"""
import asyncio
import hashlib
import json
import re
import logging
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import settings_store
import config

log = logging.getLogger(__name__)

_AUTH_CERT_PATH = Path(config.DATA_DIR) / "auth.pfx"
_AUDIT_MAX = 200


# ── Audit log ─────────────────────────────────────────────────────────────────

def _append_audit(action: str, mailbox: str, detail: str) -> None:
    """Append one entry to GATEWAY_AUDIT_LOG (rolling 200)."""
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "action": action,
        "mailbox": mailbox,
        "detail": detail,
    }
    log_list: list = list(settings_store.get("GATEWAY_AUDIT_LOG") or [])
    log_list.append(entry)
    if len(log_list) > _AUDIT_MAX:
        log_list = log_list[-_AUDIT_MAX:]
    settings_store.force_update({"GATEWAY_AUDIT_LOG": log_list})


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_result(status: str, detail: str) -> dict:
    return {"status": status, "checked_at": _now_str(), "detail": detail}


# ── Liveness für /health (Bypass-Wächter) ─────────────────────────────────────

def _smtp_listener_ok() -> bool:
    """Bedient der SMTP-Listener? Non-blocking über den Zustand des
    aiosmtpd-Controllers — KEIN Verbindungsaufbau auf :25.

    ⚠️ Bewusst kein `socket.create_connection` auf :25: Das würde (a) den
    Event-Loop des /health-Handlers bis zum Timeout bremsen, wenn der Listener
    weg ist, und (b) bei jedem Aufruf eine Leerverbindung im SMTP-Log erzeugen.
    `server.is_serving()` liest nur ein Flag — der Bypass-Wächter selbst spricht
    ohnehin nur HTTPS mit /health, nie SMTP.
    """
    try:
        import runtime_state
        c = runtime_state.smtp_controller
        srv = getattr(c, "server", None) if c is not None else None
        if srv is None:
            return False
        is_serving = getattr(srv, "is_serving", None)
        return bool(is_serving()) if callable(is_serving) else True
    except Exception:                                       # noqa: BLE001
        return False


def liveness() -> dict:
    """Kompakter Zustand für `/health` — der Bypass-Wächter braucht nur das."""
    import config
    import graph_client
    return {
        "smtp_listener": _smtp_listener_ok(),
        "graph_token": graph_client.cached_token_valid(),
        "reinject_mode": settings_store.reinject_mode(),
        "version": config.VERSION,
    }


# ── Check 1: exo_mailbox ──────────────────────────────────────────────────────

def _check_exo_mailbox_sync(email: str) -> dict:
    """
    Authoritative existence check via EXO (Get-EXOMailbox, through exo_mailboxes'
    cached enumeration + alias resolution) — replaces the former Graph/license-
    based check.

    Why: Graph /users assignedPlans answers "does this account have an Exchange
    license", not "does this mailbox exist and work". That produced a false
    "Keine aktive Exchange-Lizenz" warning for Shared Mailboxes, which have no
    license but are perfectly valid, functioning EXO mailboxes. EXO's own
    Get-EXOMailbox is the direct, correct answer to "does this mailbox exist".
    """
    import exo_mailboxes
    guid = exo_mailboxes.resolve_guid(email)
    if guid:
        return _make_result("ok", "Postfach in EXO vorhanden")
    return _make_result("error", "Postfach nicht in EXO gefunden (auch nicht als Alias)")


# ── Check 2+3: dg_member + imap_permission (single PS run for all mailboxes) ─

def _check_exo_batch_sync(emails: list[str], ohne_app: set[str] | None = None) -> dict:
    """
    Run a single PowerShell session for all emails at once.
    Returns dict: {email: {"dg_member": bool, "dg_fixed": bool,
                            "imap_perm": bool, "imap_fixed": bool}}

    `ohne_app`: Adressen ohne Get-App-Abfrage — Hybrid-Nutzer. Get-App braucht
    ein EXO-Postfach; für einen MailUser scheitert jeder Aufruf, und
    `_checks_hybrid` wertet die Add-ins ohnehin nicht aus. Bis v1.12.66 lief er
    trotzdem: je Hybrid-Nutzer und Add-in ein Aufruf (0,8–1,3 s), bei 500
    Hybrid-Nutzern und zwei Add-ins rund eine Viertelstunde je Prüflauf.
    """
    if ohne_app is None:
        import exo_mailboxes
        ohne_app = {e for e in emails if exo_mailboxes.ist_hybrid(e)}
    ohne_app = {e.lower() for e in ohne_app}
    import config as _config

    app_id = _config.CLIENT_ID or settings_store.get("CLIENT_ID") or ""
    tenant_domain = settings_store.get("TENANT_DOMAIN") or ""
    reinject_mode = settings_store.reinject_mode()

    if not app_id or not tenant_domain:
        error_result = {"dg_member": False, "dg_fixed": False, "imap_perm": None, "imap_fixed": False,
                        "error": "CLIENT_ID oder TENANT_DOMAIN nicht konfiguriert"}
        return {e: dict(error_result) for e in emails}
    if not _AUTH_CERT_PATH.exists():
        error_result = {"dg_member": False, "dg_fixed": False, "imap_perm": None, "imap_fixed": False,
                        "error": "Auth-Zertifikat nicht gefunden"}
        return {e: dict(error_result) for e in emails}

    check_imap = (reinject_mode == "graph_plus")
    gw = settings_store.get("GATEWAY_NAME") or "EXO Signature Gateway"
    dg_name = f"{gw} - Enabled Mailboxes"

    # Build email list as JSON for PS
    emails_json = json.dumps(emails)

    import ps_text
    ps_check_imap = "1" if check_imap else "0"
    addin_ids_json = json.dumps([a["id"] for a in _addin_soll()])
    ohne_app_json = json.dumps(sorted(ohne_app))

    ps_script = f"""
$ErrorActionPreference = 'SilentlyContinue'
$cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
    '{ps_text.lit(_AUTH_CERT_PATH)}', [string]$null,
    ([System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet -bor
     [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::Exportable))
try {{
    Connect-ExchangeOnline -AppId '{ps_text.lit(app_id)}' -Certificate $cert -Organization '{ps_text.lit(tenant_domain)}' -ShowBanner:$false -ShowProgress:$false -ErrorAction Stop
}} catch {{
    @{{error="EXO-Verbindung fehlgeschlagen: $($_.Exception.Message)"}} | ConvertTo-Json -Compress
    exit 1
}}

$dgName = '{ps_text.lit(dg_name)}'
$checkImap = [int]'{ps_check_imap}'
$emailsJson = '{ps_text.lit(emails_json)}'
$emails = $emailsJson | ConvertFrom-Json
$addinIds = @('{ps_text.lit(addin_ids_json)}' | ConvertFrom-Json)
$ohneApp = @('{ps_text.lit(ohne_app_json)}' | ConvertFrom-Json)

# Get SP for IMAP permission checks
$sp = $null
if ($checkImap -eq 1) {{
    $sp = Get-ServicePrincipal | Where-Object {{ $_.AppId -eq '{ps_text.lit(app_id)}' }}
}}

# Get current DG members
$dgMembers = @()
$dgFetchError = $null
try {{
    $dg = Get-DistributionGroup -Identity $dgName -ErrorAction Stop
    $dgMemberObjs = Get-DistributionGroupMember -Identity $dgName -ResultSize Unlimited -ErrorAction Stop
    $dgMembers = @($dgMemberObjs | ForEach-Object {{ $_.PrimarySmtpAddress.ToLower() }})
}} catch {{
    $dgFetchError = $_.Exception.Message
}}

$results = @{{}}

# Fassung im Katalog „Integrierte Apps“ (seit v1.12.161): Get-App -OrganizationApp
# -PrivateCatalog liest app-only, was das Admin Center bereitstellt — getrennt
# von der Postfach-Sicht, die bis zu 72 h hinterherhinkt (DEV-Tenant 25.11.2026:
# Katalog 1.12.121 Minuten nach dem Aktualisieren, Postfach noch 1.11.57).
# Fehler als "!<Meldung>" — dann gilt die Einordnung ohne Katalog.
$katalog = @{{}}
foreach ($aid in $addinIds) {{
    try {{
        $k = Get-App -OrganizationApp -PrivateCatalog -Identity $aid -ErrorAction Stop
        $katalog[$aid] = if ($k) {{ [string]$k.AppVersion }} else {{ "" }}
    }} catch {{
        $grund = [string]$_.CategoryInfo.Reason
        if ($grund -eq 'ManagementObjectNotFoundException' -or $_.FullyQualifiedErrorId -like '*NotFound*') {{
            $katalog[$aid] = ""
        }} else {{
            $katalog[$aid] = "!" + [string]$_.Exception.Message
        }}
    }}
}}
$results["_katalog"] = $katalog
foreach ($email in $emails) {{
    $emailLow = $email.ToLower()
    $dgFixed = $false
    $imapPerm = $null
    $imapFixed = $false

    if ($dgFetchError) {{
        # DG fetch failed — report error, skip auto-fix attempt
        $results[$emailLow] = @{{
            dg_member = $false
            dg_fixed = $false
            imap_perm = $imapPerm
            imap_fixed = $imapFixed
            error = "DG-Abruf fehlgeschlagen: $dgFetchError"
        }}
        continue
    }}

    $dgMember = $dgMembers -contains $emailLow

    # Auto-fix: add to DG if missing
    if (-not $dgMember) {{
        try {{
            Add-DistributionGroupMember -Identity $dgName -Member $email -ErrorAction Stop
            $dgMember = $true
            $dgFixed = $true
        }} catch {{
            if ($_.Exception.Message -like '*already*' -or $_.Exception.Message -like '*Duplicate*' -or $_.Exception.Message -like '*member*') {{
                $dgMember = $true
            }}
        }}
    }}

    # IMAP permission check
    if ($checkImap -eq 1 -and $sp) {{
        try {{
            $perm = Get-MailboxPermission -Identity $email -User $sp.Identity -ErrorAction Stop
            $imapPerm = ($null -ne $perm)
        }} catch {{
            $imapPerm = $false
        }}
        # Auto-fix: grant FullAccess if missing
        if ($imapPerm -eq $false) {{
            try {{
                Add-MailboxPermission -Identity $email -User $sp.Identity -AccessRights FullAccess -AutoMapping $false -ErrorAction Stop | Out-Null
                $imapPerm = $true
                $imapFixed = $true
            }} catch {{
                if ($_.Exception.Message -like '*already present*') {{
                    $imapPerm = $true
                }} else {{
                    $imapPerm = $false
                }}
            }}
        }}
    }}

    # Welche Fassung der Add-ins Exchange diesem Postfach ausgeliefert hat (nur lesen)
    $addins = @{{}}
    # „Nicht gefunden“ heisst nicht installiert (""). Jeder ANDERE Fehler
    # (Drosselung, Sitzung weg) kommt als "!<Meldung>" zurück — bis v1.12.42
    # verschluckte SilentlyContinue ihn, und aus „nicht abfragbar“ wurde
    # „nicht installiert“ bzw. in der Abnahme „entfällt“.
    foreach ($aid in $(if ($ohneApp -contains $emailLow) {{ @() }} else {{ $addinIds }})) {{
        try {{
            $app = Get-App -Mailbox $email -Identity $aid -ErrorAction Stop
            $addins[$aid] = if ($app) {{ [string]$app.AppVersion }} else {{ "" }}
        }} catch {{
            $grund = [string]$_.CategoryInfo.Reason
            if ($grund -eq 'ManagementObjectNotFoundException' -or $_.FullyQualifiedErrorId -like '*NotFound*') {{
                $addins[$aid] = ""
            }} else {{
                $addins[$aid] = "!" + [string]$_.Exception.Message
            }}
        }}
    }}

    $results[$emailLow] = @{{
        dg_member = $dgMember
        dg_fixed = $dgFixed
        imap_perm = $imapPerm
        imap_fixed = $imapFixed
        addins = $addins
    }}
}}

$results | ConvertTo-Json -Depth 3 -Compress
Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
"""

    try:
        with tempfile.NamedTemporaryFile(suffix=".ps1", mode="w", delete=False,
                                         encoding="utf-8") as f:
            f.write(ps_script)
            ps_path = f.name

        proc = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-File", ps_path],
            # Get-App ist ein Aufruf je Postfach und Add-in (VM 18.10.2026: 0,8–1,3 s) —
            # die feste Grenze reichte für große Tenants nicht mehr.
            capture_output=True, text=True,
            timeout=180 + 2 * len([e for e in emails if e.lower() not in ohne_app])
            * len(_addin_soll()),
        )
        Path(ps_path).unlink(missing_ok=True)

        output = proc.stdout.strip()
        # Find JSON line(s)
        for line in output.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                raw = json.loads(line)
                # Connection-error sentinel: {"error": "..."}
                if "error" in raw and len(raw) == 1:
                    err_msg = raw["error"]
                    log.warning("health_check: PS EXO connect error: %s", err_msg)
                    error_result = {"dg_member": False, "dg_fixed": False,
                                    "imap_perm": None, "imap_fixed": False,
                                    "error": err_msg}
                    return {e.lower(): dict(error_result) for e in emails}
                # Normal result: {email: {dg_member, ...}}
                result = {}
                kat = raw.pop("_katalog", None)
                if isinstance(kat, dict):
                    _katalog_merken(kat)
                for k, v in raw.items():
                    if not isinstance(v, dict):
                        continue
                    result[k.lower()] = {
                        "dg_member": bool(v.get("dg_member", False)),
                        "dg_fixed": bool(v.get("dg_fixed", False)),
                        "imap_perm": v.get("imap_perm"),
                        "imap_fixed": bool(v.get("imap_fixed", False)),
                        "addins": v.get("addins") if isinstance(v.get("addins"), dict) else None,
                        "error": v.get("error"),
                    }
                return result
            except Exception as exc:
                log.warning("health_check: PS JSON parse error: %s — %s", exc, line[:300])

        err_msg = (proc.stderr or proc.stdout or "Kein Output")[:300]
        log.warning("health_check: PS batch check failed rc=%d: %s", proc.returncode, err_msg)
        error_result = {"dg_member": False, "dg_fixed": False, "imap_perm": None, "imap_fixed": False,
                        "error": f"PS Fehler (rc={proc.returncode}): {err_msg}"}
        return {e.lower(): dict(error_result) for e in emails}

    except Exception as exc:
        log.error("health_check: _check_exo_batch_sync error: %s", exc)
        error_result = {"dg_member": False, "dg_fixed": False, "imap_perm": None, "imap_fixed": False,
                        "error": str(exc)}
        return {e.lower(): dict(error_result) for e in emails}


# ── Check: Outlook-Add-ins ────────────────────────────────────────────────────

# Versionsstand je Add-in, geschrieben beim Ausliefern des Manifests
# (webui/routen/addin.py::_manifest_version): {"hash", "version"}.
_ADDINS = (("_ADDIN_VERFASSEN_STAND", "verfassen"),
           ("_ADDIN_MANIFEST_STAND", "knopf"))


def _addin_id_aus_adresse(art: str) -> str:
    """Kennung aus der EINGESTELLTEN Außenadresse — dieselbe Formel wie beim
    Ausliefern des Manifests.

    ⚠️ Nicht aus dem Manifest-Abruf übernehmen: Ohne ADDIN_BASE_URL kommt der
    Host dort aus X-Forwarded-Host, und das Manifest ist anonym abrufbar —
    jeder könnte so eine Kennung in settings.json schreiben
    (tests/test_addin_manifest.py::test_fremde_adresse_…)."""
    from urllib.parse import urlsplit
    import aussenadresse
    host = urlsplit(aussenadresse.konfiguriert() or "").hostname
    if not host:
        return ""
    if art == "verfassen":
        from webui.routen.addin_verfassen import addin_id
        return addin_id(host)
    from webui.routen.addin import knopf_addin_id
    return knopf_addin_id(host)


def _addin_soll() -> list[dict]:
    """Die Add-ins, deren ausgelieferte Fassung je Postfach verglichen wird.

    Nur mit eingeschaltetem Add-in, eingestellter Außenadresse und wenn das
    Manifest schon einmal ausgeliefert wurde (sonst gibt es keine Version)."""
    if not settings_store.get("ADDIN_ENABLED"):
        return []
    # Soll frisch bestimmen statt den zuletzt ABGERUFENEN Stand zu lesen — der
    # blieb nach einem Update alt, und alt gegen alt hieß „aktuell“ (v1.12.42).
    try:
        from webui.routen import addin as _addin
        _addin.manifest_staende_auffrischen()
    except Exception as exc:                                # noqa: BLE001
        log.warning("Add-in-Fassungen nicht aufgefrischt: %s", exc)
    soll = []
    for schluessel, art in _ADDINS:
        stand = settings_store.get(schluessel) or {}
        if not (isinstance(stand, dict) and stand.get("version")):
            continue
        kennung = _addin_id_aus_adresse(art)
        if kennung:
            soll.append({"art": art, "id": str(kennung), "version": str(stand["version"]),
                         "seit": str(stand.get("seit") or _versionsdatum(stand["version"]))})
    return soll


_CHANGELOG = Path(__file__).resolve().parent / "CHANGELOG.md"      # im Image: /app/CHANGELOG.md
if not _CHANGELOG.exists():
    _CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def _versionsdatum(version: str) -> str:
    """Datum einer Gateway-Version laut Changelog (ISO) — Rückfall für einen
    Add-in-Stand ohne `seit` (vor v1.9.176). 1.9.146.1 zählt als 1.9.146."""
    import re
    kern = ".".join(str(version).split(".")[:3])
    try:
        text = _CHANGELOG.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"^## v" + re.escape(kern) + r" — (\d{4}-\d{2}-\d{2})", text, re.M)
    return m.group(1) if m else ""


def _versionstupel(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(t) for t in str(v).split("."))
    except ValueError:
        return (0,)


def _katalog_merken(katalog: dict) -> None:
    """Katalogfassungen ({Add-in-Kennung: Version}) je Art merken — siehe
    addin_verteilung.katalog_gelesen. Gescheiterte Abfragen ("!…") zählen nicht."""
    import addin_verteilung as av
    for a in _addin_soll():
        v = katalog.get(a["id"])
        if isinstance(v, str) and not v.startswith("!"):
            av.katalog_gelesen(a["art"], v.strip())


_NICHT_GEFUNDEN = re.compile(r"couldn'?t be found|could not be found|wurde nicht gefunden", re.I)


def _check_addins(installiert: dict | None) -> dict[str, dict]:
    """Fassung der Outlook-Add-ins, die Exchange dem Postfach ausgeliefert hat,
    gegen die Fassung, die das Gateway ausliefert — EIN Ergebnis je Add-in
    ({art: result} für jede Art aus `_ADDINS`).

    Getrennt, weil die Add-ins unabhängig voneinander aktualisiert und verteilt
    werden: In einer gemeinsamen Zeile färbte ein veraltetes Add-in die ganze
    Zeile gelb, und dass das andere längst aktuell war, ging darin unter.

    Ein über „Integrierte Apps“ bereitgestelltes Add-in übernimmt ein neues
    Manifest erst, wenn ein Admin es dort aktualisiert; danach verteilt
    Microsoft Postfach für Postfach (24–72 h). Ob eine ältere Fassung eine
    Handlungsanweisung (Aktualisieren, später Zuweisung) oder nur „wird
    verteilt“ ist, entscheidet `addin_verteilung.einordnen` — dieselbe
    Einordnung wie die Aufgabe in abnahme._addin_fassung."""
    soll = {a["art"]: a for a in _addin_soll()}
    ergebnis = {}
    for _schluessel, art in _ADDINS:
        a = soll.get(art)
        if not a:
            ergebnis[art] = _make_result("skip", "Nicht eingerichtet")
            continue
        if installiert is None:
            ergebnis[art] = _make_result("skip", "Nicht abgefragt (PowerShell-Fehler)")
            continue
        ist = (installiert.get(a["id"]) or "").strip()
        if ist.startswith("!") and _NICHT_GEFUNDEN.search(ist):
            # „Objekt nicht gefunden“ heisst: dem Postfach nicht ausgeliefert. Das
            # REST-Modul liefert dafür nicht immer ManagementObjectNotFoundException
            # (gesehen 28.11.2026 an einem geteilten Postfach), der Text ist eindeutig.
            ist = ""
        if ist.startswith("!"):
            # Abfrage gescheitert — weder „nicht installiert“ noch eine Fassung.
            grund = ist[1:].lstrip("|").strip()
            ergebnis[art] = _make_result(
                "skip", "Nicht abgefragt — Exchange-Abfrage fehlgeschlagen: " + grund[:120])
            continue
        # EINE Einordnung für Postfachprüfung und Aufgabe (addin_verteilung):
        # Ist die Soll-Fassung schon bei einem Postfach angekommen, ist das
        # Aktualisieren im Admin Center erledigt — dann nur Verteilung, keine
        # Handlungsanweisung.
        import addin_verteilung as av
        av.gesehen(art, a["version"], ist)
        lage = av.einordnen(art, a["version"], ist)
        if lage == av.AKTUELL:
            ergebnis[art] = _make_result("ok", f"{ist} — aktuell")
        elif lage == av.NICHT_INSTALLIERT:
            ergebnis[art] = _make_result("skip", "Nicht installiert")
        elif lage == av.VERTEILUNG:
            ergebnis[art] = _make_result(
                "info", f"{ist} statt {a['version']} — wird verteilt. Nichts zu tun; "
                        "Exchange braucht bis zu 72 Stunden.")
        elif lage == av.ZUWEISUNG:
            ergebnis[art] = _make_result(
                "warn", f"{ist or 'nicht installiert'} statt {a['version']}. "
                        "Zuweisung prüfen: " + av.ZUWEISUNG_WEG)
        else:
            seit = ""
            if a.get("seit"):
                j, m, t = a["seit"].split("-")
                seit = f" (seit {t}.{m}.{j})"
            # Kurz halten (Handy). Das Admin Center zeigt keine Versionsnummer,
            # nur „Last modified“ — daher der Abgleich mit dem Datum.
            ergebnis[art] = _make_result(
                "warn", f"{ist} statt {a['version']}{seit}. Integrierte Apps → Add-in → "
                        "Aktualisieren; „Last modified“ vor diesem Datum = noch nicht "
                        "aktualisiert. Danach bis 72 h.")
        ergebnis[art].update(ist=ist, soll=a["version"], lage=lage)
    # Anzeigename wie im Admin Center — dort sucht man das Add-in.
    from webui.routen.addin import addin_namen
    for art, name in addin_namen().items():
        ergebnis[art]["name"] = name
    return ergebnis


# ── Check 4: template ─────────────────────────────────────────────────────────

def _vorlage(email: str, cfg: dict) -> str:
    """Die Signaturvorlage, die das Postfach im Betrieb TATSÄCHLICH bekommt —
    dieselbe Auflösung wie handler.py (nicht das rohe `template`-Feld, das bei
    einem Postfach ohne eigene Wahl fehlt)."""
    import policies
    return policies.vorlage_fuer(email, "sig", sender_cfg=cfg)


async def _check_template(email: str, cfg: dict) -> dict:
    """Try to render the signature template for this user."""
    try:
        import graph_client
        import signature_engine
        user = await graph_client.get_user(email)
        # ⚠️ Nicht `render()` und auf eine Ausnahme warten: render fängt jeden
        # Fehler selbst ab und liefert "" — diese Prüfung meldete bis v1.9.134
        # deshalb jede kaputte Vorlage als „ok". render_fehler sagt es.
        template_name = _vorlage(email, cfg)
        fehler = signature_engine.render_fehler(user, template_name=template_name)
        if fehler:
            return _make_result("error", f"Vorlage {template_name or 'default'}: {fehler}")
        if not user.displayName:
            # Ohne Entra-Datensatz baut get_user ein LEERES Profil — die Meldung
            # hieß dann „displayName leer", obwohl das Konto einen Namen hat und
            # nur nicht gefunden wurde. Zuordnungsfehler und Datenfehler getrennt
            # melden.
            if not user.entra_gefunden:
                return _make_result("warn", "Entra-Konto zu dieser Adresse nicht gefunden — "
                                    "Signatur ohne Benutzerdaten (Name, Variablen)")
            return _make_result("warn", "Benutzerdaten unvollständig (displayName leer)")
        return _make_result("ok", f"Signatur „{template_name or 'default'}“ mit den Daten "
                            "des Postfachs fehlerfrei erstellt")
    except Exception as exc:
        return _make_result("error", f"Template-Fehler: {exc}")


# ── Check 5: smime_cert ───────────────────────────────────────────────────────

def _check_smime_cert(email: str) -> dict:
    """Check S/MIME certificate validity and expiry."""
    import smime_store
    warn_days = int(settings_store.get("CERT_WARN_DAYS") or 14)
    try:
        certs = smime_store.list_user_certs(email)
    except Exception as exc:
        return _make_result("error", f"Fehler beim Laden: {exc}")

    if not certs:
        return _make_result("error", "Kein Zertifikat gefunden")

    # Use the default cert
    default = next((c for c in certs if c.get("is_default")), certs[0])
    if default.get("error"):
        return _make_result("error", f"Zertifikat ungültig: {default['error']}")

    days = default.get("days_left", 999)
    expiry = default.get("expiry", "?")
    if default.get("expired"):
        return _make_result("error", f"Abgelaufen seit {abs(days)} Tagen ({expiry})")
    if days <= warn_days:
        return _make_result("warn", f"Läuft in {days} Tagen ab ({expiry})")
    return _make_result("ok", f"Gültig noch {days} Tage (bis {expiry})")


# ── Check 6: smime_key ────────────────────────────────────────────────────────

def _check_smime_key(email: str) -> dict:
    """Check whether a signing key is available (local, backup, or Key Vault)."""
    import smime_store
    try:
        # Signierfähig = ein nicht widerrufenes Zertifikat mit Schlüssel.
        paths = smime_store.get_signing_paths(email, allow_backup=True, nur_unwiderrufen=True)
    except Exception as exc:
        return _make_result("error", f"Fehler: {exc}")

    if paths:
        cert_path, key_path = paths
        if key_path.name == "key.pem.bak":
            return _make_result("ok", "Backup-Schlüssel vorhanden")
        return _make_result("ok", "Lokaler Schlüssel vorhanden")

    # Kein lokaler Schlüssel/Backup — nur „ok/Key Vault" melden, wenn für DIESES
    # Postfach tatsächlich ein Schlüssel im Key Vault liegt. Ein global gesetztes
    # KEYVAULT_URL sagt darüber nichts; die autoritative Auskunft gibt
    # default_key_location() (dieselbe Quelle wie der kv_sign-Check) — sonst
    # behauptet die Anzeige „Key Vault" bei einem Postfach ganz ohne Schlüssel.
    if smime_store.default_key_location(email) == "kv":
        # „kv“ heisst nur: Zertifikat da, lokal kein Schlüssel. Ohne eingetragenen
        # Key Vault liegt er nirgends — bis v1.12.42 stand dann trotzdem „ok“.
        if not (settings_store.get("KEYVAULT_URL") or "").strip():
            return _make_result("error", "Zertifikat ohne Schlüssel: lokal keiner, "
                                         "und kein Key Vault eingetragen")
        return _make_result("ok", "Key Vault")

    return _make_result("error", "Kein Schlüssel gefunden (lokal + Key Vault)")


# ── Check 7: kv_sign ─────────────────────────────────────────────────────────

async def _check_kv_sign(email: str) -> dict:
    """Test the Key Vault sign API with a health-check digest."""
    import keyvault
    import smime_store
    from cms_sign import _get_cert_key_type, _parse_cert_der
    try:
        digest = hashlib.sha256(b"health-check").digest()
        # Determine algorithm from cert key type (EC→ES256, RSA→RS256)
        algo = "RS256"
        try:
            cert_path = smime_store.get_signing_cert_path(email)
            if cert_path and cert_path.exists():
                cert_pem = cert_path.read_bytes()
                cert_der = _parse_cert_der(cert_pem)
                algo = "ES256" if _get_cert_key_type(cert_der) == "EC" else "RS256"
        except Exception:
            pass
        # zaehlen=False: die Probe ist keine Nachrichten-Signatur, sondern eine
        # Schlüssel-Prüfung — zählt in kv_key_pruefung, nicht in kv_sign_calls.
        await keyvault.sign(email, digest, algorithm=algo, zaehlen=False)
        return _make_result("ok", "Sign API erreichbar")
    except Exception as exc:
        return _make_result("error", f"Sign API Fehler: {exc}")


# ── Check: Graph-Zugriff auf das Postfach ─────────────────────────────────────

def _graph_fehlertext(resp) -> str:
    try:
        return str(resp.json().get("error", {}).get("message") or "")[:200]
    except Exception:                                          # noqa: BLE001
        return (resp.text or "")[:200]


async def _check_graph_postfach(email: str, token: str | None) -> dict:
    """Darf die App per Graph auf dieses Postfach zugreifen?

    Darüber laufen Zustellung und Ablage (Modi graph/graph_plus). Postfächer mit
    Premium-Lizenzen (z. B. Copilot) verlangen RBAC for Applications; ohne
    Zuweisung antwortet Graph 403 „[RAOP]“, während andere Postfächer derselben
    App funktionieren — Post dieses Absenders blieb dann still liegen
    (VM 06./07.09.2026). `Test-ApplicationAccessPolicy` erkennt das NICHT, nur
    ein echter Aufruf. Gelesen wird nur die Kennung des Posteingangs."""
    if not token:
        return _make_result("skip", "Kein Graph-Token")
    import httpx
    url = f"https://graph.microsoft.com/v1.0/users/{email}/mailFolders/inbox?$select=id"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    except Exception as exc:                                   # noqa: BLE001
        return _make_result("skip", f"Graph nicht erreichbar: {exc}"[:200])
    if resp.status_code == 200:
        return _make_result("ok", "Graph darf auf das Postfach zugreifen")
    text = _graph_fehlertext(resp)
    if resp.status_code == 403:
        hilfe = ("Bei Postfächern mit Premium-Lizenz (z. B. Copilot) verlangt Exchange "
                 "RBAC for Applications: Postfächer → Speichern legt die Zuweisung an, "
                 "wenn die App-Zugriffsbeschränkung eingeschaltet ist.")
        return _make_result("error", f"Graph verweigert den Zugriff (403): {text} — {hilfe}")
    return _make_result("warn", f"Graph antwortet {resp.status_code}: {text}")


async def _check_abwesenheit(email: str, token: str | None) -> dict:
    """Kann das Gateway die Abwesenheit dieses Postfachs lesen (und damit setzen)?
    Nur mit eingeschalteter zentraler Abwesenheit."""
    if not settings_store.get("ABWESENHEIT_AKTIV"):
        return _make_result("skip", "Zentrale Abwesenheit ausgeschaltet")
    if not token:
        return _make_result("skip", "Kein Graph-Token")
    import abwesenheit
    zustand, _ = await abwesenheit._get_setting(email, token)
    if zustand == "ok":
        return _make_result("ok", "Abwesenheit lesbar")
    if zustand == "kein_zugriff":
        return _make_result("error", "Kein Zugriff auf die Abwesenheit (403) — Berechtigung "
                            "MailboxSettings.ReadWrite prüfen: Abwesenheit → Einstellungen")
    return _make_result("warn", "Abwesenheit nicht lesbar (siehe Protokoll)")


async def _check_ews_zugriff(email: str) -> dict:
    """Hybrid-Nutzer mit Anbindung an Exchange lokal: Kann das Gateway die
    Abwesenheit per EWS lesen (GetUserOofSettings)? Nur mit eingeschalteter
    zentraler Abwesenheit — wie die Graph-Prüfung."""
    if not settings_store.get("ABWESENHEIT_AKTIV"):
        return _make_result("skip", "Zentrale Abwesenheit ausgeschaltet")
    alt = _frisch(_alt_check(email, "abwesenheit"), _EWS_GUELTIG)
    if alt and str(alt.get("detail", "")).startswith("EWS-Zugriff"):
        return alt
    import ews_onprem
    erg = await asyncio.to_thread(ews_onprem.verbindung_testen, email)
    if erg["ok"]:
        return _make_result("ok", "EWS-Zugriff: Abwesenheit lesbar (Exchange lokal)")
    return _ews_fehlschlag(email, erg["code"], f"EWS-Zugriff: {erg['detail']}")


def _ews_fehlschlag(email: str, code: str, text: str) -> dict:
    """Ergebnis einer gescheiterten EWS-Probe — EINE Fassung für Abwesenheit und
    Posteingang. Eine Ablehnung (KEIN_ZUGRIFF_CODES) trägt `ews_code` für die
    Aufgabe abnahme._ews_bereich — AUSSER das Postfach ist frisch in die Gruppe
    des Dienstkontos aufgenommen (ews_bereich.wartet): Exchange übernimmt das
    verzögert, das ist in dieser Zeit kein Fehler (seit v1.12.92)."""
    import ews_bereich
    import ews_onprem
    if code in ews_onprem.KEIN_ZUGRIFF_CODES and ews_bereich.wartet(email):
        return _make_result("warn", (
            f"{text} — wartet: frisch in die Gruppe {ews_bereich.gruppe_name()} aufgenommen, "
            f"Exchange übernimmt das verzögert (bis {ews_bereich.WARTEZEIT_S // 3600} h)")[:300])
    stufe = ("error" if code in ews_onprem.KEIN_ZUGRIFF_CODES
             or code in ("anmeldung", "tls", "konfiguration") else "warn")
    erg_pruefung = _make_result(stufe, text[:300])
    if code in ews_onprem.KEIN_ZUGRIFF_CODES:
        erg_pruefung["ews_code"] = code             # für die Aufgabe abnahme._ews_bereich
    return erg_pruefung


async def _check_postfach_lesen(email: str, smime_active) -> dict:
    """Hybrid-Nutzer mit S/MIME: Kann das Gateway die Mail der
    Zertifizierungsstelle lesen (CASTLE-Challenge, Bestätigungslink)? Über
    dieselbe Weiche wie der Bezug selbst (ews_onprem.postfach_weg). Gelesen
    wird die Kennung der jüngsten Mail — kein Betreff, kein Text."""
    if not smime_active:
        return _make_result("skip", "S/MIME nicht aktiviert")
    import ews_onprem
    if ews_onprem.postfach_weg(email) != ews_onprem.WEG_EWS:
        return _make_result("skip", "Zertifikatsbezug nur per Import — Anbindung an "
                                    "Exchange lokal (EWS) nicht eingerichtet")
    alt = _frisch(_alt_check(email, "postfach_lesen"), _EWS_GUELTIG)
    if alt:
        return alt
    try:
        await asyncio.to_thread(ews_onprem.posteingang_suchen, email, 1)
    except ews_onprem.EwsFehler as exc:
        return _ews_fehlschlag(email, exc.code, f"Posteingang über EWS nicht lesbar: {exc.detail}")
    except Exception as exc:                                       # noqa: BLE001
        return _make_result("warn", f"Posteingang über EWS nicht lesbar: {exc}"[:300])
    return _make_result("ok", "Posteingang über EWS lesbar (Zertifikatsbezug möglich)")


# Die Sign-API-Probe kostet je Postfach einen Key-Vault-Aufruf. Ein Erfolg gilt
# einen Tag; ein Fehler wird bei jedem Lauf neu geprüft, damit die Behebung
# sofort sichtbar wird.
_KV_GUELTIG = 24 * 3600


def _kv_frisch(alt: dict | None) -> dict | None:
    return _frisch(alt, _KV_GUELTIG)


def _frisch(alt: dict | None, gueltig: float) -> dict | None:
    """Das letzte Ergebnis, wenn es OK war und jünger als `gueltig` Sekunden."""
    if not alt or alt.get("status") != "ok" or not alt.get("checked_at"):
        return None
    try:
        t = datetime.strptime(alt["checked_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    if (datetime.now(timezone.utc) - t).total_seconds() < gueltig:
        return alt
    return None


# Die EWS-Proben der Hybrid-Nutzer (Abwesenheit lesbar, Posteingang lesbar)
# kosten je Postfach einen Aufruf an die lokale Exchange (Lab: 0,30 s median).
# Bei 500 Hybrid-Nutzern mit S/MIME rund fünf Minuten je Prüflauf im
# Scheduler-Faden, alle 30 Minuten. Ein Erfolg gilt deshalb sechs Stunden; ein
# Fehler wird bei jedem Lauf neu geprüft (Behebung sofort sichtbar). Ob der
# Zugriff im Betrieb noch geht, zeigt ohnehin der Abwesenheitsabgleich alle
# 10 Minuten („kein Zugriff“ in seiner Zählung).
_EWS_GUELTIG = 6 * 3600


def _alt_check(email: str, name: str) -> dict | None:
    return (((settings_store.get("MAILBOX_HEALTH") or {}).get(email) or {})
            .get("checks", {}).get(name))


# ── Overall status ────────────────────────────────────────────────────────────

def _compute_overall(checks: dict) -> str:
    statuses = [v.get("status", "skip") for v in checks.values()]
    if "error" in statuses:
        return "error"
    if "warn" in statuses or "fixed" in statuses:
        return "warn"
    return "ok"


# ── Single mailbox check ──────────────────────────────────────────────────────

async def run_checks_for_mailbox(email: str, exo_data: dict | None = None,
                                 _cfg: dict | None = None, _puffer: dict | None = None) -> dict:
    """
    Run all checks for a single mailbox.
    exo_data: pre-fetched result from _check_exo_batch_sync (avoids duplicate PS sessions).
    Updates settings_store under MAILBOX_HEALTH[email].
    Returns the check result dict for this mailbox.

    `_cfg`/`_puffer` nur aus `run_all_checks`: der vorab aufgelöste Eintrag
    (ein Index statt n Indexaufbauten) und der Schreibpuffer (siehe dort).
    """
    import mailbox_match
    email = email.lower().strip()
    if _cfg is None:
        mailbox_config: dict = settings_store.get("MAILBOX_CONFIG") or {}
        cfg = mailbox_match.match_sender(mailbox_config, email)
    else:
        cfg = _cfg
    reinject_mode = settings_store.reinject_mode()
    kv_url = (settings_store.get("KEYVAULT_URL") or "").strip()
    smime_active = cfg.get("smime_sign") or cfg.get("smime_encrypt") or cfg.get("smime")

    checks: dict = {}

    import exo_mailboxes
    if exo_mailboxes.ist_hybrid(email):
        checks = await _checks_hybrid(email, cfg, exo_data)
        return _ergebnis_speichern(email, checks, _puffer)

    # 1. exo_mailbox (sync, wrapped in executor)
    loop = asyncio.get_event_loop()
    checks["exo_mailbox"] = await loop.run_in_executor(None, _check_exo_mailbox_sync, email)

    # 2+3. dg_member + imap_permission (+ Add-in-Fassung) — aus dem Sammellauf,
    # sonst ein eigener PS-Lauf nur für dieses Postfach.
    if exo_data is None:
        exo_data = await loop.run_in_executor(None, _check_exo_batch_sync, [email])
    data = exo_data.get(email, {})
    err = data.get("error")
    if err:
        checks["dg_member"] = _make_result("error", f"PS-Fehler: {err}")
        checks["imap_permission"] = _make_result("skip", "PS-Fehler")
    else:
        dg_ok = data.get("dg_member", False)
        dg_fixed = data.get("dg_fixed", False)
        if dg_fixed:
            checks["dg_member"] = _make_result("fixed", "Fehlte — automatisch hinzugefügt")
            _append_audit("dg_member_added", email, "Automatisch zur DG hinzugefügt")
        elif dg_ok:
            checks["dg_member"] = _make_result("ok", "Mitglied der Distribution Group")
        else:
            checks["dg_member"] = _make_result("error", "Nicht in Distribution Group")

        if reinject_mode == "graph_plus":
            imap_perm = data.get("imap_perm")
            imap_fixed = data.get("imap_fixed", False)
            if imap_fixed:
                checks["imap_permission"] = _make_result(
                    "fixed", "FullAccess fehlte — automatisch gesetzt")
                _append_audit("imap_permission_set", email, "FullAccess automatisch gesetzt")
            elif imap_perm is True:
                checks["imap_permission"] = _make_result("ok", "FullAccess vorhanden")
            elif imap_perm is False:
                checks["imap_permission"] = _make_result("error", "FullAccess fehlt")
            else:
                checks["imap_permission"] = _make_result("skip", "SP nicht gefunden")
        else:
            checks["imap_permission"] = _make_result("skip", "REINJECT_MODE != graph_plus")
    addins = _check_addins(None if err else data.get("addins"))
    checks["addin_verfassen"] = addins["verfassen"]
    checks["addin_knopf"] = addins["knopf"]

    # 4. template
    if cfg.get("sig"):
        checks["template"] = await _check_template(email, cfg)
    else:
        checks["template"] = _make_result("skip", "Signatur nicht aktiviert")

    # Graph-Zugriff und Abwesenheit — ein Token für beide
    import graph_client
    token = await graph_client._acquire_token_async()
    checks["graph_postfach"] = await _check_graph_postfach(email, token)
    checks["abwesenheit"] = await _check_abwesenheit(email, token)

    checks.update(await _checks_smime(email, smime_active, kv_url))
    return _ergebnis_speichern(email, checks, _puffer)


async def _checks_smime(email: str, smime_active, kv_url: str) -> dict:
    """Schlüssel, Zertifikat und Key Vault — alles am Gateway, kein Postfach.
    EINE Fassung für EXO-Postfächer und Hybrid-Nutzer (seit v1.12.62)."""
    checks: dict = {}
    # 5. smime_cert
    if smime_active:
        checks["smime_cert"] = _check_smime_cert(email)
    else:
        checks["smime_cert"] = _make_result("skip", "S/MIME nicht aktiviert")

    # 6. smime_key
    if smime_active:
        checks["smime_key"] = _check_smime_key(email)
    else:
        checks["smime_key"] = _make_result("skip", "S/MIME nicht aktiviert")

    # 7. kv_sign — decide by where the DEFAULT signing slot's key actually lives.
    # kv_url configured GLOBALLY doesn't mean THIS mailbox's key is in Key Vault,
    # and a leftover local key in a NON-default slot must not make a KV-backed
    # mailbox (kv+backup) look purely local.
    import smime_store as _sst
    key_loc = _sst.default_key_location(email)   # local | kv_backup | kv | none
    if kv_url and smime_active and key_loc in ("kv", "kv_backup"):
        alt = ((settings_store.get("MAILBOX_HEALTH") or {}).get(email) or {}).get("checks", {})
        res = _kv_frisch(alt.get("kv_sign"))
        if res is None:
            res = await _check_kv_sign(email)
            if res.get("status") == "ok" and key_loc == "kv_backup":
                res = _make_result("ok", "Sign API erreichbar (Key Vault + lokales Backup)")
        checks["kv_sign"] = res
    elif smime_active and key_loc == "local":
        checks["kv_sign"] = _make_result("skip", "Lokaler Schlüssel — nicht in Key Vault")
    elif not smime_active:
        checks["kv_sign"] = _make_result("skip", "S/MIME nicht aktiv")
    elif not kv_url:
        checks["kv_sign"] = _make_result("skip", "Key Vault nicht konfiguriert")
    elif key_loc == "none":
        checks["kv_sign"] = _make_result("skip", "Kein Schlüssel für dieses Postfach")
    else:
        checks["kv_sign"] = _make_result("skip", "Kein Key-Vault-Schlüssel für dieses Postfach")
    return checks


def _ergebnis_speichern(email: str, checks: dict, puffer: dict | None = None) -> dict:
    """Ergebnis eines Postfachs ablegen. Ohne `puffer` sofort (Einzelprüfung);
    mit `puffer` (Sammellauf) erst beim nächsten `_puffer_schreiben`.

    Anlass für den Puffer (18.11.2026): `settings.json` trägt MAILBOX_CONFIG
    und MAILBOX_HEALTH aller Postfächer — gemessen 1,7 MB bei 700 Postfächern,
    328 ms je Schreiben auf dem Raspi. Je Postfach geschrieben waren das rund
    vier Minuten und gut 1 GB Schreiblast je Prüflauf (alle 30 min)."""
    result = {"last_checked": _now_str(), "overall": _compute_overall(checks),
              "checks": checks}
    if puffer is None:
        _ergebnisse_schreiben({email: result})
    else:
        puffer[email] = result
    log.info("health_check: %s → overall=%s%s", email, result["overall"],
             " (Schreiben gesammelt)" if puffer is not None else "")
    return result


def _ergebnisse_schreiben(ergebnisse: dict) -> None:
    """Ergebnisse in MAILBOX_HEALTH übernehmen — EIN Schreibvorgang.

    Die Regelprobe läuft nur auf Knopfdruck, nie in dieser Prüfung: Ihr letztes
    Ergebnis wird übernommen, solange es noch gilt (aktives Postfach, Probe nach
    der letzten Aktivierung). Gelesen wird es HIER, unter der Sperre, nicht beim
    Prüfen — eine Probe kann parallel in denselben Eintrag schreiben
    (regel_probe.speichern), auch während ein Sammellauf puffert."""
    if not ergebnisse:
        return
    import mailbox_match
    import regel_probe
    with settings_store._lock:          # RLock: Lesen und Schreiben als ein Schritt
        health: dict = dict(settings_store.get("MAILBOX_HEALTH") or {})
        adr_idx = mailbox_match.build_address_index(settings_store.get("MAILBOX_CONFIG") or {})
        for email, result in ergebnisse.items():
            probe = (health.get(email) or {}).get("regel_probe")
            checks = result["checks"]
            if regel_probe.aktuell(probe, adr_idx.get(email.lower(), {})):
                checks["regel_greift"] = regel_probe.als_pruefung(probe)
                result["regel_probe"] = probe
            else:
                checks.pop("regel_greift", None)
                result.pop("regel_probe", None)
            result["overall"] = _compute_overall(checks)
            health[email] = result
        settings_store.force_update({"MAILBOX_HEALTH": health})


_LOKAL = "Postfach in der lokalen Organisation"


ADDIN_LOKAL_NIE = ("Nicht prüfbar: in Exchange lokal bereitgestellt, Get-App gibt es nur in "
                   "Exchange Online. Noch kein Abruf aus Outlook gesehen — bereitstellen über "
                   "Einrichtung → Outlook Add-in → Exchange lokal.")


def _checks_addin_lokal(email: str) -> dict[str, dict]:
    """Add-ins eines Hybrid-Nutzers (seit v1.12.69). Sie kommen aus Exchange
    lokal (New-App), nicht aus „Integrierte Apps“; Get-App ist von hier nicht
    erreichbar. Beleg ist der letzte Abruf aus Outlook
    (addin_verteilung.lokal_stand). Ohne Abruf „entfällt“ mit Weg — nie ein
    Fehler: Ob der Betreiber das Add-in lokal will, entscheidet er selbst.
    Bewusst ohne `soll`/`ist`: Die Fassungsprüfung der Abnahme
    (abnahme._addin_fassung) zählt nur Postfächer mit Get-App-Ergebnis."""
    import addin_verteilung as av
    from webui.routen.addin import addin_namen
    namen = addin_namen()
    aus = {}
    for schluessel, art in (("addin_verfassen", "verfassen"), ("addin_knopf", "knopf")):
        st = av.lokal_stand(email, art)
        if not settings_store.get("ADDIN_ENABLED"):
            aus[schluessel] = _make_result("skip", "Nicht eingerichtet")
        elif st.get("tag"):
            j, m, t = st["tag"].split("-")
            client = f", {st['client']}" if st.get("client") else ""
            aus[schluessel] = _make_result(
                "ok", f"Exchange lokal — zuletzt aus Outlook abgerufen am {t}.{m}.{j}{client}")
        else:
            aus[schluessel] = _make_result("skip", ADDIN_LOKAL_NIE)
        aus[schluessel]["name"] = namen.get(art, "")
    return aus


async def _checks_hybrid(email: str, cfg: dict, exo_data: dict | None) -> dict:
    """Prüfungen für einen Hybrid-Nutzer (Postfach in der lokalen Exchange-
    Organisation). Add-ins: Abruf aus Outlook statt Get-App (`_checks_addin_lokal`).
    Graph-Zugriff, Abwesenheit und IMAP setzen ein
    EXO-Postfach voraus und entfallen — die Abwesenheit nur, solange keine
    Anbindung an Exchange lokal (EWS) eingerichtet ist; mit ihr wird der
    EWS-Zugriff geprüft. Geprüft werden Bestand in EXO,
    Verteilerliste (nur solange die Option wirkt — sonst nähme die Selbstheilung
    ein Postfach auf, dessen Post das Gateway nicht zurückgeben kann) und Vorlage.

    S/MIME (seit v1.12.62): Zertifikat, Schlüssel und Key Vault wie bei
    EXO-Postfächern (`_checks_smime`, dieselbe Fassung). Dazu `postfach_lesen`:
    Kann das Gateway die Mail der CA lesen (Zertifikatsbezug)? Über EWS geprüft,
    ohne Anbindung „entfällt“ mit Grund — Import geht trotzdem."""
    import exo_mailboxes
    checks: dict = {}
    aktiv = exo_mailboxes.hybrid_aktiv()
    if not aktiv:
        checks["exo_mailbox"] = _make_result(
            "warn", "Hybrid-Nutzer: Option aus oder Rückweg nicht SMTP — wird nicht bearbeitet")
        checks["dg_member"] = _make_result("skip", "Hybrid-Option nicht wirksam")
    else:
        if email in exo_mailboxes.hybrid_addresses():
            checks["exo_mailbox"] = _make_result("ok", f"Hybrid-Nutzer ({_LOKAL})")
        else:
            checks["exo_mailbox"] = _make_result(
                "error", "Hybrid-Nutzer nicht (mehr) in EXO gefunden")
        loop = asyncio.get_event_loop()
        if exo_data is None:
            exo_data = await loop.run_in_executor(None, _check_exo_batch_sync, [email])
        data = (exo_data or {}).get(email, {})
        if data.get("error"):
            checks["dg_member"] = _make_result("error", f"PS-Fehler: {data['error']}")
        elif data.get("dg_fixed"):
            checks["dg_member"] = _make_result("fixed", "Fehlte — automatisch hinzugefügt")
            _append_audit("dg_member_added", email, "Automatisch zur DG hinzugefügt")
        elif data.get("dg_member"):
            checks["dg_member"] = _make_result("ok", "Mitglied der Distribution Group")
        else:
            checks["dg_member"] = _make_result("error", "Nicht in Distribution Group")
    import abwesenheit
    ueber_ews = abwesenheit.ews_verfuegbar()
    for name in ("imap_permission", "graph_postfach", "abwesenheit"):
        if name == "abwesenheit" and ueber_ews:
            checks[name] = await _check_ews_zugriff(email)
            continue
        checks[name] = _make_result("skip", _LOKAL)
    checks.update(_checks_addin_lokal(email))
    smime_active = cfg.get("smime_sign") or cfg.get("smime_encrypt") or cfg.get("smime")
    checks.update(await _checks_smime(email, smime_active,
                                      (settings_store.get("KEYVAULT_URL") or "").strip()))
    checks["postfach_lesen"] = await _check_postfach_lesen(email, smime_active)
    if cfg.get("sig"):
        checks["template"] = await _check_template(email, cfg)
    else:
        checks["template"] = _make_result("skip", "Signatur nicht aktiviert")
    return checks


# ── All mailboxes ─────────────────────────────────────────────────────────────

def _veraltete_entfernen(aktuell: list[str]) -> None:
    """Status von Postfächern löschen, die nicht mehr konfiguriert sind.

    Ein abgeschaltetes Postfach behielt seinen letzten Status — auf der VM
    standen zwei Einträge vom 30.06. mit „Fehler“, die nie wieder geprüft
    wurden. Nur im Lauf über ALLE Postfächer (eine Teilliste sagt nichts
    darüber, was sonst noch konfiguriert ist)."""
    health: dict = dict(settings_store.get("MAILBOX_HEALTH") or {})
    soll = {e.lower() for e in aktuell}
    weg = [e for e in health if e.lower() not in soll]
    if weg:
        for e in weg:
            health.pop(e, None)
        settings_store.force_update({"MAILBOX_HEALTH": health})
        log.info("health_check: Status nicht mehr konfigurierter Postfächer entfernt: %d", len(weg))


def _verteilung_vormerken(exo_data: dict) -> None:
    """Vor der Einzelbewertung: Hat IRGENDEIN Postfach dieses Laufs schon die
    Soll-Fassung, startet die Verteilungsuhr — sonst bekäme ein Postfach, das in
    der Reihenfolge vor dem ersten aktuellen kommt, „Aktualisieren“ gemeldet."""
    import addin_verteilung as av
    for a in _addin_soll():
        for daten in (exo_data or {}).values():
            if isinstance(daten, dict) and not daten.get("error"):
                av.gesehen(a["art"], a["version"], ((daten.get("addins") or {}).get(a["id"]) or ""))


SCHREIB_TAKT = 25      # Sammellauf: so viele Ergebnisse je Schreibvorgang


async def run_all_checks(emails: list[str] | None = None) -> dict:
    """
    Run checks for all configured mailboxes (or given list).
    Uses a single PS session for EXO batch checks (efficiency).
    Updates settings_store. Returns {email: result_dict}.
    """
    import mailbox_match
    mailbox_config: dict = settings_store.get("MAILBOX_CONFIG") or {}
    if emails is None:
        emails = mailbox_match.configured_addresses(mailbox_config)
        _veraltete_entfernen(emails)
    if not emails:
        log.info("health_check: no mailboxes configured — skipping")
        return {}

    log.info("health_check: running checks for %d mailbox(es): %s",
             len(emails), ", ".join(emails))

    # Run the EXO batch PS check once for all emails — ohne Hybrid-Nutzer, deren
    # Option gerade nicht wirkt: Der Sammellauf nimmt fehlende Mitglieder
    # selbsttätig in die Verteilerliste auf (siehe _checks_hybrid).
    import exo_mailboxes
    _hybrid_wirkt = exo_mailboxes.hybrid_aktiv()
    sammel = [e for e in emails if _hybrid_wirkt or not exo_mailboxes.ist_hybrid(e)]
    loop = asyncio.get_event_loop()
    # Ohne Get-App für Hybrid-Nutzer: bestimmt _check_exo_batch_sync selbst.
    exo_data = await loop.run_in_executor(None, _check_exo_batch_sync, sammel) if sammel else {}
    _verteilung_vormerken(exo_data)

    # Ein Index statt eines Indexaufbaus je Postfach (mailbox_match.match_sender).
    adr_idx = mailbox_match.build_address_index(mailbox_config)
    # Gesammelt schreiben: alle SCHREIB_TAKT Postfächer und am Ende (auch nach
    # einem Abbruch) — siehe _ergebnis_speichern. Die Übersicht füllt sich so
    # weiter schrittweise, ohne je Postfach die ganze settings.json zu schreiben.
    puffer: dict = {}
    results = {}
    try:
        for email in emails:
            try:
                results[email] = await run_checks_for_mailbox(
                    email, exo_data=exo_data,
                    _cfg=adr_idx.get(email.lower().strip(), {}), _puffer=puffer)
            except Exception as exc:
                log.error("health_check: error for %s: %s", email, exc)
                results[email] = {
                    "last_checked": _now_str(),
                    "overall": "error",
                    "checks": {"exo_mailbox": _make_result("error", str(exc))},
                }
            if len(puffer) >= SCHREIB_TAKT:
                _ergebnisse_schreiben(puffer)
                puffer.clear()
    finally:
        _ergebnisse_schreiben(puffer)

    return results


# ── Nach einer Änderung der Anbindung an Exchange lokal (seit v1.12.72) ───────
#
# Die Prüfungen „Abwesenheit“ (EWS-Zugriff) und „Postfach lesen (EWS)“ der
# Hybrid-Nutzer hängen an EWS_ONPREM_*. Ohne neuen Lauf zeigte der Status nach
# dem Einrichten weiter „entfällt“ (Stand von vor der Einrichtung) — bis zum
# nächsten Prüflauf oder „Status aktualisieren“. Ein Erfolg gilt sonst sechs
# Stunden (_EWS_GUELTIG): Ein „OK“ der ALTEN Anbindung darf eine neue, kaputte
# nicht überdecken — deshalb werden beide Einträge vor dem Lauf verworfen.

EWS_SCHLUESSEL = ("EWS_ONPREM_URL", "EWS_ONPREM_KONTO", "EWS_ONPREM_KENNWORT",
                  "EWS_ONPREM_VERBINDEN", "EWS_ONPREM_CA")
EWS_PRUEFUNGEN = ("abwesenheit", "postfach_lesen")
_ews_lauf: set = set()            # höchstens EIN Nachlauf (Referenz gegen den GC)


def ews_geaendert(neu: dict, alt_get) -> bool:
    """Ändert `neu` (bereinigte Einstellungen vor dem Speichern) die Anbindung?
    `alt_get` liest den gespeicherten Stand (settings_store.get)."""
    return any(k in neu and (neu[k] or "") != (alt_get(k) or "") for k in EWS_SCHLUESSEL)


def _hybrid_konfiguriert() -> list[str]:
    import exo_mailboxes
    import mailbox_match
    cfg = settings_store.get("MAILBOX_CONFIG") or {}
    return [e for e in mailbox_match.configured_addresses(cfg) if exo_mailboxes.ist_hybrid(e)]


def _ews_ergebnisse_verwerfen(emails: list[str]) -> None:
    with settings_store._lock:
        health = dict(settings_store.get("MAILBOX_HEALTH") or {})
        geaendert = False
        for e in emails:
            eintrag = health.get(e)
            if not isinstance(eintrag, dict) or not isinstance(eintrag.get("checks"), dict):
                continue
            checks = dict(eintrag["checks"])
            weg = [n for n in EWS_PRUEFUNGEN if checks.pop(n, None) is not None]
            if weg:      # Liste, nicht any(Generator): der bräche nach dem ersten ab
                health[e] = {**eintrag, "checks": checks}
                geaendert = True
        if geaendert:
            settings_store.force_update({"MAILBOX_HEALTH": health})


def nach_ews_aenderung() -> str:
    """Hybrid-Nutzer im Hintergrund neu prüfen. Aus dem Speicherweg
    (POST /settings), im Event-Loop. → "gestartet" | "laeuft" | "keine".
    Gedrosselt: Läuft schon ein Nachlauf, startet kein zweiter — er liest die
    Anbindung ohnehin erst beim jeweiligen Postfach."""
    if _ews_lauf:
        log.info("health_check: Anbindung an Exchange lokal geändert — Nachlauf läuft schon")
        return "laeuft"
    emails = _hybrid_konfiguriert()
    if not emails:
        return "keine"
    _ews_ergebnisse_verwerfen(emails)

    async def _lauf():
        try:
            await run_all_checks(emails)
            log.info("health_check: Nachlauf nach Änderung der Anbindung an Exchange lokal "
                     "— %d Hybrid-Nutzer geprüft", len(emails))
        except Exception as exc:                               # noqa: BLE001
            log.warning("health_check: Nachlauf nach Änderung der Anbindung gescheitert — %s", exc)
    aufgabe = asyncio.get_running_loop().create_task(_lauf())
    _ews_lauf.add(aufgabe)
    aufgabe.add_done_callback(_ews_lauf.discard)
    log.info("health_check: Anbindung an Exchange lokal geändert — %d Hybrid-Nutzer werden "
             "neu geprüft", len(emails))
    return "gestartet"
