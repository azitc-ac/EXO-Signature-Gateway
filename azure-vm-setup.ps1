#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Erstellt eine Azure VM für den EXO Signature Gateway.

.DESCRIPTION
    Legt eine Debian 12 Bookworm-VM (Standard_B2s) mit statischer öffentlicher IP an,
    öffnet die benötigten Ports (80, 443, 25, 22) und installiert Docker + das Gateway.

.PARAMETER ResourceGroup
    Name der Ressourcengruppe (wird angelegt falls nicht vorhanden).

.PARAMETER Location
    Azure-Region, z.B. "germanywestcentral" oder "westeurope".

.PARAMETER VmName
    Name der VM.

.PARAMETER AdminUser
    SSH-Benutzername auf der VM.

.PARAMETER SshPublicKeyFile
    Pfad zur lokalen SSH-Public-Key-Datei (z.B. ~/.ssh/id_rsa.pub).

.PARAMETER VmSize
    Azure-VM-Groesse (Standard_B2s ist breit verfuegbar; Standard_B1ms kann je nach
    Region/Kapazitaet nicht verfuegbar sein).

.PARAMETER RepoUrl
    Git-Repository-URL des Gateways.

.EXAMPLE
    .\azure-vm-setup.ps1 -ResourceGroup "exo-gateway-rg" -Location "germanywestcentral" `
        -SshPublicKeyFile "~/.ssh/id_rsa.pub"

.EXAMPLE
    .\azure-vm-setup.ps1 -Location "northeurope" -VmSize "Standard_B2ps_v2" `
        -SshPublicKeyFile "~/.ssh/id_rsa.pub"
#>

param(
    [string]$ResourceGroup  = "exo-gateway-rg",
    [string]$Location       = "germanywestcentral",
    [string]$VmName         = "exo-gateway",
    [string]$AdminUser      = "azureuser",
    [string]$VmSize         = "Standard_B2ps_v2",
    [string]$VmImage        = "Debian:debian-12-arm64:12-arm64:latest",
    [string]$SshPublicKeyFile = "~/.ssh/id_rsa.pub",
    [string]$RepoUrl        = "https://github.com/azitc-ac/EXO-signature-service.git"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Az CLI: nur echte Fehler auf stderr; Region-Kosten-Hinweis dauerhaft deaktivieren
$env:AZURE_CORE_ONLY_SHOW_ERRORS = 'true'
$null = az config set core.display_region_identified=false 2>$null

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    OK: $msg" -ForegroundColor Green }
function Write-Info($msg) { Write-Host "    $msg" -ForegroundColor Gray }

# Wrapper für az-Aufrufe: stderr intern unterdrücken, bei echtem Fehler abbrechen.
# PS 5.x behandelt jede stderr-Ausgabe nativer Befehle als NativeCommandError —
# das verhindert diese Funktion, ohne echte Fehler zu verschlucken.
function Invoke-Az([scriptblock]$cmd) {
    $ErrorActionPreference = "Continue"
    $output = & $cmd 2>&1
    $ec     = $LASTEXITCODE
    $errors = $output | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] }
    $result = $output | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] }
    if ($ec -ne 0) {
        if ($errors) { Write-Host ($errors -join "`n") -ForegroundColor Red }
        throw "az-Aufruf fehlgeschlagen (ExitCode $ec)"
    }
    $result
}

# ── Prüfungen ──────────────────────────────────────────────────────────────────
Write-Step "Voraussetzungen prüfen"

# PATH aus der Registry neu laden — nötig wenn Azure CLI gerade erst installiert wurde
$env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
            [System.Environment]::GetEnvironmentVariable('Path', 'User')

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "Azure CLI (az) nicht gefunden. Installation: https://aka.ms/installazurecliwindows`nFalls gerade installiert: PowerShell-Fenster neu starten und Skript erneut ausführen."
}

$login = $null
try { $login = (az account show 2>$null) | ConvertFrom-Json } catch {}
if (-not $login) {
    Write-Info "Nicht angemeldet — öffne Browser für az login..."
    az login | Out-Null
    try { $login = (az account show 2>$null) | ConvertFrom-Json } catch {}
    if (-not $login) { throw "Anmeldung fehlgeschlagen. Bitte 'az login' manuell ausführen." }
}
Write-Ok "Azure CLI OK — Subscription: $($login.name)"

