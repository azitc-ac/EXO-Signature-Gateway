# remove_rule_recipient_scope.ps1
# Entfernt an der Signatur-Transportregel NUR die Empfängerbedingung
# (SentToScope, z. B. NotInOrganization aus Installationen vor v1.8.64).
#
# Warum gezielt und nicht über setup_exo_connector.ps1: Dessen Aktualisieren-
# Zweig heilt SentToScope zwar auch, setzt aber zugleich Connectoren neu (u. a.
# TlsDomain des Outbound-Connectors) — hinter einem Proxy mit anderem
# Zertifikat bricht das die Zustellung. Hier wird NICHTS ausser SentToScope
# angefasst.
#
# Sicherung: Ohne FromMemberOf-Gate wird nichts geändert. Eine Regel ohne Gate
# matcht jeden internen Absender; ihr auch noch die Empfängerbedingung zu
# nehmen, schickte ALLE Post des Tenants durchs Gateway (Ausfall 04.09.).
#
# Parameter via -File (keine String-Interpolation). Ausgabe: eine JSON-Zeile
# {ok, geaendert, vorher, nachher} oder {ok:false, error}.
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
    $vorher = [string]$r.SentToScope
    if (-not $vorher) {
        Write-Output (@{ ok = $true; geaendert = $false; vorher = ""; nachher = "" } | ConvertTo-Json -Compress)
    } elseif (-not $r.FromMemberOf -or @($r.FromMemberOf).Count -eq 0) {
        Write-Output (@{ ok = $false; vorher = $vorher;
            error = "Die Regel hat keine Mitgliedschaftsbedingung (FromMemberOf). Ohne sie wird die Empfängerbedingung nicht entfernt — erst 'Connector neu einrichten' oder die Postfächer speichern, damit das Gate wieder steht." } | ConvertTo-Json -Compress)
    } else {
        Set-TransportRule -Identity $RuleName -SentToScope $null -ErrorAction Stop | Out-Null
        $n = Get-TransportRule -Identity $RuleName -ErrorAction Stop
        $nachher = [string]$n.SentToScope
        Write-Output (@{ ok = (-not $nachher); geaendert = $true; vorher = $vorher; nachher = $nachher } | ConvertTo-Json -Compress)
    }
} catch {
    Write-Output (@{ ok = $false; error = "rule: $($_.Exception.Message)" } | ConvertTo-Json -Compress)
} finally {
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
}
