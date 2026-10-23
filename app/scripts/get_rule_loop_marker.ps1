# get_rule_loop_marker.ps1
# READ-ONLY: Loop-Ausnahme (Header + Muster) der Gateway-Transportregeln lesen.
# RuleName = kommagetrennte Liste (regel_kennung.regelnamen()); fehlende Regeln
# werden mit found=$false gemeldet. Ausgabe: eine JSON-Zeile.
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
    $regeln = @()
    foreach ($name in ($RuleName -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })) {
        $r = Get-TransportRule -Identity $name -ErrorAction SilentlyContinue
        if (-not $r) { $regeln += @{ name = $name; found = $false }; continue }
        $regeln += @{ name = $name; found = $true
                      header = [string]$r.ExceptIfHeaderMatchesMessageHeader
                      patterns = @($r.ExceptIfHeaderMatchesPatterns | ForEach-Object { [string]$_ }) }
    }
    Write-Output (@{ ok = $true; regeln = $regeln } | ConvertTo-Json -Compress -Depth 4)
} catch {
    Write-Output (@{ ok = $false; error = "rule: $($_.Exception.Message)" } | ConvertTo-Json -Compress)
} finally {
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
}