$sshKey = (Resolve-Path $SshPublicKeyFile -ErrorAction Stop).Path
$sshKeyContent = Get-Content $sshKey -Raw
Write-Ok "SSH-Key: $sshKey"

# ── Ressourcengruppe ───────────────────────────────────────────────────────────
Write-Step "Ressourcengruppe '$ResourceGroup' in '$Location'"
if ($Location -notin @('northeurope','westeurope')) {
    Write-Info "Tipp: 'northeurope' oder 'westeurope' sind oft günstiger als '$Location'."
    Write-Info "      Für strenge EU-Datenschutzanforderungen bleibt '$Location' die richtige Wahl."
}
Invoke-Az { az group create --name $ResourceGroup --location $Location --output none }
Write-Ok "Ressourcengruppe bereit"

# ── Statische öffentliche IP ───────────────────────────────────────────────────
Write-Step "Statische öffentliche IP anlegen"
Invoke-Az {
    az network public-ip create `
        --resource-group $ResourceGroup `
        --name "$VmName-ip" `
        --allocation-method Static `
        --sku Standard `
        --output none
}
$publicIp = Invoke-Az {
    az network public-ip show `
        --resource-group $ResourceGroup `
        --name "$VmName-ip" `
        --query "ipAddress" -o tsv
}
Write-Ok "Öffentliche IP: $publicIp"

# ── VM anlegen ─────────────────────────────────────────────────────────────────
Write-Step "VM '$VmName' anlegen ($VmSize, Debian 12 Bookworm)"
Write-Info "Das dauert ca. 2–3 Minuten..."

Invoke-Az {
    az vm create `
        --resource-group $ResourceGroup `
        --name $VmName `
        --image $VmImage `
        --size $VmSize `
        --admin-username $AdminUser `
        --ssh-key-values $sshKeyContent `
        --public-ip-address "$VmName-ip" `
        --public-ip-sku Standard `
        --os-disk-size-gb 32 `
        --output none
}
Write-Ok "VM erstellt"

# ── Netzwerk-Sicherheitsgruppe: Ports öffnen ──────────────────────────────────
Write-Step "Firewall-Regeln setzen"

$rules = @(
    @{ name="SSH";   port=22;  prio=100; desc="SSH-Zugang (Verwaltung)" },
    @{ name="HTTP";  port=80;  prio=110; desc="Let's Encrypt HTTP-01 + Setup-Wizard" },
    @{ name="HTTPS"; port=443; prio=120; desc="Web-UI + Office Add-in" },
    @{ name="SMTP";  port=25;  prio=130; desc="Exchange Online Outbound Connector" }
)

foreach ($r in $rules) {
    Invoke-Az {
        az network nsg rule create `
            --resource-group $ResourceGroup `
            --nsg-name "$VmName-NSG" `
            --name $r.name `
            --priority $r.prio `
            --protocol Tcp `
            --destination-port-ranges $r.port `
            --access Allow `
            --direction Inbound `
            --description $r.desc `
            --output none
    }
    Write-Ok "Port $($r.port) ($($r.name)) geöffnet"
}

# ── Docker + Gateway per cloud-init installieren ───────────────────────────────
Write-Step "Docker + EXO Gateway auf VM installieren"
Write-Info "cloud-init läuft nach dem ersten Boot im Hintergrund (~3–5 Min.)..."

