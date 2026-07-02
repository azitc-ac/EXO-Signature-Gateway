#Requires -Modules ExchangeOnlineManagement
<#
.SYNOPSIS
    Creates/updates "EXO Signature Gateway - Enabled Mailboxes" Distribution Group
    and updates the transport rule to use DG membership as condition.
#>
param(
    [Parameter(Mandatory)][string]$AppId,
    [Parameter(Mandatory)][string]$Organization,
    [Parameter(Mandatory)][string]$CertPath,
    [string]$GatewayName = "EXO Signature Gateway",
    [string]$Members = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Python passes members as a single comma-separated string — split into array
$MemberList = if ($Members) {
    @($Members -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" })
} else { @() }

function Write-Step([string]$msg) { Write-Host "[DG-SETUP] $msg" -ForegroundColor Cyan }
function Write-OK([string]$msg)   { Write-Host "[OK] $msg"       -ForegroundColor Green }
function Write-Warn([string]$msg) { Write-Host "[WARN] $msg"     -ForegroundColor Yellow }

$managedBy = "##Managed by $GatewayName, last update: $(Get-Date -Format 'yyyy-MM-dd HH:mm')##"

$dgName   = "$GatewayName - Enabled Mailboxes"
$ruleName = "Route via $GatewayName"

# ── Load certificate ──────────────────────────────────────────────────────────
Write-Step "Loading certificate from $CertPath"
$cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
    $CertPath, [string]$null,
    ([System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet -bor
     [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::Exportable)
)
if (-not $cert.HasPrivateKey) { throw "PFX missing private key" }
Write-OK "Certificate loaded: $($cert.Subject)"

# ── Connect ───────────────────────────────────────────────────────────────────
Write-Step "Connecting to Exchange Online: org=$Organization"
Connect-ExchangeOnline -AppId $AppId -Certificate $cert -Organization $Organization `
    -ShowBanner:$false -ShowProgress:$false
Write-OK "Connected"

try {
    # ── Distribution Group ────────────────────────────────────────────────────
    Write-Step "Checking Distribution Group '$dgName'..."
    $dg = Get-DistributionGroup -Identity $dgName -ErrorAction SilentlyContinue
    if (-not $dg) {
        Write-Step "Creating '$dgName'..."
        New-DistributionGroup -Name $dgName -Type Distribution `
            -MemberJoinRestriction Closed -MemberDepartRestriction Closed | Out-Null
        Start-Sleep -Seconds 5
        $dg = Get-DistributionGroup -Identity $dgName
        Write-OK "Distribution Group created"
    } else {
        Write-OK "Distribution Group exists"
    }

    # ── Sync members ──────────────────────────────────────────────────────────
    Write-Step "Syncing members ($($MemberList.Count) configured)..."
    $current = @(Get-DistributionGroupMember -Identity $dgName -ResultSize Unlimited |
        ForEach-Object { $_.PrimarySmtpAddress.ToLower() })

    $toAdd    = $MemberList | Where-Object { $_.ToLower() -notin $current }
    $toRemove = $current | Where-Object { $_ -notin ($MemberList | ForEach-Object { $_.ToLower() }) }

    foreach ($m in $toAdd) {
        try {
            Add-DistributionGroupMember -Identity $dgName -Member $m -ErrorAction Stop
            Write-OK "Added: $m"
        } catch {
            Write-Host "[ERROR] Failed to add $m : $_" -ForegroundColor Red
        }
    }
    foreach ($m in $toRemove) {
        try {
            Remove-DistributionGroupMember -Identity $dgName -Member $m -Confirm:$false -ErrorAction Stop
            Write-OK "Removed: $m"
        } catch {
            Write-Host "[ERROR] Failed to remove $m : $_" -ForegroundColor Red
        }
    }
    if (-not $toAdd -and -not $toRemove) { Write-OK "Members already in sync" }

    # ── Update transport rule ─────────────────────────────────────────────────
    Write-Step "Updating transport rule '$ruleName'..."
    $rule = Get-TransportRule -Identity $ruleName -ErrorAction SilentlyContinue
    if ($rule) {
        if ($MemberList.Count -gt 0) {
            Set-TransportRule -Identity $ruleName -FromMemberOf @($dgName) -Comments $managedBy | Out-Null
            Write-OK "Transport rule updated: FromMemberOf=$dgName"
        } else {
            # No members — remove DG condition so nothing is routed (implicit)
            Set-TransportRule -Identity $ruleName -FromMemberOf $null -Comments $managedBy | Out-Null
            Write-OK "Transport rule updated: FromMemberOf cleared (no active mailboxes)"
        }
    } else {
        Write-Warn "Transport rule '$ruleName' not found — skipping rule update"
    }

    Write-OK "Done. Active mailboxes: $($MemberList -join ', ')"

} finally {
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
}
