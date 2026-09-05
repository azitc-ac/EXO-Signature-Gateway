<#
.SYNOPSIS
    Legt die Azure Function fuer Header-Echo an und laedt den Code hoch.

.DESCRIPTION
    Voraussetzung ist nur die Azure CLI (az), angemeldet per "az login" und auf
    das richtige Abonnement gesetzt ("az account set"). Azure Functions Core
    Tools werden NICHT benoetigt: der Code geht als Zip mit Remote-Build hoch.

    Das Skript ist wiederholbar. Vorhandene Ressourcengruppe, Speicherkonto und
    Function App werden erkannt und weiterverwendet; nur die App Settings und
    der Code werden bei jedem Lauf neu gesetzt.

    Kostenrahmen: Consumption-Plan mit Freikontingent (1 Mio. Ausfuehrungen
    und 400.000 GB-s je Monat), dazu ein Speicherkonto Standard_LRS mit
    wenigen Cent monatlich fuer die Timer-Sperre.

.PARAMETER FunctionAppName
    Weltweit eindeutiger Name der Function App (wird Teil des Hostnamens).

.PARAMETER MailUser
    IONOS-Postfach, z. B. echo@azitc.org. Dient fuer IMAP und SMTP.

.PARAMETER MailPassword
    Passwort des Postfachs als SecureString. Fehlt es, wird es abgefragt.

.PARAMETER PlanKind
    Consumption (Vorgabe) oder FlexConsumption. Lehnt Azure die Anlage eines
    klassischen Linux-Consumption-Plans in der Region ab, FlexConsumption waehlen.

.PARAMETER DryRun
    Setzt ECHO_DRY_RUN=true: die Function entscheidet und protokolliert nur,
    sendet und verschiebt aber nichts. Fuer den ersten Test empfohlen.

.PARAMETER NoAuthCheck
    Setzt ECHO_REQUIRE_AUTH_PASS=false. Nur, wenn der Posteingang keinen
    brauchbaren Authentication-Results-Header traegt (siehe README).

.PARAMETER SkipInfrastructure
    Nur App Settings setzen und Code hochladen; keine Ressourcen anlegen.

.PARAMETER SkipDeploy
    Nur Ressourcen und App Settings; keinen Code hochladen.

.EXAMPLE
    .\deploy.ps1 -FunctionAppName azitc-header-echo -MailUser echo@azitc.org -DryRun

.EXAMPLE
    .\deploy.ps1 -FunctionAppName azitc-header-echo -MailUser echo@azitc.org -SkipInfrastructure
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-zA-Z][a-zA-Z0-9-]{1,58}$')]
    [string]$FunctionAppName,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[^@\s]+@[^@\s]+\.[^@\s]+$')]
    [string]$MailUser,

    [securestring]$MailPassword,

    [string]$ResourceGroup = 'rg-header-echo',
    [string]$Location = 'germanywestcentral',
    [string]$StorageAccountName = '',
    [ValidateSet('Consumption', 'FlexConsumption')]
    [string]$PlanKind = 'Consumption',

    [string]$ImapHost = 'imap.ionos.de',
    [int]$ImapPort = 993,
    [string]$SmtpHost = 'smtp.ionos.de',
    [int]$SmtpPort = 587,
    [string]$EchoFrom = '',
    [string]$AllowedSenderDomains = '',
    [string]$AuthservId = '',
    [int]$PerSenderDailyLimit = 20,
    [int]$DailyLimit = 200,
    [int]$MaxAgeHours = 24,

    [switch]$DryRun,
    [switch]$NoAuthCheck,
    [switch]$SkipInfrastructure,
    [switch]$SkipDeploy
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

function Invoke-Az {
    # Fuehrt az aus und bricht bei Fehlern ab. Ausgabe wird durchgereicht.
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    Write-Verbose ("az " + ($Arguments -join ' '))
    & az @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw ("az " + ($Arguments[0..1] -join ' ') + " fehlgeschlagen (Exitcode $LASTEXITCODE)")
    }
}

function Test-AzResource {
    # Gibt $true zurueck, wenn "az ... show" gelingt. Fehlermeldungen von az
    # werden unterdrueckt; unter PowerShell 5.1 darf dabei nichts nach 2>&1
    # umgeleitet werden, sonst wird der stderr-Text zum abbrechenden Fehler.
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $null = & az @Arguments --only-show-errors -o none 2> $null
        return ($LASTEXITCODE -eq 0)
    }
    finally {
        $ErrorActionPreference = $previous
    }
}

