#Requires -Modules ExchangeOnlineManagement
<#
.SYNOPSIS
    Erteilt der Wächter-Identität (Managed Identity der Azure Function) die
    MINIMALE EXO-Rolle, um Transportregeln zu schalten — nichts weiter.

    Der Wächter braucht nur `Enable-/Disable-TransportRule`. Die eingebaute
    Management-Rolle "Transport Rules" ist der kleinste vorgefertigte Umfang dafür
    (Transportregel-Cmdlets, kein Postfach-/Mail-Zugriff, keine Admin-Rechte).

    ⚠️ Das ist NUR die EXO-RBAC-Berechtigung. Damit die Managed Identity sich
    ueberhaupt an EXO anmelden kann, braucht sie ZUSAETZLICH die App-Rolle
    `Exchange.ManageAsApp` (Graph, siehe README Schritt 1) — die erteilt dieses
    Skript NICHT (anderer Auth-Weg: Graph statt EXO-Cmdlets). Ohne sie: UnAuthorized.

    Laeuft einmalig bei der Einrichtung, verbindet sich mit dem bestehenden
    Gateway-/Admin-App-Zertifikat (Exchange.ManageAsApp).

.PARAMETER WatchdogAppId
    App-/Client-ID der Managed Identity der Function.
.PARAMETER WatchdogObjectId
    Objekt-ID (Enterprise-App / Service Principal) der Managed Identity.
.PARAMETER WatchdogName
    Anzeigename für den EXO-Service-Principal (Vorgabe "EXO Signature Gateway Watchdog").
#>
param(
    [Parameter(Mandatory)][string]$AppId,
    [Parameter(Mandatory)][string]$Organization,
    [Parameter(Mandatory)][string]$CertPath,
    [Parameter(Mandatory)][string]$WatchdogAppId,
    [Parameter(Mandatory)][string]$WatchdogObjectId,
    [string]$WatchdogName = "EXO Signature Gateway Watchdog"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
    $CertPath, [string]$null,
    ([System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet))
Connect-ExchangeOnline -AppId $AppId -Certificate $cert -Organization $Organization `
    -ShowBanner:$false -ShowProgress:$false | Out-Null

try {
    # ── EXO-Service-Principal für die Managed Identity sicherstellen ──
    $sp = Get-ServicePrincipal -ErrorAction SilentlyContinue | Where-Object { $_.AppId -eq $WatchdogAppId }
    if (-not $sp) {
        $sp = New-ServicePrincipal -AppId $WatchdogAppId -ObjectId $WatchdogObjectId -DisplayName $WatchdogName
        Write-Host "[OK] EXO-Service-Principal fuer den Waechter angelegt: $($sp.ObjectId)"
    } else {
        Write-Host "[OK] EXO-Service-Principal vorhanden: $($sp.ObjectId)"
    }

    # ── Minimale Rolle "Transport Rules" zuweisen (idempotent) ──
    $roleName = "Transport Rules"
    $existing = Get-ManagementRoleAssignment -RoleAssignee $sp.ObjectId -ErrorAction SilentlyContinue |
        Where-Object { $_.Role -eq $roleName }
    if (-not $existing) {
        New-ManagementRoleAssignment -App $sp.ObjectId -Role $roleName `
            -Name "Watchdog-TransportRules-$($sp.ObjectId)" | Out-Null
        Write-Host "[OK] Rolle '$roleName' zugewiesen — der Waechter darf NUR Transportregeln schalten."
    } else {
        Write-Host "[OK] Rolle '$roleName' bereits zugewiesen."
    }
} finally {
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
}
