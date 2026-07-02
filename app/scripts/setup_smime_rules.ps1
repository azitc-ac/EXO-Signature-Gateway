#Requires -Modules ExchangeOnlineManagement
<#
.SYNOPSIS
    Creates two EXO Transport Rules for S/MIME inbound mail processing
    (signed mail signature-stripping and encrypted mail decryption).

.PARAMETER AppId
    Application (client) ID of the EXO Signature Gateway app registration.

.PARAMETER Organization
    Initial tenant domain, e.g. "contoso.onmicrosoft.com".

.PARAMETER CertPath
    Path to the PFX file (no password) containing the auth certificate.

.PARAMETER ConnectorName
    Name of the existing EXO Outbound Connector to route through.
    Default: "EXO Signature Gateway - Outbound"
#>

param(
    [Parameter(Mandatory)][string]$AppId,
    [Parameter(Mandatory)][string]$Organization,
    [Parameter(Mandatory)][string]$CertPath,
    [string]$GatewayName = "EXO Signature Gateway",
    [string]$ConnectorName = "EXO Signature Gateway - Outbound"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Step([string]$msg) { Write-Host "[EXO-SMIME] $msg" -ForegroundColor Cyan }
function Write-OK([string]$msg)   { Write-Host "[OK] $msg"         -ForegroundColor Green }
function Write-Warn([string]$msg) { Write-Host "[WARN] $msg"       -ForegroundColor Yellow }

$managedBy = "##Managed by $GatewayName, last update: $(Get-Date -Format 'yyyy-MM-dd HH:mm')##"

# ── Load certificate ──────────────────────────────────────────────────────────
Write-Step "Loading certificate from $CertPath"
$cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
    $CertPath,
    [string]$null,
    ([System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet -bor
     [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::Exportable)
)
if (-not $cert.HasPrivateKey) {
    throw "PFX does not contain a private key — re-run Step 3 (Azure login)."
}
Write-OK "Certificate loaded: $($cert.Subject)"

# ── Connect ───────────────────────────────────────────────────────────────────
Write-Step "Connecting to Exchange Online org=$Organization"
Connect-ExchangeOnline `
    -AppId $AppId `
    -Certificate $cert `
    -Organization $Organization `
    -ShowBanner:$false `
    -ShowProgress:$false
Write-OK "Connected"

# ── Resolve connector identity ────────────────────────────────────────────────
Write-Step "Resolving outbound connector '$ConnectorName'..."
$connector = $null
for ($i = 0; $i -lt 6; $i++) {
    $connector = Get-OutboundConnector -Identity $ConnectorName -ErrorAction SilentlyContinue
    if ($connector) { break }
    Write-Step "Waiting for connector… ($($i+1)/6)"
    Start-Sleep -Seconds 10
}
if (-not $connector) {
    throw "Outbound Connector '$ConnectorName' not found. Run Step 7 (EXO Connector) first."
}
$connectorId = $connector.Identity
Write-OK "Connector resolved: $connectorId"

# ── Transport Rule: signed inbound ────────────────────────────────────────────
Write-Step "Checking Transport Rule: signed inbound..."
$signedRuleName = "$GatewayName - SMIME Signed Inbound"
$existingSigned = Get-TransportRule -Identity $signedRuleName -ErrorAction SilentlyContinue

if ($existingSigned) {
    Write-Warn "Rule '$signedRuleName' already exists — updating comment"
    Set-TransportRule -Identity $signedRuleName -Comments $managedBy | Out-Null
    Write-OK "Rule comment updated: $signedRuleName"
} else {
    New-TransportRule `
        -Name $signedRuleName `
        -FromScope NotInOrganization `
        -MessageTypeMatches Signed `
        -ExceptIfHeaderMatchesMessageHeader "X-Sig-Applied" `
        -ExceptIfHeaderMatchesPatterns "1" `
        -RouteMessageOutboundConnector $connectorId `
        -StopRuleProcessing $true `
        -Comments $managedBy `
        -Mode Enforce | Out-Null
    Write-OK "Rule created: signed inbound → proxy (signature strip + cert harvest)"
}

# ── Transport Rule: encrypted inbound ─────────────────────────────────────────
Write-Step "Checking Transport Rule: encrypted inbound..."
$encRuleName = "$GatewayName - SMIME Encrypted Inbound"
$existingEnc = Get-TransportRule -Identity $encRuleName -ErrorAction SilentlyContinue

if ($existingEnc) {
    Write-Warn "Rule '$encRuleName' already exists — updating comment"
    Set-TransportRule -Identity $encRuleName -Comments $managedBy | Out-Null
    Write-OK "Rule comment updated: $encRuleName"
} else {
    New-TransportRule `
        -Name $encRuleName `
        -FromScope NotInOrganization `
        -MessageTypeMatches Encrypted `
        -ExceptIfHeaderMatchesMessageHeader "X-Sig-Applied" `
        -ExceptIfHeaderMatchesPatterns "1" `
        -RouteMessageOutboundConnector $connectorId `
        -StopRuleProcessing $true `
        -Comments $managedBy `
        -Mode Enforce | Out-Null
    Write-OK "Rule created: encrypted inbound → proxy (decrypt)"
}

# ── Done ──────────────────────────────────────────────────────────────────────
Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue

Write-OK "S/MIME transport rules setup complete."
Write-Host ""
Write-Host "Rules created:"
Write-Host "  [$signedRuleName]"
Write-Host "    FromScope=NotInOrganization + MessageTypeMatches=Signed → Proxy"
Write-Host "    Proxy: strips signature, harvests public key, adds subject tag"
Write-Host ""
Write-Host "  [$encRuleName]"
Write-Host "    FromScope=NotInOrganization + MessageTypeMatches=Encrypted → Proxy"
Write-Host "    Proxy: decrypts with recipient's private key, adds subject tag"
Write-Host ""
Write-Host "Prerequisite: recipient private keys must be imported under S/MIME → Signing Certs."
