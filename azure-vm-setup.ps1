#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Erstellt eine Azure VM für den EXO Signature Gateway.

.DESCRIPTION
    Legt eine Debian 12 Bookworm-VM (Standard_B2pls_v2, ARM64) mit statischer öffentlicher IP an,
    öffnet die benötigten Ports (22, 80, 443, 25, 587) und installiert Docker + das Gateway.

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
    Azure-VM-Groesse. Vorgabe Standard_B2pls_v2 (ARM64, 2 vCPU, 4 GB). Eine x64-Groesse
    braucht ein passendes Image: -VmImage "Debian:debian-12:12-gen2:latest".

.PARAMETER RepoUrl
    Git-Repository-URL des Gateways.

.EXAMPLE
    .\azure-vm-setup.ps1 -ResourceGroup "exo-gateway-rg" -Location "germanywestcentral" `
        -SshPublicKeyFile "~/.ssh/id_rsa.pub"

.EXAMPLE
    .\azure-vm-setup.ps1 -Location "northeurope" -VmSize "Standard_B2als_v2" -VmImage "Debian:debian-12:12-gen2:latest" `
        -SshPublicKeyFile "~/.ssh/id_rsa.pub"
#>

param(
    [string]$ResourceGroup  = "exo-gateway-rg",
    [string]$Location       = "northeurope",
    [string]$VmName         = "exo-gateway",
    [string]$AdminUser      = "azureuser",
    [string]$VmSize         = "Standard_B2pls_v2",
    [string]$VmImage        = "Debian:debian-12:12-arm64:latest",
    [string]$SshPublicKeyFile = "~/.ssh/id_rsa.pub",
    [string]$RepoUrl        = "https://github.com/azitc-ac/EXO-Signature-Gateway.git"
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
        # Fehlertext mit in die Ausnahme geben, damit Aufrufer gezielt darauf
        # reagieren können (z.B. SkuNotAvailable → Hinweis auf Ausweichregion).
        $detail = if ($errors) { ": " + ($errors -join "; ") } else { "" }
        throw "az-Aufruf fehlgeschlagen (ExitCode $ec)$detail"
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

# ── cloud-init vorbereiten (Docker + Gateway) ─────────────────────────────────
# Der folgende YAML-Block wird unten als --custom-data an "az vm create" übergeben
# und läuft dadurch als echtes cloud-init beim ersten Boot der VM.
#
# NICHT über "az vm run-command invoke --command-id RunShellScript": das ist ein
# Bash-Interpreter, kein cloud-init. Jede runcmd-Zeile "- <befehl>" liest Bash als
# Aufruf des Programms "-" ("command not found: -"), sodass nichts installiert wird
# — der Prozess meldet trotzdem Exit 0, also grün und real leer.
Write-Step "cloud-init vorbereiten (Docker + EXO Gateway)"

# Die Konfiguration steht in EINER Datei, die auch die Azure-Vorlage
# (deploy/azure/azuredeploy.json, „Deploy to Azure“) einbettet. Liegt das Skript
# allein vor (nur die .ps1 heruntergeladen), kommt sie aus dem Repository.
$cloudInitPfad = Join-Path $PSScriptRoot "deploy/azure/cloud-init.yaml"
if (Test-Path $cloudInitPfad) {
    $cloudInit = Get-Content $cloudInitPfad -Raw -Encoding UTF8
} else {
    $rawUrl = ($RepoUrl -replace '\.git$', '' -replace '^https://github\.com/', 'https://raw.githubusercontent.com/') +
              '/main/deploy/azure/cloud-init.yaml'
    Write-Info "cloud-init.yaml nicht neben dem Skript — lade $rawUrl"
    $cloudInit = (Invoke-WebRequest -Uri $rawUrl -UseBasicParsing).Content
}
# Zeilenenden auf LF: cloud-init verträgt CRLF aus einem Windows-Checkout nicht.
$cloudInit = ($cloudInit -replace "`r`n", "`n").Replace('__ADMIN_USER__', $AdminUser).Replace('__REPO_URL__', $RepoUrl)

$cloudInitFile = [System.IO.Path]::GetTempFileName()
[System.IO.File]::WriteAllText($cloudInitFile, $cloudInit, (New-Object System.Text.UTF8Encoding $false))
Write-Ok "cloud-init-Datei vorbereitet"

# ── Netzwerk-Sicherheitsgruppe explizit anlegen ───────────────────────────────
# "az vm create" legt eine implizite NSG unter dem von Azure abgeleiteten Namen
# "${VmName}NSG" an — OHNE Bindestrich. Die Regel-Erstellung sprach dagegen
# "$VmName-NSG" an und starb mit ResourceNotFound. Deshalb die NSG hier mit einem
# selbst gewählten Namen anlegen und exakt diesen Namen an "az vm create" wie an
# die Regeln übergeben — nichts wird geraten.
$nsgName = "$VmName-nsg"
Write-Step "Netzwerk-Sicherheitsgruppe '$nsgName' anlegen"
Invoke-Az {
    az network nsg create `
        --resource-group $ResourceGroup `
        --name $nsgName `
        --output none
}
Write-Ok "NSG bereit"

# ── VM anlegen ─────────────────────────────────────────────────────────────────
Write-Step "VM '$VmName' anlegen ($VmSize, Debian 12 Bookworm)"
Write-Info "Das dauert ca. 2–3 Minuten..."

try {
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
            --nsg $nsgName `
            --os-disk-size-gb 32 `
            --custom-data $cloudInitFile `
            --output none
    }
}
catch {
    # Kapazitätssperre der gewählten Größe in dieser Region ist ein häufiger,
    # betreiberseitig lösbarer Fall — nicht kommentarlos hart sterben, sondern
    # den Ausweg nennen. Anlass: Standard_B2ps_v2 war in germanywestcentral
    # zeitweise SkuNotAvailable, während dieselbe Größe in northeurope lief.
    if ($_.Exception.Message -match 'SkuNotAvailable') {
        Write-Host ""
        Write-Host "    Die VM-Größe '$VmSize' ist in der Region '$Location' derzeit nicht verfügbar (SkuNotAvailable)." -ForegroundColor Yellow
        Write-Host "    Ausweg: eine andere Region versuchen, z.B.  -Location westeurope  oder  -Location germanywestcentral" -ForegroundColor Yellow
        Write-Host "    (arm64-Größen sind regional unterschiedlich kapazitätsgesperrt; eine andere Größe braucht ggf. ein Image der passenden Architektur über -VmImage.)" -ForegroundColor Yellow
        Write-Host ""
    }
    Remove-Item $cloudInitFile -ErrorAction SilentlyContinue
    throw
}
Write-Ok "VM erstellt"