function ConvertTo-PlainText {
    param([Parameter(Mandatory = $true)][securestring]$Secure)
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function New-DeploymentZip {
    # Packt nur, was die Function App braucht. Eintragsnamen ausdruecklich mit
    # Schraegstrich: Compress-Archive aus PowerShell 5.1 schreibt Backslashes,
    # und der Linux-Host entpackt daraus Dateien namens "header_echo\core.py".
    param(
        [Parameter(Mandatory = $true)][string]$SourceRoot,
        [Parameter(Mandatory = $true)][string]$ZipPath
    )
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem

    $include = @('function_app.py', 'host.json', 'requirements.txt')
    $files = @()
    foreach ($name in $include) {
        $files += Get-Item -LiteralPath (Join-Path $SourceRoot $name)
    }
    $files += Get-ChildItem -LiteralPath (Join-Path $SourceRoot 'header_echo') -Filter '*.py' -File

    if (Test-Path -LiteralPath $ZipPath) { Remove-Item -LiteralPath $ZipPath -Force }
    $archive = [IO.Compression.ZipFile]::Open($ZipPath, [IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($file in $files) {
            $entry = $file.FullName.Substring($SourceRoot.Length).TrimStart('\', '/') -replace '\\', '/'
            $null = [IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $archive, $file.FullName, $entry, [IO.Compression.CompressionLevel]::Optimal)
            Write-Verbose "  + $entry"
        }
    }
    finally {
        $archive.Dispose()
    }
    return $files.Count
}

# ---------------------------------------------------------------------------
# Vorbereitung
# ---------------------------------------------------------------------------

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw 'Azure CLI (az) nicht gefunden. Installation: https://aka.ms/installazurecli'
}

$sourceRoot = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($sourceRoot)) { $sourceRoot = (Get-Location).Path }
foreach ($required in @('function_app.py', 'host.json', 'requirements.txt', 'header_echo\core.py')) {
    if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot $required))) {
        throw "Datei fehlt: $required. Das Skript muss im Verzeichnis tools\header-echo liegen."
    }
}

if ($null -eq $MailPassword) {
    $MailPassword = Read-Host -AsSecureString -Prompt "IONOS-Passwort fuer $MailUser"
}
$plainPassword = ConvertTo-PlainText -Secure $MailPassword
if ([string]::IsNullOrEmpty($plainPassword)) { throw 'Leeres Passwort.' }

if ([string]::IsNullOrWhiteSpace($StorageAccountName)) {
    # 3-24 Zeichen, nur Kleinbuchstaben und Ziffern, weltweit eindeutig.
    $base = 'st' + ($FunctionAppName.ToLowerInvariant() -replace '[^a-z0-9]', '')
    if ($base.Length -gt 24) { $base = $base.Substring(0, 24) }
    $StorageAccountName = $base
}
if ([string]::IsNullOrWhiteSpace($EchoFrom)) { $EchoFrom = $MailUser }

$previous = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$account = (& az account show --query '{name:name, id:id}' -o tsv 2> $null)
$ErrorActionPreference = $previous
if ($LASTEXITCODE -ne 0) { throw 'Nicht angemeldet. Bitte zuerst "az login" ausfuehren.' }
Write-Host "Abonnement:      $account"
Write-Host "Ressourcengruppe: $ResourceGroup ($Location)"
Write-Host "Speicherkonto:    $StorageAccountName"
Write-Host "Function App:     $FunctionAppName ($PlanKind)"
Write-Host "Postfach:         $MailUser via $ImapHost / $SmtpHost"

# ---------------------------------------------------------------------------
# Ressourcen
# ---------------------------------------------------------------------------

if (-not $SkipInfrastructure) {
    Write-Host ''
    Write-Host '== Ressourcen =='
    Invoke-Az @('group', 'create', '--name', $ResourceGroup, '--location', $Location, '-o', 'none')

    if (Test-AzResource @('storage', 'account', 'show', '--name', $StorageAccountName, '--resource-group', $ResourceGroup)) {
        Write-Host "Speicherkonto vorhanden: $StorageAccountName"
    }
    else {
        Write-Host "Lege Speicherkonto an: $StorageAccountName"
        Invoke-Az @('storage', 'account', 'create',
            '--name', $StorageAccountName, '--resource-group', $ResourceGroup, '--location', $Location,
            '--sku', 'Standard_LRS', '--kind', 'StorageV2',
            '--min-tls-version', 'TLS1_2', '--allow-blob-public-access', 'false', '-o', 'none')
    }

    if (Test-AzResource @('functionapp', 'show', '--name', $FunctionAppName, '--resource-group', $ResourceGroup)) {
        Write-Host "Function App vorhanden: $FunctionAppName"
    }
    else {
        Write-Host "Lege Function App an: $FunctionAppName"
        $createArgs = @('functionapp', 'create',
            '--name', $FunctionAppName, '--resource-group', $ResourceGroup,
            '--storage-account', $StorageAccountName,
            '--runtime', 'python', '--runtime-version', '3.11', '--functions-version', '4',
            '-o', 'none')
        if ($PlanKind -eq 'FlexConsumption') {
            $createArgs += @('--flexconsumption-location', $Location)
        }
        else {
            $createArgs += @('--consumption-plan-location', $Location, '--os-type', 'Linux')
        }
        Invoke-Az $createArgs
    }
}

# ---------------------------------------------------------------------------
# App Settings (ueber eine Datei, damit das Passwort nicht in der
# Prozessliste erscheint; die Datei wird sofort wieder geloescht)
# ---------------------------------------------------------------------------

Write-Host ''
Write-Host '== App Settings =='
$requireAuth = 'true'
if ($NoAuthCheck) { $requireAuth = 'false' }
$dryRunValue = 'false'
if ($DryRun) { $dryRunValue = 'true' }

