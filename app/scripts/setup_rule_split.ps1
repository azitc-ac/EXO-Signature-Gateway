#Requires -Modules ExchangeOnlineManagement
<#
.SYNOPSIS
    Bypass-Wächter Phase 1: pflegt ZWEI Routing-Wege statt einem.

    - Signatur-Weg (bypass-fähig): Regel "Route via <GatewayName>" + DG
      "<GatewayName> - Enabled Mailboxes" — nur reine Signatur-Postfächer.
    - S/MIME-Weg (Warte-Regel): Regel "Route via <GatewayName> (S/MIME)" + DG
      "<GatewayName> - SMIME Mailboxes" — verschlüsselungsfähige Postfächer.

    Der externe Wächter darf im Ausfall NUR den Signatur-Weg abschalten; der
    S/MIME-Weg bleibt an, damit verschlüsselungsfähige Post in der Queue wartet
    (statt unverschlüsselt hinauszugehen).

    Invarianten (wie setup_exo_connector/update_mailbox_dg): das FromMemberOf-Gate
    ist IMMER gesetzt (nie geleert); eine Regel ist genau dann aktiv, wenn ihre DG
    Mitglieder hat; keine empfängerbezogene Bedingung (Bifurkation).

    Idempotent, mehrfach ausführbar. Erzeugt fehlende DGs/Regeln, synchronisiert
    Mitglieder, setzt Gate + Aktivierung. Verändert NICHT den Nicht-Split-Pfad.
#>
param(
    [Parameter(Mandatory)][string]$AppId,
    [Parameter(Mandatory)][string]$Organization,
    [Parameter(Mandatory)][string]$CertPath,
    [string]$GatewayName = "EXO Signature Gateway",
    [string]$SigMembers = "",
    [string]$SmimeMembers = "",
    [string]$LoopHeader = "X-Sig-Applied"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Step([string]$m) { Write-Host "[SPLIT] $m" -ForegroundColor Cyan }
function Write-OK([string]$m)   { Write-Host "[OK] $m"     -ForegroundColor Green }
function Write-Warn([string]$m) { Write-Host "[WARN] $m"   -ForegroundColor Yellow }

function Split-List([string]$csv) {
    return @(if ($csv) { $csv -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" } } else { @() })
}

$managedBy   = "##Managed by $GatewayName (Split), last update: $(Get-Date -Format 'yyyy-MM-dd HH:mm')##"
$sigDg       = "$GatewayName - Enabled Mailboxes"
$smimeDg     = "$GatewayName - SMIME Mailboxes"
$sigRule     = "Route via $GatewayName"
$smimeRule   = "Route via $GatewayName (S/MIME)"
$connector   = "$GatewayName - Outbound"

$sigList   = Split-List $SigMembers
$smimeList = Split-List $SmimeMembers

# ── DG anlegen + Mitglieder synchronisieren ──────────────────────────────────
function Sync-Dg([string]$dgName, [string[]]$desired) {
    $dg = Get-DistributionGroup -Identity $dgName -ErrorAction SilentlyContinue
    if (-not $dg) {
        Write-Step "Creating DG '$dgName'..."
        New-DistributionGroup -Name $dgName -Type Distribution `
            -MemberJoinRestriction Closed -MemberDepartRestriction Closed | Out-Null
        Start-Sleep -Seconds 5
        $dg = Get-DistributionGroup -Identity $dgName
    }
    $current = @(Get-DistributionGroupMember -Identity $dgName -ResultSize Unlimited |
        ForEach-Object { $_.PrimarySmtpAddress.ToLower() })
    $want = @($desired | ForEach-Object { $_.ToLower() })
    foreach ($m in ($want | Where-Object { $_ -notin $current })) {
        try { Add-DistributionGroupMember -Identity $dgName -Member $m -ErrorAction Stop; Write-OK "  + $m" }
        catch { Write-Host "[ERROR] add $m : $_" -ForegroundColor Red }
    }
    foreach ($m in ($current | Where-Object { $_ -notin $want })) {
        try { Remove-DistributionGroupMember -Identity $dgName -Member $m -Confirm:$false -ErrorAction Stop; Write-OK "  - $m" }
        catch { Write-Host "[ERROR] remove $m : $_" -ForegroundColor Red }
    }
}

# ── Regel: Gate IMMER auf die DG, Aktivierung nach Mitgliederzahl ─────────────
function Set-RuleGate([string]$ruleName, [string]$dgName, [int]$count) {
    Set-TransportRule -Identity $ruleName -FromMemberOf @($dgName) -Comments $managedBy | Out-Null
    if ($count -gt 0) {
        Enable-TransportRule -Identity $ruleName -Confirm:$false | Out-Null
        Write-OK "Rule '$ruleName': gate=$dgName, aktiviert ($count)"
    } else {
        Disable-TransportRule -Identity $ruleName -Confirm:$false | Out-Null
        Write-OK "Rule '$ruleName': gate=$dgName (leer), DEAKTIVIERT"
    }
}

# ── Connect ──────────────────────────────────────────────────────────────────
$cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
    $CertPath, [string]$null,
    ([System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet))
Connect-ExchangeOnline -AppId $AppId -Certificate $cert -Organization $Organization `
    -ShowBanner:$false -ShowProgress:$false | Out-Null

try {
    $outConn = Get-OutboundConnector -Identity $connector -ErrorAction SilentlyContinue
    if (-not $outConn) { throw "Outbound Connector '$connector' not found — run connector setup first." }

    # ── Signatur-Weg ─────────────────────────────────────────────────────────
    Sync-Dg $sigDg $sigList
    if (-not (Get-TransportRule -Identity $sigRule -ErrorAction SilentlyContinue)) {
        throw "Signatur-Regel '$sigRule' fehlt — Connector-Setup zuerst ausfuehren."
    }
    Set-RuleGate $sigRule $sigDg $sigList.Count

    # ── S/MIME-Weg ───────────────────────────────────────────────────────────
    Sync-Dg $smimeDg $smimeList
    if (-not (Get-TransportRule -Identity $smimeRule -ErrorAction SilentlyContinue)) {
        Write-Step "Creating S/MIME rule '$smimeRule' (DEAKTIVIERT, gegated)..."
        New-TransportRule `
            -Name $smimeRule `
            -FromScope InOrganization `
            -FromMemberOf @($smimeDg) `
            -ExceptIfHeaderMatchesMessageHeader $LoopHeader `
            -ExceptIfHeaderMatchesPatterns "1" `
            -ExceptIfMessageTypeMatches Calendaring `
            -RouteMessageOutboundConnector $outConn.Identity `
            -Priority 0 `
            -Comments $managedBy `
            -Enabled $false `
            -Mode Enforce | Out-Null
        Write-OK "S/MIME-Regel angelegt"
    }
    Set-RuleGate $smimeRule $smimeDg $smimeList.Count

    Write-OK "Split fertig. Signatur: $($sigList.Count) | S/MIME: $($smimeList.Count)"
} finally {
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
}
