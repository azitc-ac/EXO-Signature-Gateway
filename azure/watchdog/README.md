# Bypass-Wächter — Azure Function

Ein **externer** Wächter, der die Gateway-`/health` überwacht und bei Ausfall die
**Signatur**-Transportregel abschaltet (ausgehende Signatur-Post fließt dann
unsigniert weiter, statt in Exchange zu stauen); bei Erholung schaltet er sie
wieder ein. Die **S/MIME-Regel bleibt immer an** — verschlüsselungsfähige Post
darf nie unverschlüsselt hinaus (die wartet bewusst in der Queue).

Er läuft in Azure (serverlos), also **unabhängig vom Gateway-Host** — genau das
ist der Sinn: Wenn der Gateway-Host stirbt, muss jemand anderes handeln.

> Alternative: der **cron-Wächter** (`contrib/watchdog/`) für einen eigenen
> zweiten Host. Beide sind gleichwertig; die Auswahl steht in `WATCHDOG_KIND`
> (`azure` | `cron`).

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

Dann die **minimale EXO-Rolle** an die Managed Identity erteilen (einmalig, mit dem
Gateway-Auth-Zertifikat):
```bash
docker exec exo-signature-gateway pwsh -NoLogo -NonInteractive -File \
  /app/../azure/watchdog/setup_watchdog_role.ps1 \
  -AppId <GATEWAY_APP_ID> -Organization zarenko.onmicrosoft.com -CertPath /app/data/auth.pfx \
  -WatchdogAppId <MI-AppId> -WatchdogObjectId <MI-ObjectId>
```
(MI-AppId/ObjectId liefert `az functionapp identity show -n $APP -g $RG`.)

## Konfiguration
| App-Einstellung | Zweck |
|---|---|
| `GATEWAY_HEALTH_URL` | z.B. `https://sig.zarenko.net/health` |
| `WATCHDOG_TOKEN` | Klartext-Token; das Gateway prüft dessen PBKDF2-Hash (`/api/watchdog/token/rotate` erzeugt ihn einmalig) |
| `EXO_ORGANIZATION` | z.B. `zarenko.onmicrosoft.com` |
| `SIG_RULE_NAME` | Name der Signatur-Regel, z.B. `Route via EXO Signature Gateway` |
| `FAIL_THRESHOLD` | aufeinanderfolgende Fehlversuche bis Bypass (Vorgabe 3, je 10 s) |

## Sicherheit
- **Managed Identity**, kein Secret/Zertifikat im Wächter.
- Rolle **nur** „Transport Rules" — kein Postfach-, Mail- oder Admin-Zugriff.
- Schaltet **ausschließlich** die Signatur-Regel; die S/MIME-Regel wird nie berührt.
- **Entprellt** (mehrere Fehlversuche), damit ein kurzer Aussetzer keinen Bypass auslöst.
- Idempotent: schaltet nur, wenn sich der Zustand tatsächlich ändern muss.
