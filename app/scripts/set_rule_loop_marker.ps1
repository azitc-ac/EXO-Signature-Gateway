# set_rule_loop_marker.ps1
# Gleicht an den Gateway-Transportregeln NUR die Loop-Ausnahme an
# (ExceptIfHeaderMatchesMessageHeader + ExceptIfHeaderMatchesPatterns).
# Gate, Connector, übrige Bedingungen und Ausnahmen bleiben unberührt.
# Anlass: Bis v1.10.43 war die Markierung überall `1`; Post eines anderen
# Gateways nahm die eigenen S/MIME-Regeln dadurch aus (regel_kennung.py).
# RuleName = kommagetrennte Liste; fehlende Regeln werden übersprungen.
# Ausgabe: eine JSON-Zeile {ok, regeln:[{name, found, vorher, nachher}]}.
param(
    [string]$AppId,
    [string]$CertPath,
    [string]$Organization,
    [string]$RuleName,
    [string]$LoopHeader = "X-Sig-Applied",
    [string]$LoopPattern
)

$ErrorActionPreference = 'Stop'

if (-not $LoopPattern) {
    Write-Output (@{ ok = $false; error = "LoopPattern fehlt" } | ConvertTo-Json -Compress)
    exit 0
}

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
    $alleOk = $true
    foreach ($name in ($RuleName -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })) {
        $r = Get-TransportRule -Identity $name -ErrorAction SilentlyContinue
        if (-not $r) { $regeln += @{ name = $name; found = $false }; continue }
        $vorher = @($r.ExceptIfHeaderMatchesPatterns | ForEach-Object { [string]$_ }) -join ','
        Set-TransportRule -Identity $name -ExceptIfHeaderMatchesMessageHeader $LoopHeader `
            -ExceptIfHeaderMatchesPatterns $LoopPattern -ErrorAction Stop | Out-Null
        $n = Get-TransportRule -Identity $name -ErrorAction Stop
        $nachher = @($n.ExceptIfHeaderMatchesPatterns | ForEach-Object { [string]$_ }) -join ','
        if ($nachher -ne $LoopPattern -or [string]$n.ExceptIfHeaderMatchesMessageHeader -ne $LoopHeader) { $alleOk = $false }
        $regeln += @{ name = $name; found = $true; vorher = $vorher; nachher = $nachher }
    }
    Write-Output (@{ ok = $alleOk; regeln = $regeln } | ConvertTo-Json -Compress -Depth 4)
} catch {
    Write-Output (@{ ok = $false; error = "rule: $($_.Exception.Message)" } | ConvertTo-Json -Compress)
} finally {
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
}
