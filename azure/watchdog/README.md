# Bypass-Wächter — Azure Function

Ein **externer** Wächter, der die Gateway-`/health` überwacht und bei Ausfall die
**Signatur**-Transportregel abschaltet (ausgehende Signatur-Post fließt dann
unsigniert weiter, statt in Exchange zu stauen); bei Erholung schaltet er sie
wieder ein. Die **S/MIME-Regel bleibt immer an** — verschlüsselungsfähige Post
darf nie unverschlüsselt hinaus (die wartet bewusst in der Queue).

Er läuft in Azure (serverlos), also **unabhängig vom Gateway-Host** — genau das
ist der Sinn: Wenn der Gateway-Host stirbt, muss jemand anderes handeln.

> Alternative: der **cron-Wächter** (`contrib/watchdog/`) für einen eigenen
> zweiten Host. Beide sind gleichwertig; die Auswahl (Variante, Heartbeat-Token,
> Aktivierung) triffst du in der Oberfläche unter **Erweitert → Bypass-Wächter**
> (`WATCHDOG_KIND` = `azure` | `cron`).

## Architektur
```
Timer (alle 5 Min)
  → GET https://<gateway>/health   (X-Watchdog-Token; mehrfach = entprellt)
  → ausgefallen?  Connect-ExchangeOnline -ManagedIdentity → Disable-TransportRule (Signatur)
  → erholt?       Enable-TransportRule (Signatur)
```
Authentifizierung an Exchange per **Managed Identity** (kein Zertifikat zu
verwalten) mit der **minimalen** Rolle „Transport Rules" (siehe unten).

## Deploy (Skizze, `az` CLI)
```bash
RG=rg-exo-watchdog; APP=exo-sig-watchdog; LOC=northeurope

# ⚠️ IMMER die aktuell unterstützte PowerShell-Version nehmen — NICHT diese Zahl blind
# kopieren. Aktuelle Liste (höchste = neueste):
#   az functionapp list-runtimes --os windows --query "powershell[].version" -o tsv
PS_VER=7.6

az group create -n $RG -l $LOC
az storage account create -n exosigwd$RANDOM -g $RG -l $LOC --sku Standard_LRS --min-tls-version TLS1_2
az functionapp create -n $APP -g $RG -s <storage> -c $LOC --os-type Windows \
   --runtime powershell --runtime-version $PS_VER --functions-version 4 --consumption-plan-location $LOC
az functionapp identity assign -n $APP -g $RG          # System-assigned Managed Identity

# Code deployen — mit Functions Core Tools …
func azure functionapp publish $APP                     # aus diesem Verzeichnis
# … oder OHNE func per Zip:
#   zip -r wd.zip host.json requirements.psd1 Watchdog/
#   az functionapp deployment source config-zip -n $APP -g $RG --src wd.zip

# App-Einstellungen (SIG_RULE_NAME aus der Gateway-UI kopieren)
az functionapp config appsettings set -n $APP -g $RG --settings \
   GATEWAY_HEALTH_URL="https://sig.zarenko.net/health" \
   EXO_ORGANIZATION="zarenko.onmicrosoft.com" \
   SIG_RULE_NAME="Route via EXO Signature Gateway (Signatur)" \
   FAIL_THRESHOLD=3 \
   WATCHDOG_TOKEN="<Token aus /api/watchdog/token/rotate>"

# Läuft eine ältere Runtime? (az warnt beim create/config) — nachziehen:
#   az functionapp config set -n $APP -g $RG --powershell-version $PS_VER
# Prüfen:  az functionapp config show -n $APP -g $RG --query powerShellVersion -o tsv
```

Die Managed Identity braucht für Exchange **drei** Zuweisungen — least-privilege,
**ohne** breite „Exchange Administrator"-Rolle (2026-09-07 live bestätigt: der
Wächter schaltet die Regel damit, ohne EXO-Voll-Admin). Alle einmalig, brauchen
Tenant-Directory-Admin. `MI_OBJ` ist die Objekt-ID der MI (`az functionapp identity
show -n $APP -g $RG --query principalId -o tsv`).

> **Am einfachsten über die Oberfläche:** *Erweitert → Bypass-Wächter →
> „Berechtigungen der Wächter-Identität einrichten"* — dort MI-AppId + Objekt-ID
> eintragen und **„Berechtigungen erteilen (Azure-Login)"** (macht Schritt 1+2 mit
> deinem Admin-Token, ganz ohne Kommandozeile) sowie **„Rolle zuweisen"** (Schritt 3).
> Die folgenden Befehle sind der manuelle Weg zur selben Sache.

