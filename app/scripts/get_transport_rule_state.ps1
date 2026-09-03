# get_transport_rule_state.ps1
# READ-ONLY: reports whether a transport rule is Enabled/Disabled.
# Used by the bypass watchdog banner (independent of the external watchdog).
# Parameters are passed via -File (no string interpolation). Output: one JSON line.
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
    $r = Get-TransportRule -Identity $RuleName -ErrorAction Stop
    Write-Output (@{ ok = $true; state = [string]$r.State } | ConvertTo-Json -Compress)
} catch {
    Write-Output (@{ ok = $false; error = "rule: $($_.Exception.Message)" } | ConvertTo-Json -Compress)
} finally {
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
}
