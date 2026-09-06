#Requires -Modules ExchangeOnlineManagement
<#
.SYNOPSIS
    Least Privilege: beschränkt den Graph-/IMAP-Zugriff der App auf GENAU die
    Gateway-Postfächer — über BEIDE Modelle, die Exchange Online parallel fährt:
    die klassische ApplicationAccessPolicy (RestrictAccess) UND die neue
    RBAC-for-Applications-Zuweisung.

    Idempotent, bei jeder Postfach-Änderung aufrufbar:
      1. Security-Gruppe (mail-enabled) sicherstellen — ApplicationAccessPolicy
         verlangt eine Security-Gruppe, keine Distribution-Gruppe.
      2. Mitglieder auf die übergebene Liste synchronisieren
         (`-BypassSecurityGroupManagerCheck`, da die App-only-Identität nicht
         Gruppen-Manager ist). Nicht existierende Adressen werden übersprungen.
      3. Policy anlegen (RestrictAccess), falls noch keine für die App existiert.
      4. RBAC for Applications: Management-Scope auf DIESELBE Gruppe + die Rollen
         Application Mail.Send/Mail.ReadWrite der App zuweisen. Postfächer mit
         Copilot/Defender ignorieren die klassische Policy und verlangen RBAC;
         ohne diese Zuweisung antwortet Graph mit "[RAOP] AppOnly AccessPolicy"
         (403) und der Reinject scheitert. Best-effort: fehlt RBAC im Tenant,
         bleibt die Policy aus Schritt 3 wirksam.

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

    # ── 4. RBAC for Applications (Copilot-/Defender-Postfaecher verlangen es) ───
    # Fuer Premium-Postfaecher greift die klassische Policy aus Schritt 3 NICHT
    # mehr; Exchange verlangt dort eine RBAC-Rollenzuweisung. Analog gescoped auf
    # DIESELBE Gruppe (Least Privilege). Best-effort + idempotent — scheitert es,
    # bleibt die klassische Policy wirksam, das Speichern darf nicht kippen.
    if ($members_now.Count -eq 0) {
        Write-Warn "Gruppe leer — RBAC-Zuweisung wird NICHT angelegt."
    } else {
        try {
            $sp = Get-ServicePrincipal -ErrorAction SilentlyContinue | Where-Object { $_.AppId -eq $AppId } | Select-Object -First 1
            if (-not $sp) {
                Write-Warn "Kein EXO-ServicePrincipal fuer $AppId — RBAC uebersprungen."
            } else {
                $grpDn = (Get-DistributionGroup -Identity $GroupName).DistinguishedName
                $scopeName = "$GroupName - RBAC Scope"
                if (-not (Get-ManagementScope -Identity $scopeName -ErrorAction SilentlyContinue)) {
                    New-ManagementScope -Name $scopeName -RecipientRestrictionFilter "MemberOfGroup -eq '$grpDn'" | Out-Null
                    Write-OK "RBAC Management-Scope angelegt ($scopeName)"
                }
                foreach ($role in @("Application Mail.Send", "Application Mail.ReadWrite")) {
                    $have = Get-ManagementRoleAssignment -RoleAssignee $sp.ObjectId -ErrorAction SilentlyContinue | Where-Object { $_.Role -eq $role }
                    if ($have) { Write-OK "RBAC-Zuweisung vorhanden: $role"; continue }
                    New-ManagementRoleAssignment -App $sp.ObjectId -Role $role -CustomResourceScope $scopeName | Out-Null
                    Write-OK "RBAC-Zuweisung angelegt: $role"
                }
            }
        } catch {
            Write-Warn "RBAC-for-Apps uebersprungen: $($_.Exception.Message)"
        }
    }
} finally {
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
}
