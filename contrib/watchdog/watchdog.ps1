# Bypass-Wächter (cron-Variante, für einen eigenen Zweithost).
#
# Gleiche Aufgabe wie die Azure-Function (azure/watchdog/): prüft die Gateway-
# /health und schaltet bei Ausfall die SIGNATUR-Transportregel ab, bei Erholung
# wieder ein. Die S/MIME-Regel bleibt IMMER an.
#
# ⚠️ Muss auf einem ANDEREN Host als das Gateway laufen (sonst stirbt der Wächter
# mit dem Gateway). Anders als in Azure gibt es hier keine Managed Identity → die
# Anmeldung erfolgt per ZERTIFIKAT der Wächter-App (WATCHDOG_CERT_PATH). Dieses
# Zertifikat ist zu schützen (600) — der Preis dafür, ohne Azure auszukommen.
#
# Konfiguration per Umgebung (siehe watchdog.env.example):
#   GATEWAY_HEALTH_URL, WATCHDOG_TOKEN, EXO_ORGANIZATION, WATCHDOG_APP_ID,
#   WATCHDOG_CERT_PATH, SIG_RULE_NAME, FAIL_THRESHOLD
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$healthUrl = $env:GATEWAY_HEALTH_URL
$token     = $env:WATCHDOG_TOKEN
$watcherId = $env:WATCHDOG_ID        # ordnet den Heartbeat im Mehrwächter-Register zu
$org       = $env:EXO_ORGANIZATION
$appId     = $env:WATCHDOG_APP_ID
$certPath  = $env:WATCHDOG_CERT_PATH
$ruleName  = $env:SIG_RULE_NAME
$threshold = [int]($env:FAIL_THRESHOLD); if ($threshold -lt 1) { $threshold = 3 }

foreach ($v in @("GATEWAY_HEALTH_URL","EXO_ORGANIZATION","WATCHDOG_APP_ID","WATCHDOG_CERT_PATH","SIG_RULE_NAME")) {
    if (-not (Get-Item "env:$v" -ErrorAction SilentlyContinue).Value) { throw "Fehlende Umgebung: $v" }
}

function Test-GatewayHealthy {
    $headers = @{}
    if ($token) { $headers["X-Watchdog-Token"] = $token }
    if ($watcherId) { $headers["X-Watchdog-Id"] = $watcherId }
    for ($i = 0; $i -lt $threshold; $i++) {
        try {
            $r = Invoke-RestMethod -Uri $healthUrl -Headers $headers -TimeoutSec 10 -SkipCertificateCheck
            if ($r.status -eq "ok" -and $r.smtp_listener) { return $true }
        } catch {
            Write-Host "Health-Versuch $($i+1)/$threshold fehlgeschlagen: $($_.Exception.Message)"
        }
        if ($i -lt $threshold - 1) { Start-Sleep -Seconds 10 }
    }
    return $false
}

# Heartbeat: dem Gateway melden, dass der Wächter lebt + wie es steht (best-effort;
# ein Heartbeat-Fehler darf den Lauf nie scheitern lassen). URL aus der Health-URL
# abgeleitet (…/health → …/api/watchdog/heartbeat).
function Send-Heartbeat([bool]$healthy, [bool]$bypassActive, [string]$exoError = "") {
    if (-not $token) { return }
    try {
        $base = $healthUrl -replace '/health/?$', ''
        $body = @{ healthy = $healthy; bypass_active = $bypassActive; exo_error = $exoError } | ConvertTo-Json -Compress
        $hbHeaders = @{ "X-Watchdog-Token" = $token }
        if ($watcherId) { $hbHeaders["X-Watchdog-Id"] = $watcherId }
        Invoke-RestMethod -Uri "$base/api/watchdog/heartbeat" -Method Post `
            -Headers $hbHeaders -ContentType "application/json" `
            -Body $body -TimeoutSec 10 -SkipCertificateCheck | Out-Null
    } catch {
        Write-Host "Heartbeat fehlgeschlagen (unkritisch): $($_.Exception.Message)"
    }
}

$healthy = Test-GatewayHealthy
Write-Host "$(Get-Date -Format o)  Gateway healthy = $healthy"

# ⚠️ EXO-Fehler dürfen den Heartbeat NICHT verhindern (sonst „meldet sich nicht",
# obwohl der Wächter läuft). try/catch — Heartbeat läuft immer, trägt den Fehler mit.
$bypassActive = $false
$exoError = ""
try {
    $cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
        $certPath, [string]$null,
        ([System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet))
    Import-Module ExchangeOnlineManagement
    Connect-ExchangeOnline -AppId $appId -Certificate $cert -Organization $org -ShowBanner:$false | Out-Null
    try {
        $rule = Get-TransportRule -Identity $ruleName -ErrorAction SilentlyContinue
        if (-not $rule) {
            Write-Host "Regel '$ruleName' nicht gefunden — nichts zu tun."
        } elseif (-not $healthy -and $rule.State -eq "Enabled") {
            Disable-TransportRule -Identity $ruleName -Confirm:$false
            $bypassActive = $true
            Write-Host "BYPASS AKTIV: Signatur-Regel '$ruleName' DEAKTIVIERT."
        } elseif ($healthy -and $rule.State -eq "Disabled") {
            Enable-TransportRule -Identity $ruleName -Confirm:$false
            Write-Host "ERHOLT: Signatur-Regel '$ruleName' AKTIVIERT."
        } else {
            $bypassActive = ($rule.State -eq "Disabled")
            Write-Host "Kein Wechsel nötig (healthy=$healthy, State=$($rule.State))."
        }
    } finally {
        Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    }
} catch {
    $exoError = $_.Exception.Message
    Write-Host "EXO-Fehler (Heartbeat wird dennoch gesendet): $exoError"
}

Send-Heartbeat $healthy $bypassActive $exoError
