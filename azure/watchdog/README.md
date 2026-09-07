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
RG=rg-exo-watchdog; APP=exo-sig-watchdog; LOC=westeurope
az group create -n $RG -l $LOC
az storage account create -n exosigwd$RANDOM -g $RG -l $LOC --sku Standard_LRS
az functionapp create -n $APP -g $RG -s <storage> -c $LOC \
   --runtime powershell --runtime-version 7.4 --functions-version 4 --consumption-plan-location $LOC
az functionapp identity assign -n $APP -g $RG          # System-assigned Managed Identity
func azure functionapp publish $APP                     # aus diesem Verzeichnis

# App-Einstellungen
az functionapp config appsettings set -n $APP -g $RG --settings \
   GATEWAY_HEALTH_URL="https://sig.zarenko.net/health" \
   EXO_ORGANIZATION="zarenko.onmicrosoft.com" \
   SIG_RULE_NAME="Route via EXO Signature Gateway" \
   FAIL_THRESHOLD=3 \
   WATCHDOG_TOKEN="<Token aus /api/watchdog/token/rotate>"
```

Die Managed Identity braucht für Exchange **zwei** Dinge (siehe
[MS-Doku](https://learn.microsoft.com/en-us/powershell/exchange/connect-exo-powershell-managed-identity)):

**1. App-Rolle `Exchange.ManageAsApp`** — der reine *Anmelde*-Schlüssel. Für sich
genommen erlaubt sie NICHTS (jedes Cmdlet → 403); erst die Rolle in Schritt 2 gibt
Berechtigungen. Einmalig per Graph erteilen (braucht Tenant-Directory-Admin):
```bash
MI_OBJ=$(az ad sp list --filter "displayName eq '$APP'" --query "[0].id" -o tsv)
EXO_SP=$(az ad sp show --id 00000002-0000-0ff1-ce00-000000000000 --query id -o tsv)
az rest --method POST \
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/$MI_OBJ/appRoleAssignments" \
  --headers "Content-Type=application/json" \
  --body "{\"principalId\":\"$MI_OBJ\",\"resourceId\":\"$EXO_SP\",\"appRoleId\":\"dc50a0fb-09a3-484d-be87-e023b12c6440\"}"
```

**2. Minimale EXO-RBAC-Rolle „Transport Rules"** — die eigentliche *Berechtigung*
(nur Transportregeln, sonst nichts). Einmalig mit dem Gateway-Auth-Zertifikat:
```bash
docker exec exo-signature-gateway pwsh -NoLogo -NonInteractive -File \
  /app/../azure/watchdog/setup_watchdog_role.ps1 \
  -AppId <GATEWAY_APP_ID> -Organization zarenko.onmicrosoft.com -CertPath /app/data/auth.pfx \
  -WatchdogAppId <MI-AppId> -WatchdogObjectId <MI-ObjectId>
```
(MI-AppId/ObjectId liefert `az functionapp identity show -n $APP -g $RG`.)

⚠️ **Keine breite Entra-Rolle** (z.B. „Exchange Administrator") zuweisen — dann wäre
`Exchange.ManageAsApp` voller EXO-Zugriff. Die Kombination App-Rolle + „Transport
Rules"-RBAC hält den Wächter auf genau eine Fähigkeit begrenzt. Beide Zuweisungen
propagieren ~15–30 Min, bevor `Connect-ExchangeOnline -ManagedIdentity` greift.

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
