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
    [string]$Members = "",
    # Modus `imap`: App-only-IMAP (XOAUTH2) verlangt FullAccess PRO Postfach —
    # einen tenant-weiten IMAP-Grant gibt es nicht. Beim Aktivieren wird der
    # Grant deshalb gleich mitgesetzt, sonst fiele ein neues Postfach beim
    # IMAP-APPEND-Reinject still durch.
    [switch]$GrantImapFullAccess
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Python passes members as a single comma-separated string — split into array.
# NOTE: the outer @() must wrap the whole if/else, not just the branches — otherwise
# an empty result collapses to $null on assignment (PowerShell pipeline-flattening),
# and .Count throws under Set-StrictMode.
$MemberList = @(if ($Members) {
    $Members -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
} else { @() })

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
        # Das Gate ist IMMER die DG — NIE leeren. Eine Regel ohne FromMemberOf
        # (nur FromScope InOrganization) matcht JEDEN internen Absender und
        # leitet den gesamten internen Mailverkehr durchs Gateway; kann es nicht
        # zustellen (z.B. noch nicht fertig konfiguriert), staut/verwirft
        # Exchange die Post. Genau das war der frühere `-FromMemberOf $null`-Fall.
        #
        # Bei null aktiven Postfächern zeigt das Gate deshalb auf eine LEERE DG
        # (matcht niemanden) UND die Regel wird deaktiviert — doppelt
        # ausfallsicher. Aktiviert wird sie erst, wenn es aktive Postfächer gibt.
        Set-TransportRule -Identity $ruleName -FromMemberOf @($dgName) -Comments $managedBy | Out-Null
        if ($MemberList.Count -gt 0) {
            Enable-TransportRule -Identity $ruleName -Confirm:$false | Out-Null
            Write-OK "Transport rule updated: FromMemberOf=$dgName, aktiviert"
        } else {
            Disable-TransportRule -Identity $ruleName -Confirm:$false | Out-Null
            Write-OK "Transport rule updated: keine aktiven Postfaecher -> Gate auf leere DG, Regel DEAKTIVIERT"
        }
    } else {
        Write-Warn "Transport rule '$ruleName' not found — skipping rule update"
    }

    # ── IMAP FullAccess (nur Modus imap) ──────────────────────────────────────
    if ($GrantImapFullAccess -and $MemberList.Count -gt 0) {
        Write-Step "Granting IMAP FullAccess to service principal for active mailboxes..."
        $sp = Get-ServicePrincipal | Where-Object { $_.AppId -eq $AppId }
        if (-not $sp) {
            Write-Warn "Kein EXO Service Principal fuer AppId $AppId — IMAP-Zugriff erst einrichten (Schritt IMAP)."
        } else {
            foreach ($m in $MemberList) {
                try {
                    Add-MailboxPermission -Identity $m -User $sp.ObjectId `
                        -AccessRights FullAccess -AutoMapping $false -ErrorAction Stop | Out-Null
                    Write-OK "IMAP FullAccess: $m"
                } catch {
                    if ($_.Exception.Message -like '*already present*') {
                        Write-OK "IMAP FullAccess: $m (bereits vorhanden)"
                    } else {
                        Write-Host "[ERROR] IMAP FullAccess $m : $_" -ForegroundColor Red
                    }
                }
            }
        }
    }

    Write-OK "Done. Active mailboxes: $($MemberList -join ', ')"

} finally {
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
}
