#Requires -Modules ExchangeOnlineManagement
<#
.SYNOPSIS
    READ-ONLY-Abzug der gateway-relevanten Tenant-Konfiguration als JSON.
    Ausschliesslich Get-* — kein schreibender Eingriff. Fuer den Soll-Abgleich
    (tools/tenant_soll_check.py). Test-Mode-Connectoren werden EINBEZOGEN, sonst
    bleiben Validierungs-Reste unsichtbar.
#>
param(
    [Parameter(Mandatory)][string]$AppId,
    [Parameter(Mandatory)][string]$Organization,
    [Parameter(Mandatory)][string]$CertPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
    $CertPath, [string]$null,
    ([System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet))
Connect-ExchangeOnline -AppId $AppId -Certificate $cert -Organization $Organization `
    -ShowBanner:$false -ShowProgress:$false | Out-Null

try {
    $gw = "*Signature Gateway*"
    $out = [ordered]@{}

    $out.transportregeln = @(Get-TransportRule | Where-Object {
        $_.Name -like "*Route via*" -or $_.Name -like $gw -or $_.Name -like "*SMIME*" } | ForEach-Object {
      [ordered]@{ Name=$_.Name; State="$($_.State)"; Priority=$_.Priority; Mode="$($_.Mode)";
        FromScope="$($_.FromScope)"; SentToScope="$($_.SentToScope)";
        FromMemberOf=@($_.FromMemberOf | ForEach-Object { "$_" });
        RouteMessageOutboundConnector="$($_.RouteMessageOutboundConnector)";
        ExceptIfHeaderMatchesMessageHeader="$($_.ExceptIfHeaderMatchesMessageHeader)";
        ExceptIfMessageTypeMatches="$($_.ExceptIfMessageTypeMatches)";
        MessageTypeMatches="$($_.MessageTypeMatches)";
        StopRuleProcessing=$_.StopRuleProcessing } })

    $out.outbound_connectoren = @(Get-OutboundConnector -IncludeTestModeConnectors $true |
        Where-Object { $_.Name -like $gw } | ForEach-Object {
      [ordered]@{ Name=$_.Name; ConnectorType="$($_.ConnectorType)"; Enabled=$_.Enabled;
        IsValidated=$_.IsValidated; UseMXRecord=$_.UseMXRecord;
        SmartHosts=@($_.SmartHosts | ForEach-Object { "$_" }); TlsSettings="$($_.TlsSettings)";
        TlsDomain="$($_.TlsDomain)"; IsTransportRuleScoped=$_.IsTransportRuleScoped } })

    $out.inbound_connectoren = @(Get-InboundConnector | Where-Object { $_.Name -like $gw } | ForEach-Object {
      [ordered]@{ Name=$_.Name; ConnectorType="$($_.ConnectorType)"; Enabled=$_.Enabled;
        SenderDomains=@($_.SenderDomains | ForEach-Object { "$_" }); RequireTls=$_.RequireTls;
        TlsSenderCertificateName="$($_.TlsSenderCertificateName)" } })

    $out.verteilerlisten = @(Get-DistributionGroup | Where-Object { $_.Name -like $gw } | ForEach-Object {
      $dg=$_
      [ordered]@{ Name=$dg.Name; Alias=$dg.Alias; MemberJoinRestriction="$($dg.MemberJoinRestriction)";
        MemberDepartRestriction="$($dg.MemberDepartRestriction)";
        RequireSenderAuthenticationEnabled=$dg.RequireSenderAuthenticationEnabled;
        Mitglieder=@(Get-DistributionGroupMember -Identity $dg.Identity -ResultSize Unlimited -ErrorAction SilentlyContinue |
                     ForEach-Object { "$($_.PrimarySmtpAddress)" }) } })

    $out.remotedomain_castle = @(Get-RemoteDomain | Where-Object {
        $_.Name -like "*Castle*" -or $_.DomainName -like "*castle*" } | ForEach-Object {
      [ordered]@{ Name=$_.Name; DomainName="$($_.DomainName)";
        ByteEncoderTypeFor7BitCharsets="$($_.ByteEncoderTypeFor7BitCharsets)";
        ContentType="$($_.ContentType)"; TNEFEnabled=$_.TNEFEnabled } })

    $out | ConvertTo-Json -Depth 6
} finally {
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
}
