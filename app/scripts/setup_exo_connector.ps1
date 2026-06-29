#Requires -Modules ExchangeOnlineManagement
<#
.SYNOPSIS
    Creates the EXO Outbound Connector, Inbound Connector and Transport Rule
    for the EXO Signature Gateway loop-back architecture.

.PARAMETER AppId
    Application (client) ID of the EXO Signature Gateway app registration.

.PARAMETER Organization
    Initial tenant domain, e.g. "contoso.onmicrosoft.com".

.PARAMETER CertPath
    Path to the PFX file (no password) containing the auth certificate and private key.

.PARAMETER SmtpProxyHostname
    Public hostname of the SMTP proxy (e.g. "mail.zarenko.net").
#>

param(
    [Parameter(Mandatory)][string]$AppId,
    [Parameter(Mandatory)][string]$Organization,
    [Parameter(Mandatory)][string]$CertPath,
    [Parameter(Mandatory)][string]$SmtpProxyHostname,
    [switch]$SkipInboundConnector
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Step([string]$msg) { Write-Host "[EXO-SETUP] $msg" -ForegroundColor Cyan }
function Write-OK([string]$msg)   { Write-Host "[OK] $msg"         -ForegroundColor Green }
function Write-Warn([string]$msg) { Write-Host "[WARN] $msg"       -ForegroundColor Yellow }

$managedBy = "##Managed by EXO Signature Gateway, last update: $(Get-Date -Format 'yyyy-MM-dd HH:mm')##"

# ── Load certificate (PFX, no password) ──────────────────────────────────────
Write-Step "Loading certificate from $CertPath"

$cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
    $CertPath,
    [string]$null,
    ([System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet -bor
     [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::Exportable)
)

Write-OK "Certificate loaded: $($cert.Subject) (HasPrivateKey=$($cert.HasPrivateKey))"

if (-not $cert.HasPrivateKey) {
    throw "PFX does not contain a private key — re-run Step 3 (Azure login) to regenerate."
}

# ── Connect to Exchange Online ────────────────────────────────────────────────
Write-Step "Connecting to Exchange Online (app-only) org=$Organization"

Connect-ExchangeOnline `
    -AppId $AppId `
    -Certificate $cert `
    -Organization $Organization `
    -ShowBanner:$false `
    -ShowProgress:$false

Write-OK "Connected to Exchange Online"

# ── Outbound Connector ────────────────────────────────────────────────────────
Write-Step "Checking Outbound Connector..."

$outName = "EXO Signature Gateway - Outbound"
$existing = Get-OutboundConnector -Identity $outName -ErrorAction SilentlyContinue

if ($existing) {
    Write-Warn "Outbound Connector '$outName' exists — updating SmartHosts"
    Set-OutboundConnector `
        -Identity $outName `
        -SmartHosts @($SmtpProxyHostname) `
        -TlsDomain $SmtpProxyHostname `
        -Enabled $true `
        -Comment $managedBy
    Write-OK "Outbound Connector updated"
} else {
    New-OutboundConnector `
        -Name $outName `
        -ConnectorType OnPremises `
        -IsTransportRuleScoped $true `
        -UseMxRecord $false `
        -SmartHosts @($SmtpProxyHostname) `
        -TlsSettings DomainValidation `
        -TlsDomain $SmtpProxyHostname `
        -Enabled $true `
        -Comment $managedBy | Out-Null
    Write-OK "Outbound Connector created → $SmtpProxyHostname"
}

# Retrieve the connector identity (GUID) to use in the Transport Rule.
# Using the GUID avoids propagation race conditions with name-based lookup.
$outConnector = $null
for ($i = 0; $i -lt 6; $i++) {
    $outConnector = Get-OutboundConnector -Identity $outName -ErrorAction SilentlyContinue
    if ($outConnector) { break }
    Write-Step "Waiting for connector to propagate… ($($i+1)/6)"
    Start-Sleep -Seconds 10
}
if (-not $outConnector) { throw "Outbound Connector '$outName' not found after waiting." }
$outConnectorId = $outConnector.Identity
Write-OK "Connector identity resolved: $outConnectorId"

# ── Inbound Connector (SMTP-Modus only) ───────────────────────────────────────
if ($SkipInboundConnector) {
    Write-Warn "Inbound Connector skipped (not needed for Graph/IMAP mode)"
} else {
    Write-Step "Checking Inbound Connector..."
    $inName = "EXO Signature Gateway - Inbound"
    $existingIn = Get-InboundConnector -Identity $inName -ErrorAction SilentlyContinue
    if ($existingIn) {
        Write-Warn "Inbound Connector '$inName' exists — updating TLS"
        Set-InboundConnector `
            -Identity $inName `
            -RequireTls $true `
            -TlsSenderCertificateName $SmtpProxyHostname `
            -Enabled $true `
            -Comment $managedBy
        Write-OK "Inbound Connector updated"
    } else {
        New-InboundConnector `
            -Name $inName `
            -ConnectorType OnPremises `
            -SenderDomains @("*") `
            -RequireTls $true `
            -TlsSenderCertificateName $SmtpProxyHostname `
            -Enabled $true `
            -Comment $managedBy | Out-Null
        Write-OK "Inbound Connector created ← $SmtpProxyHostname (TLS required)"
    }
}

# ── Transport Rule ────────────────────────────────────────────────────────────
Write-Step "Checking Transport Rule..."

$ruleName = "Route via EXO Signature Gateway"
$existingRule = Get-TransportRule -Identity $ruleName -ErrorAction SilentlyContinue

if ($existingRule) {
    Write-Warn "Transport Rule '$ruleName' exists — updating comment"
    Set-TransportRule -Identity $ruleName -Comments $managedBy | Out-Null
    Write-OK "Transport Rule comment updated"
} else {
    New-TransportRule `
        -Name $ruleName `
        -FromScope InOrganization `
        -SentToScope NotInOrganization `
        -ExceptIfHeaderMatchesMessageHeader "X-Sig-Applied" `
        -ExceptIfHeaderMatchesPatterns "1" `
        -RouteMessageOutboundConnector $outConnectorId `
        -Priority 0 `
        -Comments $managedBy `
        -Mode Enforce | Out-Null
    Write-OK "Transport Rule created (priority 0)"
}

# ── Done ──────────────────────────────────────────────────────────────────────
Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue

Write-OK "Setup complete."
Write-Host ""
Write-Host "Mail flow:"
if ($SkipInboundConnector) {
    # Graph/IMAP-Modus: Re-inject ohne Inbound-Connector/Smarthost ($inName ist hier nicht gesetzt)
    Write-Host "  EXO → [$outName] → Gateway"
    Write-Host "  Gateway → EXO (Graph API sendMail / IMAP APPEND, kein Port 25)"
} else {
    Write-Host "  EXO → [$outName] → $SmtpProxyHostname:25"
    Write-Host "  $SmtpProxyHostname:25 → EXO Smarthost → [$inName] → Delivery"
}
Write-Host ""
Write-Host "Loop prevention: header X-Sig-Applied: 1 (set by proxy before re-inject)"
Write-Host "Forwarding: FromScope=InOrganization ensures only internally-composed mails are routed"
