#Requires -Modules ExchangeOnlineManagement
<#
.SYNOPSIS
    Least Privilege: pflegt eine ApplicationAccessPolicy (RestrictAccess), die den
    Graph-/IMAP-Zugriff der App auf GENAU die Gateway-Postfächer beschränkt.

    Idempotent, bei jeder Postfach-Änderung aufrufbar:
      1. Security-Gruppe (mail-enabled) sicherstellen — ApplicationAccessPolicy
         verlangt eine Security-Gruppe, keine Distribution-Gruppe.
      2. Mitglieder auf die übergebene Liste synchronisieren
         (`-BypassSecurityGroupManagerCheck`, da die App-only-Identität nicht
         Gruppen-Manager ist). Nicht existierende Adressen werden übersprungen.
      3. Policy anlegen (RestrictAccess), falls noch keine für die App existiert.

    ⚠️ ZWEI FALLEN (aus dem Live-Test):
      - NIE die Gruppe leeren, solange eine RestrictAccess-Policy greift: eine
        leere Scope-Gruppe = Deny-all = bricht ALLEN Graph-/IMAP-Zugriff. Ist die
        Ziel-Liste leer, bleiben die Mitglieder unangetastet (+ Warnung).
      - Policy NIE vor befüllter Gruppe anlegen (gleicher Grund).
#>
param(
    [Parameter(Mandatory)][string]$AppId,
    [Parameter(Mandatory)][string]$Organization,
    [Parameter(Mandatory)][string]$CertPath,
    [Parameter(Mandatory)][string]$GroupName,
    [string]$Members = "",
    [string]$PolicyDescription = "EXO Signature Gateway - Graph/IMAP nur auf Gateway-Postfaecher"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Step([string]$m) { Write-Host "[AAP] $m"  -ForegroundColor Cyan }
function Write-OK([string]$m)   { Write-Host "[OK] $m"   -ForegroundColor Green }
function Write-Warn([string]$m) { Write-Host "[WARN] $m" -ForegroundColor Yellow }

# @() an der Zuweisung erzwingen (Ein-Element-Liste sonst Skalar → .Count wirft).
$desired = @(if ($Members) { $Members -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" } } else { @() })

$cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
    $CertPath, [string]$null,
    ([System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet))
Connect-ExchangeOnline -AppId $AppId -Certificate $cert -Organization $Organization `
    -ShowBanner:$false -ShowProgress:$false | Out-Null

try {
    # ── 1. Security-Gruppe sicherstellen ──────────────────────────────────────
    $grp = Get-DistributionGroup -Identity $GroupName -ErrorAction SilentlyContinue
    if (-not $grp) {
        Write-Step "Creating mail-enabled security group '$GroupName'..."
        New-DistributionGroup -Name $GroupName -Type Security `
            -MemberJoinRestriction Closed -MemberDepartRestriction Closed | Out-Null
        Start-Sleep -Seconds 6
        Write-OK "Security-Gruppe angelegt"
    }

    # ── 2. Mitglieder synchronisieren ─────────────────────────────────────────
    $current = @(Get-DistributionGroupMember -Identity $GroupName -ResultSize Unlimited |
        ForEach-Object { $_.PrimarySmtpAddress.ToLower() })
    if ($desired.Count -eq 0) {
        Write-Warn "Keine Ziel-Postfaecher — Mitglieder werden NICHT geleert (RestrictAccess wuerde sonst ALLES sperren)."
    } else {
        $want = @($desired | ForEach-Object { $_.ToLower() })
        foreach ($m in ($want | Where-Object { $_ -notin $current })) {
            try { Add-DistributionGroupMember -Identity $GroupName -Member $m -BypassSecurityGroupManagerCheck -ErrorAction Stop; Write-OK "  + $m" }
            catch { if ($_.Exception.Message -like "*already*"){Write-OK "  = $m"} else {Write-Warn "  uebersprungen $m : $($_.Exception.Message)"} }
        }
        foreach ($m in ($current | Where-Object { $_ -notin $want })) {
            try { Remove-DistributionGroupMember -Identity $GroupName -Member $m -BypassSecurityGroupManagerCheck -Confirm:$false -ErrorAction Stop; Write-OK "  - $m" }
            catch { Write-Warn "  entfernen fehlgeschlagen $m : $($_.Exception.Message)" }
        }
    }

    # ── 3. Policy sicherstellen (nur bei befuellter Gruppe) ────────────────────
    $members_now = @(Get-DistributionGroupMember -Identity $GroupName -ResultSize Unlimited)
    $pol = Get-ApplicationAccessPolicy -ErrorAction SilentlyContinue | Where-Object { $_.AppId -eq $AppId }
    if ($members_now.Count -eq 0) {
        Write-Warn "Gruppe ist leer — Policy wird NICHT angelegt (Deny-all vermeiden)."
    } elseif (-not $pol) {
        New-ApplicationAccessPolicy -AppId $AppId -PolicyScopeGroupId $GroupName `
            -AccessRight RestrictAccess -Description $PolicyDescription | Out-Null
        Write-OK "ApplicationAccessPolicy angelegt (RestrictAccess)"
    } else {
        Write-OK "Policy vorhanden; Mitglieder synchronisiert ($($members_now.Count))"
    }
} finally {
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
}