Remove-Item $cloudInitFile
Write-Info "cloud-init läuft nach dem ersten Boot im Hintergrund (~3–5 Min.)..."

# ── Netzwerk-Sicherheitsgruppe: Ports öffnen ──────────────────────────────────
Write-Step "Firewall-Regeln setzen"

$rules = @(
    @{ name="SSH";        port=22;  prio=100; desc="SSH-Zugang (Verwaltung)" },
    @{ name="HTTP";       port=80;  prio=110; desc="Let's Encrypt HTTP-01 + Setup-Wizard" },
    @{ name="HTTPS";      port=443; prio=120; desc="Web-UI + Office Add-in" },
    @{ name="SMTP";       port=25;  prio=130; desc="Exchange Online Outbound Connector" },
    # Eingehende Submission (587) fuer Sende-Identitaeten: Geraete/Anwendungen
    # liefern mit eigenem Login ueber TLS ein. Der Listener oeffnet nur bei
    # vorhandenem TLS-Zertifikat und weist ohne aktivierte Submission jede
    # Anmeldung ab (Anmelde-Bremse gegen Brute-Force) — der offene Port ist damit
    # unkritisch. Wer die Quellen kennt, kann die Regel spaeter auf deren IPs engen.
    @{ name="Submission"; port=587; prio=140; desc="Sende-Identitaeten: authentifizierte Einlieferung (587)" }
)

foreach ($r in $rules) {
    Invoke-Az {
        az network nsg rule create `
            --resource-group $ResourceGroup `
            --nsg-name $nsgName `
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
Write-Host " SSH:" -ForegroundColor Gray
Write-Host "    ssh $AdminUser@$publicIp"
Write-Host ""
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor White
