# Bypass-Wächter — cron-Variante (eigener Zweithost)

Gleiche Aufgabe wie die [Azure-Function](../../azure/watchdog/): überwacht die
Gateway-`/health` und schaltet bei Ausfall die **Signatur**-Transportregel ab
(ausgehende Signatur-Post fließt dann unsigniert weiter, statt in Exchange zu
stauen); bei Erholung schaltet er sie wieder ein. Die **S/MIME-Regel bleibt
immer an** — verschlüsselungsfähige Post darf nie unverschlüsselt hinaus.

> **Wähle diese Variante**, wenn du keinen Azure-Function-Dienst betreiben
> willst. Die Azure-Variante ist bequemer (Managed Identity, kein Zertifikat);
> diese kommt ohne Azure aus, verlangt dafür ein **Zertifikat** der Wächter-App.
> Die Auswahl (Variante, Heartbeat-Token, Aktivierung) triffst du in der
> Oberfläche unter **Erweitert → Bypass-Wächter** (`WATCHDOG_KIND` = `azure` | `cron`).

## ⚠️ Muss auf einem ANDEREN Host laufen
Ein Wächter auf demselben Host stirbt mit dem Gateway und schaltet nie um. Nimm
einen zweiten kleinen Rechner (anderer Raspi, andere VM, ein Always-on-Gerät).

## Voraussetzungen (auf dem Zweithost)
```bash
# PowerShell 7 + Exchange-Modul
sudo apt-get install -y powershell        # oder distro-spezifisch
pwsh -c 'Install-Module ExchangeOnlineManagement -Scope AllUsers -Force'
```
Eine **eigene App-Registrierung** (getrennt von der Gateway-App) mit einem
**Zertifikat** als `.pfx` ohne Passwort. Sie braucht dieselben drei Berechtigungen
wie die Azure-Variante (least-privilege, **kein** „Exchange Administrator"):
1. API-Berechtigung **`Exchange.ManageAsApp`** (Admin-Consent) — Anmelden,
2. Entra-Rolle **`Global Reader`** — kleinste unterstützte Anmelde-Rolle (nur lesen),
3. EXO-RBAC-Rolle **„Transport Rules"** — die eigentliche Schreibberechtigung.

Siehe „Berechtigungen erteilen" unten.

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

## Berechtigungen erteilen (einmalig)
Am einfachsten über die **Gateway-Oberfläche** — der Wizard funktioniert für beide
Varianten: *Erweitert → Bypass-Wächter → „Berechtigungen der Wächter-Identität
einrichten"*, dort die **AppId + SP-Objekt-ID der Zertifikats-App** eintragen
(statt der MI-IDs), dann „Berechtigungen erteilen (Azure-Login)" (Schritt 1+2:
`Exchange.ManageAsApp` + Global Reader) und „Rolle zuweisen" (Schritt 3: Transport
Rules). Die SP-Objekt-ID liefert `az ad sp show --id <WAECHTER-APP-ID> --query id -o tsv`.

Nur die EXO-Schreibrolle auch per Kommandozeile (vom Gateway-Host, mit dem
Gateway-Auth-Zertifikat):
```bash
docker exec exo-signature-gateway pwsh -NoLogo -NonInteractive -File \
  /app/scripts/grant_watchdog_role.ps1 \
  -AppId <GATEWAY_APP_ID> -Organization zarenko.onmicrosoft.com -CertPath /app/data/auth.pfx \
  -WatchdogAppId <WAECHTER-APP-ID> -WatchdogObjectId <WAECHTER-SP-OBJECT-ID>
```
Schritt 1+2 (App-Rolle + Global Reader) manuell: siehe die `az`-Befehle in
[azure/watchdog/README.md](../../azure/watchdog/README.md) — identisch, nur mit der
SP-Objekt-ID der Zertifikats-App.

## Konfiguration (`/etc/exo-watchdog/watchdog.env`)
| Variable | Zweck |
|---|---|
| `GATEWAY_HEALTH_URL` | z.B. `https://sig.zarenko.net/health` |
| `WATCHDOG_TOKEN` | Klartext-Token; das Gateway prüft dessen PBKDF2-Hash (`/api/watchdog/token/rotate`) |
| `EXO_ORGANIZATION` | z.B. `zarenko.onmicrosoft.com` |
| `WATCHDOG_APP_ID` | App-ID der Wächter-App |
| `WATCHDOG_CERT_PATH` | Pfad zur `.pfx` (600) |
| `SIG_RULE_NAME` | Name der Signatur-Regel, z.B. `Route via EXO Signature Gateway (Signatur)` (aus der Gateway-UI kopieren: Erweitert → Bypass-Wächter) |
| `FAIL_THRESHOLD` | Fehlversuche bis Bypass (Vorgabe 3, je 10 s) |

## Sicherheit
- Schaltet **ausschließlich** die Signatur-Regel (Enable/Disable); die S/MIME-Regel
  wird nie berührt, keine Regel wird je gelöscht oder angelegt.
- **Schreiben** nur „Transport Rules" (EXO-RBAC); Lesen via Global Reader. Kein
  Schreibzugriff auf Postfächer/Mail, kein „Exchange Administrator".
- **Entprellt** (mehrere Fehlversuche), damit ein kurzer Aussetzer keinen Bypass auslöst.
- Idempotent: schaltet nur, wenn sich der Zustand tatsächlich ändern muss.
- Das Zertifikat (`600`) ist der Preis fürs Auskommen ohne Azure; schütze es entsprechend.
