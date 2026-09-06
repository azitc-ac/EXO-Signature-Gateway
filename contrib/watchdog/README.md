# Bypass-Wächter — cron-Variante (eigener Zweithost)

Gleiche Aufgabe wie die [Azure-Function](../../azure/watchdog/): überwacht die
Gateway-`/health` und schaltet bei Ausfall die **Signatur**-Transportregel ab
(ausgehende Signatur-Post fließt dann unsigniert weiter, statt in Exchange zu
stauen); bei Erholung schaltet er sie wieder ein. Die **S/MIME-Regel bleibt
immer an** — verschlüsselungsfähige Post darf nie unverschlüsselt hinaus.

> **Wähle diese Variante**, wenn du keinen Azure-Function-Dienst betreiben
> willst. Die Azure-Variante ist bequemer (Managed Identity, kein Zertifikat);
> diese kommt ohne Azure aus, verlangt dafür ein **Zertifikat** der Wächter-App.
> Die Auswahl steht in `WATCHDOG_KIND` (`azure` | `cron`).

## ⚠️ Muss auf einem ANDEREN Host laufen
Ein Wächter auf demselben Host stirbt mit dem Gateway und schaltet nie um. Nimm
einen zweiten kleinen Rechner (anderer Raspi, andere VM, ein Always-on-Gerät).

## Voraussetzungen (auf dem Zweithost)
```bash
# PowerShell 7 + Exchange-Modul
sudo apt-get install -y powershell        # oder distro-spezifisch
pwsh -c 'Install-Module ExchangeOnlineManagement -Scope AllUsers -Force'
```
Ein **Zertifikat der Wächter-App** (eigene App-Registrierung mit
`Exchange.ManageAsApp`, minimale Rolle „Transport Rules") als `.pfx` ohne
Passwort. Getrennt von der Gateway-App halten — der Wächter braucht nur diese
eine Fähigkeit.

## Installation
```bash
sudo mkdir -p /opt/exo-watchdog /etc/exo-watchdog
sudo cp watchdog.ps1 /opt/exo-watchdog/
sudo cp watchdog.env.example /etc/exo-watchdog/watchdog.env
sudo cp <deine-waechter-app>.pfx /etc/exo-watchdog/watchdog.pfx
sudo chmod 600 /etc/exo-watchdog/watchdog.env /etc/exo-watchdog/watchdog.pfx
sudo $EDITOR /etc/exo-watchdog/watchdog.env      # Werte eintragen

sudo cp exo-watchdog.service exo-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now exo-watchdog.timer
```
Prüfen:
```bash
systemctl list-timers exo-watchdog.timer
sudo systemctl start exo-watchdog.service && journalctl -u exo-watchdog -n 30
```

## Minimale EXO-Rolle einmalig erteilen
Dieselbe „Transport Rules"-Rolle wie bei der Azure-Variante, nur dass hier eine
App **mit Zertifikat** statt einer Managed Identity berechtigt wird — vom
Gateway-Host aus mit dem Gateway-Auth-Zertifikat:
```bash
docker exec exo-signature-gateway pwsh -NoLogo -NonInteractive -File \
  /app/../azure/watchdog/setup_watchdog_role.ps1 \
  -AppId <GATEWAY_APP_ID> -Organization zarenko.onmicrosoft.com -CertPath /app/data/auth.pfx \
  -WatchdogAppId <WAECHTER-APP-ID> -WatchdogObjectId <WAECHTER-SP-OBJECT-ID>
```

## Konfiguration (`/etc/exo-watchdog/watchdog.env`)
| Variable | Zweck |
|---|---|
| `GATEWAY_HEALTH_URL` | z.B. `https://sig.zarenko.net/health` |
| `WATCHDOG_TOKEN` | Klartext-Token; das Gateway prüft dessen PBKDF2-Hash (`/api/watchdog/token/rotate`) |
| `EXO_ORGANIZATION` | z.B. `zarenko.onmicrosoft.com` |
| `WATCHDOG_APP_ID` | App-ID der Wächter-App |
| `WATCHDOG_CERT_PATH` | Pfad zur `.pfx` (600) |
| `SIG_RULE_NAME` | Name der Signatur-Regel, z.B. `Route via EXO Signature Gateway` |
| `FAIL_THRESHOLD` | Fehlversuche bis Bypass (Vorgabe 3, je 10 s) |

## Sicherheit
- Schaltet **ausschließlich** die Signatur-Regel (Enable/Disable); die S/MIME-Regel
  wird nie berührt, keine Regel wird je gelöscht oder angelegt.
- Rolle **nur** „Transport Rules" — kein Postfach-, Mail- oder Admin-Zugriff.
- **Entprellt** (mehrere Fehlversuche), damit ein kurzer Aussetzer keinen Bypass auslöst.
- Idempotent: schaltet nur, wenn sich der Zustand tatsächlich ändern muss.
- Das Zertifikat (`600`) ist der Preis fürs Auskommen ohne Azure; schütze es entsprechend.
