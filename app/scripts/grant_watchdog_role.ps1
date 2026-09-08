# grant_watchdog_role.ps1
# Erteilt der Wächter-Identität (Managed Identity ODER Zertifikats-App) die EXO-RBAC
# SCHREIB-Rolle "Transport Rules" — nichts weiter. Läuft im Gateway-Container
# (Phase-2-Wizard, /api/watchdog/grant-role), verbindet sich mit dem Gateway-Auth-
# Zertifikat (Exchange.ManageAsApp).
#
# ⚠️ Das ist NUR der EXO-Schreibteil. Damit die MI sich ueberhaupt anmelden kann,
# braucht sie ZUSAETZLICH (Graph, Directory-Admin — NICHT hier):
#   - App-Rolle Exchange.ManageAsApp, UND
#   - Entra-Rolle "Global Reader" (kleinste unterstuetzte Anmelde-Rolle).
# Siehe azure/watchdog/README.md. KEIN "Exchange Administrator" noetig.
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
    # ── EXO-Service-Principal fuer die Waechter-Identitaet sicherstellen ──
    # Kein Zugriff auf $sp.ServiceId fuer die Ausgabe: die zurueckgegebenen
    # ServicePrincipal-Objekte fuehren diese Eigenschaft nicht, und unter
    # Set-StrictMode bricht ein solcher Zugriff das Skript ab, BEVOR die Rolle
    # zugewiesen ist. Die IDs kennen wir ohnehin aus den Parametern.
    $sp = Get-ServicePrincipal -ErrorAction SilentlyContinue | Where-Object { $_.AppId -eq $WatchdogAppId }
    if (-not $sp) {
        New-ServicePrincipal -AppId $WatchdogAppId -ObjectId $WatchdogObjectId -DisplayName $WatchdogName | Out-Null
        Write-Output ("[OK] EXO-Service-Principal angelegt (AppId " + $WatchdogAppId + ").")
    } else {
        Write-Output ("[OK] EXO-Service-Principal vorhanden (AppId " + $WatchdogAppId + ").")
    }

    # ── Rolle "Transport Rules" zuweisen (idempotent) ──
    $roleName = "Transport Rules"
    $existing = Get-ManagementRoleAssignment -RoleAssignee $WatchdogObjectId -ErrorAction SilentlyContinue |
        Where-Object { $_.Role -eq $roleName }
    if (-not $existing) {
        New-ManagementRoleAssignment -App $WatchdogObjectId -Role $roleName `
            -Name ("Watchdog-TransportRules-" + $WatchdogObjectId) | Out-Null
        Write-Output "[OK] Rolle 'Transport Rules' zugewiesen — der Waechter darf NUR Transportregeln schalten."
    } else {
        Write-Output "[OK] Rolle 'Transport Rules' bereits zugewiesen."
    }
    Write-Output "GRANT-ROLE-OK"
} finally {
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
}