**1. App-Rolle `Exchange.ManageAsApp`** — der reine *Anmelde*-Schlüssel. Allein
erlaubt sie NICHTS (jedes Cmdlet → 403); Berechtigungen kommen aus Schritt 2+3:
```bash
EXO_SP=$(az ad sp show --id 00000002-0000-0ff1-ce00-000000000000 --query id -o tsv)
az rest --method POST \
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/$MI_OBJ/appRoleAssignments" \
  --headers "Content-Type=application/json" \
  --body "{\"principalId\":\"$MI_OBJ\",\"resourceId\":\"$EXO_SP\",\"appRoleId\":\"dc50a0fb-09a3-484d-be87-e023b12c6440\"}"
```

**2. Entra-Rolle `Global Reader`** (nur-lesen) — Managed Identities können sich nur
mit einer *unterstützten* Directory-Rolle an EXO PowerShell anmelden. Global Reader
ist die kleinste, die das erlaubt (liest, schreibt nichts):
```bash
az rest --method POST \
  --url "https://graph.microsoft.com/v1.0/roleManagement/directory/roleAssignments" \
  --headers "Content-Type=application/json" \
  --body "{\"principalId\":\"$MI_OBJ\",\"roleDefinitionId\":\"f2ef992c-3afb-46b9-b7cf-a126ee74c451\",\"directoryScopeId\":\"/\"}"
```

**3. EXO-RBAC-Rolle „Transport Rules"** — die eigentliche *Schreib*-Berechtigung
(nur Transportregeln). Am einfachsten über die **Gateway-Oberfläche**:
*Erweitert → Bypass-Wächter → „Berechtigungen der Wächter-Identität einrichten" →
MI-AppId + Objekt-ID eintragen → „Rolle zuweisen"* (das Gateway ruft
`grant_watchdog_role.ps1` mit dem Auth-Zertifikat auf).

Alternativ per Kommandozeile (Skript liegt im Container):
```bash
docker exec exo-signature-gateway pwsh -NoLogo -NonInteractive -File \
  /app/scripts/grant_watchdog_role.ps1 \
  -AppId <GATEWAY_APP_ID> -Organization zarenko.onmicrosoft.com -CertPath /app/data/auth.pfx \
  -WatchdogAppId <MI-AppId> -WatchdogObjectId <MI-ObjectId>
```
(MI-AppId/ObjectId liefert `az functionapp identity show -n $APP -g $RG`.)

⚠️ **Kein „Exchange Administrator".** Die Kombination App-Rolle + Global Reader
(lesen) + „Transport Rules"-RBAC (schreiben) hält den Wächter auf genau eine
Schreib-Fähigkeit begrenzt. Alle Zuweisungen propagieren ~15–30 Min, bevor
`Connect-ExchangeOnline -ManagedIdentity` greift. Die MI heißt in EXO
„EXO Signature Gateway Watchdog" — sie taucht **nicht** im Rollengruppen-Dialog
auf (Apps können keine Rollengruppen-Mitglieder sein; die RBAC-Zuweisung erfolgt
direkt per `New-ManagementRoleAssignment -App`, das erledigt Schritt 3).

## Konfiguration
| App-Einstellung | Zweck |
|---|---|
| `GATEWAY_HEALTH_URL` | z.B. `https://sig.zarenko.net/health` |
| `WATCHDOG_TOKEN` | Klartext-Token; das Gateway prüft dessen PBKDF2-Hash (`/api/watchdog/token/rotate` erzeugt ihn einmalig) |
| `EXO_ORGANIZATION` | z.B. `zarenko.onmicrosoft.com` |
| `SIG_RULE_NAME` | Name der Signatur-Regel, z.B. `Route via EXO Signature Gateway (Signatur)` (aus der Gateway-UI kopieren: Erweitert → Bypass-Wächter) |
| `FAIL_THRESHOLD` | aufeinanderfolgende Fehlversuche bis Bypass (Vorgabe 3, je 10 s) |

## Sicherheit
- **Managed Identity**, kein Secret/Zertifikat im Wächter.
- Rolle **nur** „Transport Rules" — kein Postfach-, Mail- oder Admin-Zugriff.
- Schaltet **ausschließlich** die Signatur-Regel; die S/MIME-Regel wird nie berührt.
- **Entprellt** (mehrere Fehlversuche), damit ein kurzer Aussetzer keinen Bypass auslöst.
- Idempotent: schaltet nur, wenn sich der Zustand tatsächlich ändern muss.
