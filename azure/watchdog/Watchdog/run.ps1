# Bypass-Wächter (Azure Function, Timer-getriggert).
#
# Prüft die Gateway-/health und schaltet bei Ausfall die SIGNATUR-Transportregel
# ab (ausgehende Signatur-Post fließt dann unsigniert weiter statt in Exchange zu
# stauen); bei Erholung wieder ein. Die S/MIME-Regel bleibt IMMER unangetastet —
# verschlüsselungsfähige Post darf nie unverschlüsselt hinaus.
#
# Läuft EXTERN (Azure), überlebt also den Ausfall des Gateway-Hosts. Authentifiziert
# sich an Exchange per MANAGED IDENTITY (kein Zertifikat zu verwalten); die Identität
# hat eine minimale EXO-Rolle "Transport Rules" (siehe setup_watchdog_role.ps1).
#
# App-Einstellungen (Function App → Konfiguration):
#   GATEWAY_HEALTH_URL   z.B. https://sig.zarenko.net/health
#   WATCHDOG_TOKEN       Klartext-Token; Gateway prüft dessen PBKDF2-Hash
#   EXO_ORGANIZATION     z.B. zarenko.onmicrosoft.com
#   SIG_RULE_NAME        z.B. "Route via EXO Signature Gateway"
#   FAIL_THRESHOLD       aufeinanderfolgende Fehlversuche bis Bypass (Vorgabe 3)
param($Timer)

$ErrorActionPreference = 'Stop'

$healthUrl = $env:GATEWAY_HEALTH_URL
$token     = $env:WATCHDOG_TOKEN
$org       = $env:EXO_ORGANIZATION
$ruleName  = $env:SIG_RULE_NAME
$threshold = [int]($env:FAIL_THRESHOLD); if ($threshold -lt 1) { $threshold = 3 }

if (-not $healthUrl -or -not $org -or -not $ruleName) {
    throw "Fehlende App-Einstellungen (GATEWAY_HEALTH_URL / EXO_ORGANIZATION / SIG_RULE_NAME)."
}

# ── Gesundheit prüfen (debounced: erst nach mehreren Fehlversuchen als DOWN werten) ──
function Test-GatewayHealthy {
    $headers = @{}
    if ($token) { $headers["X-Watchdog-Token"] = $token }
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

# ── Heartbeat: dem Gateway melden, dass der Wächter lebt + wie es steht ──
# Best-effort: ein Heartbeat-Fehler darf den Lauf nie scheitern lassen. Die URL
# leitet sich aus der Health-URL ab (…/health → …/api/watchdog/heartbeat).
function Send-Heartbeat([bool]$healthy, [bool]$bypassActive) {
    if (-not $token) { return }
    try {
        $base = $healthUrl -replace '/health/?$', ''
        $body = @{ healthy = $healthy; bypass_active = $bypassActive } | ConvertTo-Json -Compress
        Invoke-RestMethod -Uri "$base/api/watchdog/heartbeat" -Method Post `
            -Headers @{ "X-Watchdog-Token" = $token } -ContentType "application/json" `
            -Body $body -TimeoutSec 10 -SkipCertificateCheck | Out-Null
    } catch {
        Write-Host "Heartbeat fehlgeschlagen (unkritisch): $($_.Exception.Message)"
    }
}

$healthy = Test-GatewayHealthy
Write-Host "Gateway healthy = $healthy"

# ── Regel nach Bedarf schalten (idempotent; NUR die Signatur-Regel) ──
$bypassActive = $false
Import-Module ExchangeOnlineManagement
Connect-ExchangeOnline -ManagedIdentity -Organization $org -ShowBanner:$false | Out-Null
try {
    $rule = Get-TransportRule -Identity $ruleName -ErrorAction SilentlyContinue
    if (-not $rule) {
        Write-Host "Regel '$ruleName' nicht gefunden — nichts zu tun."
    } elseif (-not $healthy -and $rule.State -eq "Enabled") {
        Disable-TransportRule -Identity $ruleName -Confirm:$false
        $bypassActive = $true
        Write-Host "BYPASS AKTIV: Gateway ausgefallen → Signatur-Regel '$ruleName' DEAKTIVIERT."
    } elseif ($healthy -and $rule.State -eq "Disabled") {
        Enable-TransportRule -Identity $ruleName -Confirm:$false
        Write-Host "ERHOLT: Gateway wieder da → Signatur-Regel '$ruleName' AKTIVIERT."
    } else {
        $bypassActive = ($rule.State -eq "Disabled")
        Write-Host "Kein Wechsel nötig (healthy=$healthy, State=$($rule.State))."
    }
} finally {
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
}

Send-Heartbeat $healthy $bypassActive