$settings = @(
    @{ name = 'ECHO_MAIL_USER'; value = $MailUser; slotSetting = $false },
    @{ name = 'ECHO_MAIL_PASSWORD'; value = $plainPassword; slotSetting = $false },
    @{ name = 'ECHO_FROM'; value = $EchoFrom; slotSetting = $false },
    @{ name = 'ECHO_IMAP_HOST'; value = $ImapHost; slotSetting = $false },
    @{ name = 'ECHO_IMAP_PORT'; value = "$ImapPort"; slotSetting = $false },
    @{ name = 'ECHO_SMTP_HOST'; value = $SmtpHost; slotSetting = $false },
    @{ name = 'ECHO_SMTP_PORT'; value = "$SmtpPort"; slotSetting = $false },
    @{ name = 'ECHO_REQUIRE_AUTH_PASS'; value = $requireAuth; slotSetting = $false },
    @{ name = 'ECHO_AUTHSERV_ID'; value = $AuthservId; slotSetting = $false },
    @{ name = 'ECHO_ALLOWED_SENDER_DOMAINS'; value = $AllowedSenderDomains; slotSetting = $false },
    @{ name = 'ECHO_PER_SENDER_DAILY_LIMIT'; value = "$PerSenderDailyLimit"; slotSetting = $false },
    @{ name = 'ECHO_DAILY_LIMIT'; value = "$DailyLimit"; slotSetting = $false },
    @{ name = 'ECHO_MAX_AGE_HOURS'; value = "$MaxAgeHours"; slotSetting = $false },
    @{ name = 'ECHO_DRY_RUN'; value = $dryRunValue; slotSetting = $false }
)
if ($PlanKind -eq 'Consumption') {
    $settings += @{ name = 'SCM_DO_BUILD_DURING_DEPLOYMENT'; value = 'true'; slotSetting = $false }
    $settings += @{ name = 'ENABLE_ORYX_BUILD'; value = 'true'; slotSetting = $false }
}

$settingsFile = Join-Path ([IO.Path]::GetTempPath()) ("header-echo-settings-" + [Guid]::NewGuid().ToString('N') + ".json")
try {
    $json = ConvertTo-Json -InputObject $settings -Depth 3
    [IO.File]::WriteAllText($settingsFile, $json, (New-Object Text.UTF8Encoding($false)))
    Invoke-Az @('functionapp', 'config', 'appsettings', 'set',
        '--name', $FunctionAppName, '--resource-group', $ResourceGroup,
        '--settings', "@$settingsFile", '-o', 'none')
    Write-Host ("{0} Einstellungen gesetzt (Trockenlauf: {1}, Auth-Pruefung: {2})" -f $settings.Count, $dryRunValue, $requireAuth)
}
finally {
    if (Test-Path -LiteralPath $settingsFile) { Remove-Item -LiteralPath $settingsFile -Force }
    $plainPassword = $null
}

# ---------------------------------------------------------------------------
# Code hochladen
# ---------------------------------------------------------------------------

if (-not $SkipDeploy) {
    Write-Host ''
    Write-Host '== Code =='
    $zipPath = Join-Path ([IO.Path]::GetTempPath()) 'header-echo-deploy.zip'
    $count = New-DeploymentZip -SourceRoot $sourceRoot -ZipPath $zipPath
    Write-Host "Paket mit $count Dateien: $zipPath"

    Invoke-Az @('functionapp', 'deployment', 'source', 'config-zip',
        '--name', $FunctionAppName, '--resource-group', $ResourceGroup,
        '--src', $zipPath, '--build-remote', 'true', '-o', 'none')
    Remove-Item -LiteralPath $zipPath -Force

    Write-Host 'Warte auf die Registrierung der Function ...'
    $found = $false
    for ($i = 0; $i -lt 8 -and -not $found; $i++) {
        Start-Sleep -Seconds 15
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $names = & az functionapp function list --name $FunctionAppName --resource-group $ResourceGroup --query '[].name' -o tsv 2> $null
        $ErrorActionPreference = $previous
        if ($LASTEXITCODE -eq 0 -and $names -and ("$names" -match 'header_echo')) { $found = $true }
    }
    if ($found) {
        Write-Host 'Function "header_echo" ist registriert und laeuft jede volle Minute.'
    }
    else {
        Write-Warning 'Function noch nicht sichtbar. Der Remote-Build kann einige Minuten dauern; spaeter pruefen mit:'
        Write-Warning "  az functionapp function list -n $FunctionAppName -g $ResourceGroup -o table"
    }
}

Write-Host ''
Write-Host '== Naechste Schritte =='
Write-Host "Protokoll live:   az webapp log tail -n $FunctionAppName -g $ResourceGroup"
if ($DryRun) {
    Write-Host 'Trockenlauf aktiv. Testmail an das Postfach senden, Protokoll lesen, dann scharf schalten:'
    Write-Host "  az functionapp config appsettings set -n $FunctionAppName -g $ResourceGroup --settings ECHO_DRY_RUN=false -o none"
}
