# get_addin_rollen.ps1
# READ-ONLY: Rollen der Standard-Rollenzuweisungsrichtlinie und AppsForOfficeEnabled lesen.
# Ohne die Benutzerrollen für Add-ins laufen Outlook-Add-ins nur bei Admins
# (addin_rollen.py). RuleName wird von waechter_regel.ps_regelskript immer
# übergeben und hier nicht gebraucht. Ausgabe: eine JSON-Zeile.
param(
    [string]$AppId,
    [string]$CertPath,
    [string]$Organization,
    [string]$RuleName
)

$ErrorActionPreference = 'Stop'

try {
    $cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
        $CertPath, [string]$null,
        [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet)
    Connect-ExchangeOnline -AppId $AppId -Certificate $cert -Organization $Organization `
        -ShowBanner:$false -ShowProgress:$false -ErrorAction Stop
} catch {
    Write-Output (@{ ok = $false; error = "connect: $($_.Exception.Message)" } | ConvertTo-Json -Compress)
    exit 0
}

try {
    $p = Get-RoleAssignmentPolicy | Where-Object { $_.IsDefault } | Select-Object -First 1
    $erg = @{ ok = $true
              apps_for_office = [bool](Get-OrganizationConfig).AppsForOfficeEnabled
              richtlinie = [string]$p.Name
              rollen = @($p.AssignedRoles | ForEach-Object { [string]$_ }) }
    Write-Output ($erg | ConvertTo-Json -Compress -Depth 3)
} catch {
    Write-Output (@{ ok = $false; error = "read: $($_.Exception.Message)" } | ConvertTo-Json -Compress)
} finally {
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
}
