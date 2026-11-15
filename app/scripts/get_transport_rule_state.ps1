# get_transport_rule_state.ps1
# READ-ONLY: reports State, SentToScope and FromMemberOf of a transport rule.
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
    # SentToScope: leer = keine Empfängerbedingung (Soll). Alt-Installationen
    # tragen mitunter noch NotInOrganization — dann erreicht interne Post das
    # Gateway nie (regel_bedingung.py).
    # FromMemberOf: das Absender-Gate. Leer bei aktiver Regel = jeder interne
    # Absender läuft durchs Gateway (Vorfall 04.09., regel_soll.gate_offen).
    # @(...) hält auch ein einzelnes Element als Liste.
    $gate = @($r.FromMemberOf | ForEach-Object { [string]$_ })
    Write-Output (@{ ok = $true; state = [string]$r.State; sent_to_scope = [string]$r.SentToScope; from_member_of = $gate } | ConvertTo-Json -Compress)
} catch {
    Write-Output (@{ ok = $false; error = "rule: $($_.Exception.Message)" } | ConvertTo-Json -Compress)
} finally {
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
}
