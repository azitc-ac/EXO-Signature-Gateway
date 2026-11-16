# set_rule_auto_ausnahme.ps1
# Ergänzt an den Gateway-Routing-Regeln NUR die Auto-Antwort-Ausnahme
# (ExceptIfHeaderContainsMessageHeader 'Auto-Submitted' + Wort 'auto').
# Gate, Connector, übrige Bedingungen und Ausnahmen bleiben unberührt.
# Anlass: Die Ausnahme kam mit v1.9.88 (01.10.2026) nur an neu angelegte Regeln;
# ältere Regeln zogen Abwesenheitsnotizen (MAIL FROM:<>) ins Gateway, wo der
# Rückweg an externe Empfänger scheitert (regel_auto_ausnahme.py).
# RuleName = kommagetrennte Liste; fehlende Regeln werden übersprungen.
# Ausgabe: eine JSON-Zeile {ok, regeln:[{name, found, vorher, nachher}]}.
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
    $alleOk = $true
    foreach ($name in ($RuleName -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })) {
        $r = Get-TransportRule -Identity $name -ErrorAction SilentlyContinue
        if (-not $r) { $regeln += @{ name = $name; found = $false }; continue }
        $vorher = [string]$r.ExceptIfHeaderContainsMessageHeader + ':' + (@($r.ExceptIfHeaderContainsWords | ForEach-Object { [string]$_ }) -join ',')
        Set-TransportRule -Identity $name -ExceptIfHeaderContainsMessageHeader "Auto-Submitted" `
            -ExceptIfHeaderContainsWords "auto" -ErrorAction Stop | Out-Null
        $n = Get-TransportRule -Identity $name -ErrorAction Stop
        $worte = @($n.ExceptIfHeaderContainsWords | ForEach-Object { [string]$_ })
        $nachher = [string]$n.ExceptIfHeaderContainsMessageHeader + ':' + ($worte -join ',')
        if ([string]$n.ExceptIfHeaderContainsMessageHeader -ne "Auto-Submitted" -or ($worte -notcontains "auto")) { $alleOk = $false }
        $regeln += @{ name = $name; found = $true; vorher = $vorher; nachher = $nachher }
    }
    Write-Output (@{ ok = $alleOk; regeln = $regeln } | ConvertTo-Json -Compress -Depth 4)
} catch {
    Write-Output (@{ ok = $false; error = "rule: $($_.Exception.Message)" } | ConvertTo-Json -Compress)
} finally {
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
}