# Kein here-string: PS 5.x auf Windows hat Probleme mit @'...'@ bei LF-Zeilenenden.
# Array + -join ist auf allen PS-Versionen und Zeilenendungen zuverlässig.
$cloudInit = @(
    '#cloud-config',
    'package_update: true',
    'package_upgrade: false',
    'packages:',
    '  - ca-certificates',
    '  - curl',
    '  - git',
    '',
    'runcmd:',
    '  # Docker installieren',
    '  - install -m 0755 -d /etc/apt/keyrings',
    '  - curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc',
    '  - chmod a+r /etc/apt/keyrings/docker.asc',
    '  - echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list',
    '  - apt-get update -qq',
    '  - apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin',
    "  - usermod -aG docker $AdminUser",
    '  - systemctl enable docker',
    '  # Repository klonen',
    "  - git clone $RepoUrl /opt/exo-gateway",
    "  - chown -R ${AdminUser}:${AdminUser} /opt/exo-gateway",
    '  # .env anlegen (Platzhalter)',
    '  - |',
    '    cat > /opt/exo-gateway/.env << ENVEOF',
    '    WEBUI_SECRET_KEY=$(openssl rand -hex 32)',
    '    WEBUI_PASSWORD=changeme',
    '    TENANT_ID=',
    '    CLIENT_ID=',
    '    CLIENT_SECRET=',
    '    EXO_SMARTHOST=',
    '    ENVEOF',
    "  - chown ${AdminUser}:${AdminUser} /opt/exo-gateway/.env",
    '  - chmod 600 /opt/exo-gateway/.env',
    '  # Bind-Mount-Verzeichnisse dem Container-User (appuser, UID 1000) uebereignen.',
    '  # Sonst legt der Docker-Daemon sie beim ersten Mount als root an und der als',
    '  # appuser laufende Container crasht mit PermissionError (Restart-Loop / Backup-Restore).',
    '  - mkdir -p /opt/exo-gateway/data /opt/exo-gateway/certs /opt/exo-gateway/templates',
    '  - chown -R 1000:1000 /opt/exo-gateway/data /opt/exo-gateway/certs /opt/exo-gateway/templates',
    '  # Gateway starten',
    '  - cd /opt/exo-gateway && docker compose up -d',
    '  # Update-Watcher: Script liegt im Repo, wird via git clone bereits platziert.',
    '  - chmod 755 /opt/exo-gateway/update-watcher.sh',
    '  - |',
    '    cat > /etc/systemd/system/exo-gateway-updater.service << UNITEOF',
    '    [Unit]',
    '    Description=EXO Gateway Update Watcher',
    '    After=docker.service',
    '    Requires=docker.service',
    '    [Service]',
    '    ExecStart=/bin/bash /opt/exo-gateway/update-watcher.sh',
    '    Restart=always',
    '    RestartSec=10',
    '    User=root',
    '    [Install]',
    '    WantedBy=multi-user.target',
    '    UNITEOF',
    '  - systemctl daemon-reload',
    '  - systemctl enable --now exo-gateway-updater'
) -join "`n"

$cloudInitFile = [System.IO.Path]::GetTempFileName()
$cloudInit | Set-Content $cloudInitFile -Encoding UTF8

Invoke-Az {
    az vm run-command invoke `
        --resource-group $ResourceGroup `
        --name $VmName `
        --command-id RunShellScript `
        --scripts (Get-Content $cloudInitFile -Raw) `
        --output none
}
Remove-Item $cloudInitFile

Write-Ok "Installations-Skript gestartet"

# ── Zusammenfassung ────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor White
Write-Host " EXO Signature Gateway — VM bereit" -ForegroundColor White
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor White
Write-Host ""
Write-Host " Öffentliche IP : $publicIp" -ForegroundColor Yellow
Write-Host ""
Write-Host " Nächste Schritte:" -ForegroundColor White
Write-Host ""
Write-Host " 1. DNS setzen:" -ForegroundColor Cyan
Write-Host "    sig.example.com  A  $publicIp"
Write-Host ""
Write-Host " 2. Setup-Wizard aufrufen (nach DNS-Propagation, ca. 5 Min.):" -ForegroundColor Cyan
Write-Host "    http://$publicIp" -ForegroundColor Yellow
Write-Host "    oder nach DNS: http://sig.example.com"
Write-Host ""
Write-Host " 3. Hostname + Let's Encrypt konfigurieren → Cert beantragen"
Write-Host "    → docker compose restart (auf der VM)"
Write-Host ""
Write-Host " 4. HTTPS-Setup-Wizard:" -ForegroundColor Cyan
Write-Host "    https://sig.example.com"
Write-Host ""
Write-Host " 5. .env auf der VM ergänzen:" -ForegroundColor Cyan
Write-Host "    ssh $AdminUser@$publicIp"
Write-Host "    nano /opt/exo-gateway/.env"
Write-Host ""
Write-Host " SSH:" -ForegroundColor Gray
Write-Host "    ssh $AdminUser@$publicIp"
Write-Host ""
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor White
